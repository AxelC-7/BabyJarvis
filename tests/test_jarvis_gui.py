import time
import queue
import unittest
from unittest.mock import Mock

from desktop_geometry import ScreenGeometry
from agent_service import (
    ActionProposalEvent,
    AgentState,
    ErrorEvent,
    StatusEvent,
    CaptureRequestEvent,
)
from jarvis_gui import JarvisApp, main
from screen_vision import ScreenFrame
from screen_vision import CursorAction


class JarvisGuiAuthorizationTests(unittest.TestCase):
    def make_app(self, proposal: ActionProposalEvent) -> JarvisApp:
        app = JarvisApp.__new__(JarvisApp)
        app._pending_proposal = proposal
        app.service = Mock()
        app.approve_button = Mock()
        app.reject_button = Mock()
        app.activity = Mock()
        return app

    def test_approval_submits_the_exact_proposal_id_and_actions(self) -> None:
        actions = (CursorAction("click", x=8, y=12),)
        proposal = ActionProposalEvent(
            task_id="task-1",
            batch_id="batch-1",
            actions=actions,
            summary="click",
            requires_authorization=True,
            expires_at=time.monotonic() + 10,
        )
        app = self.make_app(proposal)

        app._respond_to_proposal(True)

        app.service.authorize.assert_called_once_with("batch-1", actions, True)
        self.assertIsNone(app._pending_proposal)

    def test_rejection_submits_the_exact_proposal_without_approval(self) -> None:
        actions = (CursorAction("write", text="temporary text"),)
        proposal = ActionProposalEvent(
            task_id="task-2",
            batch_id="batch-2",
            actions=actions,
            summary="write",
            requires_authorization=True,
            expires_at=time.monotonic() + 10,
        )
        app = self.make_app(proposal)

        app._respond_to_proposal(False)

        app.service.authorize.assert_called_once_with("batch-2", actions, False)
        self.assertIsNone(app._pending_proposal)

    def test_expired_gui_approval_is_not_sent(self) -> None:
        proposal = ActionProposalEvent(
            task_id="task-3",
            batch_id="batch-3",
            actions=(CursorAction("click", x=2, y=3),),
            summary="click",
            requires_authorization=True,
            expires_at=time.monotonic() - 1,
        )
        app = self.make_app(proposal)

        app._respond_to_proposal(True)

        app.service.authorize.assert_not_called()
        self.assertIsNone(app._pending_proposal)

    def test_stale_expiration_does_not_clear_a_newer_proposal(self) -> None:
        current = ActionProposalEvent(
            task_id="task-new",
            batch_id="batch-new",
            actions=(CursorAction("click", x=2, y=3),),
            summary="click",
            requires_authorization=True,
            expires_at=time.monotonic() + 10,
        )
        app = self.make_app(current)

        app._expire_proposal("batch-old")

        self.assertIs(app._pending_proposal, current)
        app.service.authorize.assert_not_called()

    def test_completed_and_failed_tasks_do_not_destroy_or_recreate_window(self) -> None:
        app = JarvisApp.__new__(JarvisApp)
        app.root = Mock()
        app._state = AgentState.THINKING
        app._active_task_id = "task-1"
        app._pending_proposal = None
        app.state_label = Mock()
        app.approve_button = Mock()
        app.reject_button = Mock()
        app.activity = Mock()
        app.model_progress = Mock()
        app.model_progress_text = Mock()
        app._closing = False

        app._handle_event(StatusEvent(AgentState.COMPLETED, "task-1"))
        app._handle_event(ErrorEvent("Gemini failed", "task-2"))

        app.root.destroy.assert_not_called()
        app.root.quit.assert_not_called()
        self.assertEqual(app._active_task_id, None)

    def test_main_creates_one_tk_instance_and_one_mainloop(self) -> None:
        root = Mock()
        with (
            unittest.mock.patch("gemini_agent.setup_logging"),
            unittest.mock.patch("jarvis_gui.tk.Tk", return_value=root) as create_tk,
            unittest.mock.patch("jarvis_gui.JarvisApp") as app,
        ):
            main()

        create_tk.assert_called_once_with()
        app.assert_called_once_with(root)
        root.mainloop.assert_called_once_with()

    def test_capture_does_not_hide_gui_when_it_is_outside_primary_monitor(self) -> None:
        app = JarvisApp.__new__(JarvisApp)
        app.root = Mock()
        app._closing = False
        app._window_overlaps_primary_monitor = Mock(return_value=False)
        frame = ScreenFrame(b"png", 2, 2, ScreenGeometry(0, 0, 2, 2), 0)
        reply: queue.Queue[ScreenFrame | BaseException] = queue.Queue()
        event = CaptureRequestEvent("request", reply)
        app.root.after.side_effect = lambda _delay, callback: callback()

        with unittest.mock.patch("jarvis_gui.capture_screen", return_value=frame):
            app._capture_without_jarvis(event)

        self.assertIs(reply.get_nowait(), frame)
        app.root.withdraw.assert_not_called()
        app.root.deiconify.assert_not_called()

    def test_capture_restores_gui_after_capture_when_it_overlaps_primary(self) -> None:
        app = JarvisApp.__new__(JarvisApp)
        app.root = Mock()
        app._closing = False
        app._window_overlaps_primary_monitor = Mock(return_value=True)
        frame = ScreenFrame(b"png", 2, 2, ScreenGeometry(0, 0, 2, 2), 0)
        reply: queue.Queue[ScreenFrame | BaseException] = queue.Queue()
        event = CaptureRequestEvent("request", reply)
        app.root.after.side_effect = lambda _delay, callback: callback()

        with unittest.mock.patch("jarvis_gui.capture_screen", return_value=frame):
            app._capture_without_jarvis(event)

        self.assertIs(reply.get_nowait(), frame)
        app.root.withdraw.assert_called_once_with()
        app.root.deiconify.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
