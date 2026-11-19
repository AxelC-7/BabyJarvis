import queue
import threading
import time
import unittest
from unittest.mock import patch

from agent_service import (
    ActionProposalEvent,
    ActivityEvent,
    AgentService,
    AgentState,
    CaptureRequestEvent,
    ErrorEvent,
    ModelDiagnosticsCompletedEvent,
    ModelsDiscoveredEvent,
    ScreenshotEvent,
    StatusEvent,
    TaskFinishedEvent,
)
from desktop_geometry import ScreenGeometry
from gemini_agent import TaskContext
from screen_vision import CursorAction, ScreenFrame


class AgentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geometry = ScreenGeometry(0, 0, 200, 100)
        self.frame = ScreenFrame(
            png_bytes=b"test-image",
            width=200,
            height=100,
            geometry=self.geometry,
            captured_at=0,
        )
        self.services: list[AgentService] = []

    def tearDown(self) -> None:
        for service in self.services:
            service.shutdown(wait=True)

    def make_service(self, task_runner, **kwargs) -> AgentService:
        service = AgentService(
            client_factory=lambda: object(),
            task_runner=task_runner,
            **kwargs,
        )
        self.services.append(service)
        service.start()
        return service

    def next_event(self, service: AgentService, event_type, timeout: float = 2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = service.events.get(timeout=0.05)
            except queue.Empty:
                continue
            if isinstance(event, event_type):
                return event
        self.fail(f"No se recibió un evento {event_type.__name__}.")

    def test_worker_uses_event_queue_and_serializes_submitted_tasks(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        order: list[str] = []
        active = 0
        maximum_active = 0
        guard = threading.Lock()

        def runner(_client, instruction, *, control):
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            order.append(instruction)
            if instruction == "primera":
                first_started.set()
                release_first.wait(2)
            with guard:
                active -= 1
            return True

        service = self.make_service(runner)
        task_one = service.submit_task("primera")
        self.assertTrue(first_started.wait(1))
        task_two = service.submit_task("segunda")
        self.assertNotEqual(task_one, task_two)
        release_first.set()
        finished = [
            self.next_event(service, TaskFinishedEvent),
            self.next_event(service, TaskFinishedEvent),
        ]
        self.assertEqual(order, ["primera", "segunda"])
        self.assertEqual(maximum_active, 1)
        self.assertEqual({event.task_id for event in finished}, {task_one, task_two})

    def test_action_batches_run_autonomously_without_waiting_for_approval(self) -> None:
        service = self.make_service(lambda *_args, **_kwargs: True)
        action = CursorAction("click", x=8, y=12)
        context = TaskContext(
            deadline=time.monotonic() + 2,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        result: queue.Queue[bool] = queue.Queue()
        with patch("gemini_agent._check_task"):
            worker = threading.Thread(target=lambda: result.put(service._propose_batch(
                type("Task", (), {"task_id": "task-auto"})(),
                (action,), self.frame, True, context
            )))
            worker.start()
            proposal = self.next_event(service, ActionProposalEvent)
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(proposal.requires_authorization)
        self.assertIsNone(proposal.expires_at)
        self.assertIn("modo autónomo", proposal.summary.casefold())
        self.assertTrue(result.get_nowait())

    def test_pause_resume_and_emergency_stop_are_delivered_to_worker(self) -> None:
        started = threading.Event()
        observed_pause = threading.Event()
        observed_resume = threading.Event()
        cancelled = threading.Event()
        release = threading.Event()
        started_instructions: list[str] = []

        def runner(_client, instruction, *, control):
            started_instructions.append(instruction)
            started.set()
            while not release.wait(0.01):
                if control.paused.is_set():
                    observed_pause.set()
                elif observed_pause.is_set():
                    observed_resume.set()
                if control.cancelled.is_set():
                    cancelled.set()
                    return False
            return True

        service = self.make_service(runner)
        service.submit_task("control")
        self.assertTrue(started.wait(1))
        service.submit_task("queued")
        self.assertTrue(service.pause())
        self.assertTrue(observed_pause.wait(1))
        self.assertTrue(service.resume())
        self.assertTrue(observed_resume.wait(1))
        service.emergency_stop()
        self.assertTrue(cancelled.wait(1))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            event = self.next_event(service, StatusEvent, timeout=1)
            if event.state == AgentState.STOPPED:
                break
        else:
            self.fail("No se publicó el estado STOPPED tras la parada de emergencia.")
        self.assertEqual(started_instructions, ["control"])

    def test_worker_exceptions_are_reported_as_error_events(self) -> None:
        def runner(*_args, **_kwargs):
            raise RuntimeError("error simulado del worker")

        service = self.make_service(runner)
        task_id = service.submit_task("fallar")
        error = self.next_event(service, ErrorEvent)
        self.assertEqual(error.message, "error simulado del worker")
        self.assertEqual(error.task_id, task_id)
        finished = self.next_event(service, TaskFinishedEvent)
        self.assertFalse(finished.success)

    def test_capture_is_requested_and_returned_through_events(self) -> None:
        service = self.make_service(lambda *_args, **_kwargs: True)
        task = type("Task", (), {"task_id": "capture-task"})()
        context = TaskContext(
            deadline=time.monotonic() + 2,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        result: queue.Queue[ScreenFrame] = queue.Queue()

        with patch("gemini_agent._check_task"):
            worker = threading.Thread(
                target=lambda: result.put(service._capture(task, context))
            )
            worker.start()
            request = self.next_event(service, CaptureRequestEvent)
            request.reply.put(self.frame)
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get_nowait(), self.frame)
        screenshot = self.next_event(service, ScreenshotEvent)
        self.assertEqual(screenshot.frame, self.frame)

    def test_model_discovery_and_compatibility_tests_use_worker_queue(self) -> None:
        class FakeModelManager:
            def fresh_report(self):
                return {"discovered_models": ["model-a"]}

            def fresh_discovered_models(self):
                return ["model-a"]

            def discover_models(self, client, *, cancelled, progress):
                progress("discover", 1, None, "model-a")
                return ["model-a"]

            def test_models(self, client, models, *, cancelled, progress):
                from gemini_model_manager import ModelDiagnosticResult

                progress("test", 1, 1, models[0])
                return [
                    ModelDiagnosticResult(
                        models[0], "compatible_text_image", 0.1, 0.05, 0.05
                    )
                ]

        service = AgentService(
            client_factory=lambda: object(),
            task_runner=lambda *_args, **_kwargs: True,
            model_manager=FakeModelManager(),
        )
        self.services.append(service)
        service.start()

        service.discover_models()
        discovered = self.next_event(service, ModelsDiscoveredEvent)
        self.assertEqual(discovered.models, ("model-a",))

        service.test_model_compatibility()
        results = self.next_event(service, ModelDiagnosticsCompletedEvent)
        self.assertEqual(results.results[0].model, "model-a")
        self.assertFalse(results.cancelled)

    def test_cancel_model_diagnostics_sets_the_running_cancellation_event(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingModelManager:
            def fresh_report(self):
                return {"discovered_models": ["model-a"]}

            def fresh_discovered_models(self):
                return ["model-a"]

            def discover_models(self, client, *, cancelled, progress):
                started.set()
                release.wait(1)
                return []

            def test_models(self, *_args, **_kwargs):
                return []

        service = AgentService(
            client_factory=lambda: object(),
            task_runner=lambda *_args, **_kwargs: True,
            model_manager=BlockingModelManager(),
        )
        self.services.append(service)
        service.start()
        service.discover_models()
        self.assertTrue(started.wait(1))

        self.assertTrue(service.cancel_model_tests())
        self.assertFalse(service.cancel_model_tests())
        release.set()
if __name__ == "__main__":
    unittest.main()
