import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from google.genai import types

from desktop_geometry import ScreenGeometry
from gemini_agent import (
    AgentDecision,
    GeminiRequestTimeout,
    TaskContext,
    TaskControl,
    _generate_with_fallback,
    ask_gemini,
    create_client,
    _needs_authorization,
    _redacted_actions,
    _repeated_click_point,
    run_task,
)
from screen_vision import CursorAction, ScreenFrame


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geometry = ScreenGeometry(0, 0, 1366, 768)
        self.frame = ScreenFrame(
            png_bytes=b"png",
            width=1366,
            height=768,
            geometry=self.geometry,
            captured_at=0.0,
        )

    def test_only_mutating_actions_require_authorization(self) -> None:
        self.assertFalse(_needs_authorization([CursorAction("move", x=1, y=2)]))
        self.assertFalse(_needs_authorization([CursorAction("wait", duration=1)]))
        self.assertTrue(_needs_authorization([CursorAction("click", x=1, y=2)]))

    def test_sensitive_text_is_not_written_to_action_logs(self) -> None:
        actions = [CursorAction("write", text="private text")]
        self.assertEqual(
            _redacted_actions(actions),
            [{"type": "write", "characters": 12}],
        )

    def test_api_client_is_blocked_until_key_rotation_is_acknowledged(self) -> None:
        with (
            patch("gemini_agent.load_env_file"),
            patch.dict("os.environ", {"GEMINI_KEY_ROTATED": "false"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_KEY_ROTATED=true"):
                create_client()

    def test_gemini_adapter_uses_the_shared_action_contract(self) -> None:
        response = SimpleNamespace(
            text='{"status":"continue","actions":[{"type":"click","x":12,"y":34,"button":"left"}]}'
        )
        client = SimpleNamespace(
            models=SimpleNamespace(generate_content=Mock(return_value=response))
        )
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        with patch("gemini_agent._run_api_call", side_effect=lambda operation, _: operation()):
            decision = ask_gemini(client, "test task", self.frame, [], context)
        self.assertEqual(decision.status, "continue")
        self.assertEqual(len(decision.actions), 1)
        self.assertEqual(decision.actions[0].action, "click")
        self.assertEqual((decision.actions[0].x, decision.actions[0].y), (12, 34))
        client.models.generate_content.assert_called_once()

    def test_sdk_retries_are_disabled_and_timeout_is_configurable(self) -> None:
        env = {
            "GEMINI_KEY_ROTATED": "true",
            "GEMINI_API_KEY": "unit-test-key",
            "GEMINI_TIMEOUT_SECONDS": "75",
            "GEMINI_MAX_ATTEMPTS_PER_MODEL": "2",
            "GEMINI_RETRY_DELAY_SECONDS": "0.5",
        }
        globals_to_restore = {
            "MODEL": "test-model",
            "VERIFICATION_MODEL": "test-verifier",
            "TIMEOUT_SECONDS": 30.0,
            "MAX_ACTIONS": 8,
            "MAX_TASK_DURATION_SECONDS": 180.0,
            "MAX_API_ATTEMPTS_PER_MODEL": 2,
            "RETRY_INITIAL_DELAY_SECONDS": 1.0,
        }
        with (
            patch("gemini_agent.load_env_file"),
            patch.dict("os.environ", env),
            patch.multiple("gemini_agent", **globals_to_restore),
            patch("gemini_agent.genai.Client") as sdk_client,
        ):
            create_client()

        options = sdk_client.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, 75_000)
        self.assertEqual(options.retry_options.attempts, 1)

    def test_read_timeout_retries_are_bounded_and_never_return_success(self) -> None:
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        timeout = httpx.ReadTimeout("simulated read timeout")
        with (
            patch("gemini_agent.MAX_API_ATTEMPTS_PER_MODEL", 2),
            patch("gemini_agent._run_api_call", side_effect=[timeout, timeout]) as call,
            patch("gemini_agent._sleep_for_task"),
        ):
            with self.assertRaises(GeminiRequestTimeout):
                _generate_with_fallback(
                    object(),
                    [],
                    types.GenerateContentConfig(),
                    context,
                    models=("test-model",),
                )
        self.assertEqual(call.call_count, 2)

    def test_read_timeout_can_recover_within_the_configured_budget(self) -> None:
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        response = object()
        with (
            patch("gemini_agent.MAX_API_ATTEMPTS_PER_MODEL", 2),
            patch(
                "gemini_agent._run_api_call",
                side_effect=[httpx.ReadTimeout("simulated"), response],
            ) as call,
            patch("gemini_agent._sleep_for_task"),
        ):
            result = _generate_with_fallback(
                object(),
                [],
                types.GenerateContentConfig(),
                context,
                models=("test-model",),
            )
        self.assertIs(result, response)
        self.assertEqual(call.call_count, 2)

    def test_non_timeout_validation_error_is_not_retried(self) -> None:
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        with patch(
            "gemini_agent._run_api_call",
            side_effect=ValueError("invalid request"),
        ) as call:
            with self.assertRaisesRegex(ValueError, "invalid request"):
                _generate_with_fallback(
                    object(),
                    [],
                    types.GenerateContentConfig(),
                    context,
                    models=("test-model",),
                )
        call.assert_called_once()

    def test_repeated_click_guard_rejects_third_same_pixel(self) -> None:
        actions = [
            CursorAction("click", x=5, y=6),
            CursorAction("click", x=5, y=6),
            CursorAction("click", x=5, y=6),
        ]
        self.assertEqual(_repeated_click_point(actions, None, 0), (5, 6))
        self.assertIsNone(_repeated_click_point(actions[:2], None, 0))
        self.assertEqual(_repeated_click_point(actions[:1], (5, 6), 2), (5, 6))

    def test_denied_batch_executes_no_action(self) -> None:
        click = CursorAction("click", x=12, y=34)
        with (
            patch("gemini_agent.get_screen_geometry", return_value=self.geometry),
            patch("gemini_agent.get_cursor_position", return_value=(100, 100)),
            patch("gemini_agent.capture_screen", return_value=self.frame),
            patch(
                "gemini_agent.ask_gemini",
                return_value=AgentDecision("continue", actions=(click,)),
            ),
            patch("gemini_agent._request_authorization", return_value=False),
            patch("gemini_agent._execute_action") as execute,
            patch("gemini_agent.print"),
        ):
            result = run_task(object(), "safe test")
        self.assertFalse(result)
        execute.assert_not_called()

    def test_success_status_requires_a_fresh_verification(self) -> None:
        decisions = [
            AgentDecision("continue", actions=(CursorAction("move", x=12, y=34),)),
            AgentDecision("success", message="done"),
        ]
        with (
            patch("gemini_agent.get_screen_geometry", return_value=self.geometry),
            patch("gemini_agent.get_cursor_position", return_value=(100, 100)),
            patch("gemini_agent.capture_screen", return_value=self.frame),
            patch("gemini_agent.ask_gemini", side_effect=decisions),
            patch(
                "gemini_agent._execute_action",
                return_value="Cursor movido",
            ),
            patch("gemini_agent._sleep_for_task"),
            patch("gemini_agent._verify_task", return_value=(True, "visible result")) as verify,
            patch("gemini_agent.print"),
        ):
            result = run_task(object(), "safe test")
        self.assertTrue(result)
        verify.assert_called_once()

    def test_run_task_uses_service_callbacks_instead_of_terminal_output(self) -> None:
        activity: list[str] = []
        statuses: list[str] = []
        proposals = []
        control = TaskControl(
            activity=activity.append,
            status=statuses.append,
            capture=lambda: self.frame,
            propose_batch=lambda actions, frame, required, _context: (
                proposals.append((actions, frame, required)) or True
            ),
        )
        decision = AgentDecision(
            "continue",
            actions=(CursorAction("click", x=12, y=34),),
        )
        with (
            patch("gemini_agent.get_screen_geometry", return_value=self.geometry),
            patch("gemini_agent.get_cursor_position", return_value=(100, 100)),
            patch("gemini_agent.ask_gemini", side_effect=[decision, AgentDecision("success", message="done")]),
            patch("gemini_agent._execute_action", return_value="Clic") as execute,
            patch("gemini_agent._sleep_for_task"),
            patch("gemini_agent._verify_task", return_value=(True, "visible result")),
            patch("builtins.print") as terminal_output,
        ):
            result = run_task(object(), "safe test", control=control)

        self.assertTrue(result)
        execute.assert_called_once()
        terminal_output.assert_not_called()
        self.assertEqual(len(proposals), 1)
        self.assertTrue(proposals[0][2])
        self.assertIn("thinking", statuses)
        self.assertIn("executing", statuses)
        self.assertTrue(activity)

    def test_error_status_stops_without_execution_or_success_verification(self) -> None:
        with (
            patch("gemini_agent.get_screen_geometry", return_value=self.geometry),
            patch("gemini_agent.get_cursor_position", return_value=(100, 100)),
            patch("gemini_agent.capture_screen", return_value=self.frame),
            patch(
                "gemini_agent.ask_gemini",
                return_value=AgentDecision("error", message="blocked"),
            ),
            patch("gemini_agent._execute_action") as execute,
            patch("gemini_agent._verify_task") as verify,
            patch("gemini_agent.print"),
        ):
            result = run_task(object(), "safe test")
        self.assertFalse(result)
        execute.assert_not_called()
        verify.assert_not_called()

    def test_task_timeout_from_gemini_is_not_success(self) -> None:
        with (
            patch("gemini_agent.get_screen_geometry", return_value=self.geometry),
            patch("gemini_agent.get_cursor_position", return_value=(100, 100)),
            patch("gemini_agent.capture_screen", return_value=self.frame),
            patch(
                "gemini_agent.ask_gemini",
                side_effect=GeminiRequestTimeout("simulated timeout"),
            ),
            patch("gemini_agent._execute_action") as execute,
            patch("gemini_agent._verify_task") as verify,
            patch("gemini_agent.print"),
        ):
            with self.assertRaises(GeminiRequestTimeout):
                run_task(object(), "safe test")
        execute.assert_not_called()
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
