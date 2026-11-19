"""Tests for deterministic desktop strategy enforcement."""

import time
from threading import Event

import pytest

from desktop_geometry import ScreenGeometry
from gemini_agent import (
    FIXED_GEMINI_MODEL,
    GeminiFatalError,
    TaskContext,
    _generate_single_model,
    _record_strategy_progress,
    _validate_strategy_actions,
)
from screen_vision import CursorAction, ScreenFrame, _validated_point


def make_context() -> TaskContext:
    return TaskContext(
        deadline=time.monotonic() + 60,
        geometry=ScreenGeometry(0, 0, 1366, 768),
        cancelled=Event(),
    )


def test_runtime_model_is_fixed_to_gemini_35_flash() -> None:
    assert FIXED_GEMINI_MODEL == "gemini-3.5-flash"


def test_first_strategy_requires_a_real_cursor_click() -> None:
    context = make_context()
    with pytest.raises(GeminiFatalError, match="clic visible"):
        _validate_strategy_actions(
            [CursorAction("press", keys=("WINDOWS", "R"))],
            context,
        )


def test_first_cursor_batch_may_move_before_click() -> None:
    context = make_context()
    _validate_strategy_actions(
        [
            CursorAction("move", x=10, y=10),
            CursorAction("click", x=20, y=20),
        ],
        context,
    )


def test_first_batch_cannot_use_windows_shortcuts_after_a_click_without_new_capture() -> None:
    context = make_context()
    actions = [
        CursorAction("click", x=20, y=20),
        CursorAction("press", keys=("WINDOWS", "S")),
    ]
    with pytest.raises(GeminiFatalError, match="No se permiten atajos"):
        _validate_strategy_actions(actions, context)


def test_win_r_is_allowed_only_after_cursor_click_was_executed() -> None:
    context = make_context()
    context.cursor_attempted = True
    _validate_strategy_actions(
        [CursorAction("press", keys=("WINDOWS", "R"))],
        context,
    )


def test_cursor_attempt_is_recorded_only_after_click_execution() -> None:
    context = make_context()
    _record_strategy_progress(CursorAction("move", x=20, y=20), context)
    assert not context.cursor_attempted
    _record_strategy_progress(CursorAction("click", x=20, y=20), context)
    assert context.cursor_attempted


def test_console_cannot_be_launched_before_win_r() -> None:
    context = make_context()
    context.cursor_attempted = True
    with pytest.raises(GeminiFatalError, match="antes de intentar Win"):
        _validate_strategy_actions([CursorAction("write", text="cmd")], context)


def test_shell_command_requires_console_opened_through_run_dialog() -> None:
    context = make_context()
    context.cursor_attempted = True
    context.win_r_attempted = True
    with pytest.raises(GeminiFatalError, match="no fue abierto"):
        _validate_strategy_actions(
            [CursorAction("write", text="start firefox")],
            context,
        )


def test_only_simple_application_launch_commands_are_allowed_in_shell() -> None:
    context = make_context()
    context.cursor_attempted = True
    context.win_r_attempted = True
    context.shell_opened = True
    _validate_strategy_actions(
        [CursorAction("write", text="start firefox")],
        context,
    )
    with pytest.raises(GeminiFatalError, match="comandos simples de lanzamiento"):
        _validate_strategy_actions(
            [CursorAction("write", text="Start-Process firefox; Remove-Item *")],
            context,
        )


def test_shell_opens_only_after_cmd_and_enter_are_executed() -> None:
    context = make_context()
    context.cursor_attempted = True
    context.win_r_attempted = True
    _record_strategy_progress(CursorAction("write", text="cmd"), context)
    assert context.shell_launcher_pending
    assert not context.shell_opened
    _record_strategy_progress(CursorAction("press", keys=("ENTER",)), context)
    assert context.shell_opened
    assert not context.shell_launcher_pending



def test_single_model_request_uses_fixed_gemini_and_never_retries(monkeypatch) -> None:
    from types import SimpleNamespace

    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("simulated API failure")

    monkeypatch.setattr("gemini_agent._run_api_call", lambda operation, context: operation())
    context = make_context()
    client = SimpleNamespace(models=FakeModels())
    from google.genai import types

    with pytest.raises(GeminiFatalError, match="sin reintentar"):
        _generate_single_model(
            client,
            ["prompt"],
            types.GenerateContentConfig(response_mime_type="application/json"),
            context,
        )

    assert len(calls) == 1
    assert calls[0]["model"] == FIXED_GEMINI_MODEL


def test_fatal_task_error_stops_service_and_rejects_new_tasks() -> None:
    import time

    from agent_service import AgentService

    def failing_runner(_client, _instruction, *, control=None):
        raise RuntimeError("simulated fatal model failure")

    service = AgentService(
        client_factory=lambda: object(),
        task_runner=failing_runner,
    )
    service.start()
    service.submit_task("prueba controlada")
    deadline = time.monotonic() + 3
    while service.worker_alive and time.monotonic() < deadline:
        time.sleep(0.01)

    assert not service.worker_alive
    with pytest.raises(RuntimeError, match="detenido"):
        service.submit_task("no debe ejecutarse")


def test_out_of_bounds_coordinate_error_includes_capture_dimensions() -> None:
    frame = ScreenFrame(
        png_bytes=b"",
        width=1366,
        height=768,
        geometry=ScreenGeometry(0, 0, 1366, 768),
        captured_at=time.time(),
    )
    with pytest.raises(ValueError, match="1366x768"):
        _validated_point(
            {"x": 68, "y": 974},
            {"x", "y"},
            {"x", "y"},
            frame,
        )


