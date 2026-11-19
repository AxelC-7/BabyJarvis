import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from google.genai import errors, types

from desktop_geometry import ScreenGeometry
from gemini_agent import (
    AgentDecision,
    GeminiRequestTimeout,
    TaskContext,
    TaskControl,
    _generate_with_fallback,
    _verification_models,
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
        verifier_preflight = patch("gemini_agent._verification_models", return_value=("verified-test-model",))
        verifier_preflight.start()
        self.addCleanup(verifier_preflight.stop)
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

    def test_primary_model_404_tries_fallback_once_without_repeating_primary(self) -> None:
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        response = object()
        not_found = errors.APIError(
            code=404,
            response_json={
                "error": {
                    "code": 404,
                    "message": "model is not available for this account",
                    "status": "NOT_FOUND",
                }
            },
        )
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(side_effect=[not_found, response])
            )
        )

        result = _generate_with_fallback(
            client,
            [],
            types.GenerateContentConfig(),
            context,
            models=("primary-model", "fallback-model"),
        )

        self.assertIs(result, response)
        calls = client.models.generate_content.call_args_list
        self.assertEqual(
            [call.kwargs["model"] for call in calls],
            ["primary-model", "fallback-model"],
        )

    def test_model_changes_are_reported_to_task_activity(self) -> None:
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        messages = []
        context.activity = messages.append
        not_found = errors.APIError(
            code=404,
            response_json={
                "error": {
                    "code": 404,
                    "message": "model is not available",
                    "status": "NOT_FOUND",
                }
            },
        )
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(
                    side_effect=[not_found, SimpleNamespace(text="ok")]
                )
            )
        )

        _generate_with_fallback(
            client,
            [],
            types.GenerateContentConfig(),
            context,
            models=("primary-model", "fallback-model"),
        )

        self.assertEqual(
            messages,
            [
                "Usando el modelo Gemini primary-model.",
                "Cambiando al modelo Gemini fallback-model.",
            ],
        )

    def test_incompatible_model_request_uses_next_verified_candidate(self) -> None:
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        incompatible = errors.APIError(
            code=400,
            response_json={
                "error": {
                    "code": 400,
                    "message": "This model does not support image input",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(
                    side_effect=[incompatible, SimpleNamespace(text="ok")]
                )
            )
        )

        with patch(
            "gemini_agent.MODEL_MANAGER.candidate_models",
            return_value=("primary-model", "verified-fallback"),
        ):
            result = _generate_with_fallback(
                client,
                [],
                types.GenerateContentConfig(),
                context,
            )

        self.assertEqual(result.text, "ok")
        self.assertEqual(
            [call.kwargs["model"] for call in client.models.generate_content.call_args_list],
            ["primary-model", "verified-fallback"],
        )

    def test_404_tries_each_configured_model_once(self) -> None:
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        not_found = errors.APIError(
            code=404,
            response_json={"error": {"message": "model not found"}},
        )
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(side_effect=[not_found, not_found])
            )
        )

        with self.assertRaisesRegex(RuntimeError, "no estuvo disponible"):
            _generate_with_fallback(
                client,
                [],
                types.GenerateContentConfig(),
                context,
                models=("primary-model", "fallback-model"),
            )

        calls = client.models.generate_content.call_args_list
        self.assertEqual(
            [call.kwargs["model"] for call in calls],
            ["primary-model", "fallback-model"],
        )

    def test_temporary_http_errors_keep_bounded_retries(self) -> None:
        for code in (429, 500, 503):
            with self.subTest(code=code):
                context = TaskContext(
                    deadline=time.monotonic() + 10,
                    geometry=self.geometry,
                    cancelled=threading.Event(),
                )
                temporary_error = errors.APIError(
                    code=code,
                    response_json={
                        "error": {
                            "code": code,
                            "message": "temporary service failure",
                            "status": "UNAVAILABLE",
                        }
                    },
                )
                response = object()
                client = SimpleNamespace(
                    models=SimpleNamespace(
                        generate_content=Mock(
                            side_effect=[temporary_error, response]
                        )
                    )
                )
                with patch("gemini_agent._sleep_for_task") as sleep:
                    result = _generate_with_fallback(
                        client,
                        [],
                        types.GenerateContentConfig(),
                        context,
                        models=("test-model",),
                    )

                self.assertIs(result, response)
                self.assertEqual(client.models.generate_content.call_count, 2)
                sleep.assert_called_once()

    def test_http_429_retries_current_model_but_does_not_fan_out(self) -> None:
        context = TaskContext(
            deadline=time.monotonic() + 10,
            geometry=self.geometry,
            cancelled=threading.Event(),
        )
        quota = errors.APIError(
            429,
            {"error": {"code": 429, "message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
        )
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(side_effect=[quota, quota])
            )
        )
        with (
            patch("gemini_agent.MAX_API_ATTEMPTS_PER_MODEL", 2),
            patch("gemini_agent._sleep_for_task"),
        ):
            with self.assertRaisesRegex(RuntimeError, "cuota"):
                _generate_with_fallback(
                    client,
                    [],
                    types.GenerateContentConfig(),
                    context,
                    models=("primary", "fallback"),
                )
        self.assertEqual(
            [call.kwargs["model"] for call in client.models.generate_content.call_args_list],
            ["primary", "primary"],
        )

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


class VerificationModelSelectionTests(unittest.TestCase):
    def test_prefers_configured_verifier_when_it_passed_multimodal_probe(self) -> None:
        compatible = [
            SimpleNamespace(model="gemini-3.6-flash"),
            SimpleNamespace(model="gemini-3.5-flash-lite"),
        ]
        with (
            patch("gemini_agent.MODEL", "gemini-3.5-flash-lite"),
            patch("gemini_agent.VERIFICATION_MODEL", "gemini-3.6-flash"),
            patch("gemini_agent.MODEL_MANAGER.fresh_results", return_value=compatible),
            patch("gemini_agent.MODEL_MANAGER.results_are_fresh", return_value=True),
        ):
            self.assertEqual(_verification_models(), ("gemini-3.6-flash",))

    def test_uses_another_verified_model_when_configured_verifier_is_unavailable(self) -> None:
        compatible = [
            SimpleNamespace(model="gemini-3.5-flash-lite"),
            SimpleNamespace(model="gemini-3.6-flash"),
        ]
        with (
            patch("gemini_agent.MODEL", "gemini-3.5-flash-lite"),
            patch("gemini_agent.VERIFICATION_MODEL", "gemini-2.5-flash"),
            patch("gemini_agent.MODEL_MANAGER.fresh_results", return_value=compatible),
            patch("gemini_agent.MODEL_MANAGER.results_are_fresh", return_value=True),
        ):
            self.assertEqual(_verification_models(), ("gemini-3.6-flash",))

    def test_refuses_verification_without_an_independent_verified_model(self) -> None:
        with (
            patch("gemini_agent.MODEL", "gemini-3.5-flash-lite"),
            patch("gemini_agent.VERIFICATION_MODEL", "gemini-2.5-flash"),
            patch("gemini_agent.MODEL_MANAGER.fresh_results", return_value=[]),
            patch("gemini_agent.MODEL_MANAGER.results_are_fresh", return_value=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "No hay un modelo verificador"):
                _verification_models()


if __name__ == "__main__":
    unittest.main()
