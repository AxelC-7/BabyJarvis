"""Asistente visual de escritorio para Windows con Gemini."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import traceback as traceback_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from google import genai
from google.genai import types

from cursor_control import (
    click_cursor,
    double_click_cursor,
    get_cursor_position,
    mouse_button_down,
    mouse_button_up,
    move_cursor_smoothly,
    press_key,
    press_key_combo,
    scroll_cursor,
    type_text,
)
from desktop_geometry import ScreenGeometry, get_screen_geometry
from gemini_model_manager import GeminiModelManager
from screen_vision import (
    AgentDecision,
    CursorAction,
    ScreenFrame,
    capture_screen,
    screen_point_to_desktop,
    suggest_cursor_action,
)


BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "agent_debug.log"
FIXED_GEMINI_MODEL = "gemini-3.8-flash"
MODEL = FIXED_GEMINI_MODEL
TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "60"))
MAX_ACTIONS = int(os.getenv("MAX_SCREEN_ACTIONS", "8"))
MAX_TASK_DURATION_SECONDS = float(os.getenv("MAX_TASK_DURATION_SECONDS", "180"))
AUTHORIZATION_TIMEOUT_SECONDS = 30.0
MAX_DECISION_ROUNDS = 8

LOGGER = logging.getLogger("gemini_agent")
MODEL_MANAGER = GeminiModelManager()


def _redact_sensitive(text: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return re.sub(r"\bAIza[0-9A-Za-z_-]{20,}\b", "[REDACTED]", text)


def _log_exception(message: str, exception: BaseException) -> None:
    trace = "".join(
        traceback_module.format_exception(
            type(exception), exception, exception.__traceback__
        )
    )
    LOGGER.error("%s\n%s", message, _redact_sensitive(trace))


class TaskCancelled(RuntimeError):
    """Raised when a user cancels a running desktop task."""


class TaskTimeout(RuntimeError):
    """Raised when a task reaches its global time budget."""


class GeminiFatalError(RuntimeError):
    """Raised when Gemini or its response fails; the agent must stop."""


@dataclass
class TaskContext:
    deadline: float
    geometry: ScreenGeometry
    cancelled: threading.Event
    paused: threading.Event = field(default_factory=threading.Event)
    activity: Callable[[str], None] | None = None
    status: Callable[[str], None] | None = None
    capture: Callable[[], ScreenFrame] | None = None
    propose_batch: (
        Callable[[tuple[CursorAction, ...], ScreenFrame, bool, TaskContext], bool]
        | None
    ) = None
    cursor_attempted: bool = False
    win_r_attempted: bool = False
    shell_opened: bool = False
    shell_launcher_pending: bool = False


@dataclass
class TaskControl:
    cancelled: threading.Event = field(default_factory=threading.Event)
    paused: threading.Event = field(default_factory=threading.Event)
    activity: Callable[[str], None] | None = None
    status: Callable[[str], None] | None = None
    capture: Callable[[], ScreenFrame] | None = None
    propose_batch: (
        Callable[[tuple[CursorAction, ...], ScreenFrame, bool, TaskContext], bool]
        | None
    ) = None
    context: TaskContext | None = None


class _LogStream:
    """Redirige cada línea de consola al logger sin dejar de mostrarla."""

    def __init__(self, level: int) -> None:
        self.level = level
        self.buffer = ""

    @property
    def encoding(self) -> str:
        return "utf-8"

    def write(self, text: str) -> int:
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            LOGGER.log(self.level, "%s", _redact_sensitive(line))
        return len(text)

    def flush(self) -> None:
        if self.buffer:
            LOGGER.log(self.level, "%s", _redact_sensitive(self.buffer))
            self.buffer = ""

    def isatty(self) -> bool:
        return sys.__stdout__.isatty()

    def fileno(self) -> int:
        return sys.__stdout__.fileno()


def _handle_uncaught_exception(
    exception_type: type[BaseException],
    exception: BaseException,
    traceback: object,
) -> None:
    """Registra las excepciones no controladas junto con su traza."""
    trace = "".join(
        traceback_module.format_exception(exception_type, exception, traceback)
    )
    LOGGER.critical("Excepcion no controlada:\n%s", _redact_sensitive(trace))


def setup_logging() -> None:
    """Guarda la consola y los eventos con fecha en agent_debug.log."""
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.__stdout__)
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(console_handler)

    sys.stdout = _LogStream(logging.INFO)
    sys.stderr = _LogStream(logging.ERROR)
    sys.excepthook = _handle_uncaught_exception
    LOGGER.info("=== Inicio de sesión del asistente ===")
    LOGGER.info("Archivo de registro: %s", LOG_PATH)


def load_env_file(path: Path) -> None:
    """Carga pares KEY=VALUE sin reemplazar variables ya definidas."""
    if not path.is_file():
        return
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Línea {line_number} inválida en el archivo .env.")
        if key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def create_client() -> genai.Client:
    """Inicializa exclusivamente el modelo fijo Gemini 3.8 Flash."""
    global MODEL, TIMEOUT_SECONDS, MAX_ACTIONS, MAX_TASK_DURATION_SECONDS
    load_env_file(BASE_DIR / ".env")
    configured_model = os.getenv("GEMINI_MODEL")
    if configured_model and configured_model != FIXED_GEMINI_MODEL:
        LOGGER.warning(
            "Se ignora GEMINI_MODEL=%s; BabyJarvis usa exclusivamente %s.",
            configured_model,
            FIXED_GEMINI_MODEL,
        )
    MODEL = FIXED_GEMINI_MODEL
    TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "60"))
    MAX_ACTIONS = int(os.getenv("MAX_SCREEN_ACTIONS", "8"))
    MAX_TASK_DURATION_SECONDS = float(os.getenv("MAX_TASK_DURATION_SECONDS", "180"))
    if os.getenv("GEMINI_KEY_ROTATED", "").casefold() != "true":
        raise RuntimeError(
            "Gemini esta bloqueado hasta reemplazar la clave expuesta y "
            "configurar GEMINI_KEY_ROTATED=true."
        )
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Falta GEMINI_API_KEY. Configúrala en .env o como variable de entorno.")
    if not math.isfinite(TIMEOUT_SECONDS) or TIMEOUT_SECONDS <= 0:
        raise ValueError("GEMINI_TIMEOUT_SECONDS debe ser mayor que cero.")
    if not math.isfinite(MAX_TASK_DURATION_SECONDS) or MAX_TASK_DURATION_SECONDS <= TIMEOUT_SECONDS:
        raise ValueError("MAX_TASK_DURATION_SECONDS debe superar GEMINI_TIMEOUT_SECONDS.")
    if not 1 <= MAX_ACTIONS <= 60:
        raise ValueError("MAX_SCREEN_ACTIONS debe estar entre 1 y 60.")
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=int(TIMEOUT_SECONDS * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def set_primary_model(model: str) -> None:
    """Reject every runtime model change; the model ID is fixed in code."""
    global MODEL
    if model != FIXED_GEMINI_MODEL:
        raise ValueError(
            f"BabyJarvis usa exclusivamente {FIXED_GEMINI_MODEL}; no se permite cambiarlo."
        )
    MODEL = FIXED_GEMINI_MODEL


def _check_task(context: TaskContext, *, respect_pause: bool = True) -> None:
    while respect_pause and context.paused.is_set():
        if context.cancelled.is_set():
            raise TaskCancelled("La tarea fue cancelada.")
        if time.monotonic() >= context.deadline:
            raise TaskTimeout("Se agoto el tiempo maximo de la tarea.")
        time.sleep(0.05)
    if context.cancelled.is_set():
        raise TaskCancelled("La tarea fue cancelada.")
    if time.monotonic() >= context.deadline:
        raise TaskTimeout("Se agoto el tiempo maximo de la tarea.")
    cursor_x, cursor_y = get_cursor_position()
    if (cursor_x, cursor_y) == (context.geometry.left, context.geometry.top):
        context.cancelled.set()
        raise TaskCancelled("Cancelacion solicitada moviendo el cursor a la esquina superior izquierda.")


def _sleep_for_task(seconds: float, context: TaskContext) -> None:
    end = min(time.monotonic() + seconds, context.deadline)
    while time.monotonic() < end:
        _check_task(context)
        time.sleep(min(0.05, end - time.monotonic()))
    _check_task(context)


def _task_activity(context: TaskContext, message: str) -> None:
    if context.activity is not None:
        context.activity(message)
    else:
        print(message, flush=True)


def _task_status(context: TaskContext, status: str) -> None:
    if context.status is not None:
        context.status(status)


def _run_api_call(operation, context: TaskContext):
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put((True, operation()))
        except Exception as exc:
            result_queue.put((False, exc))

    _check_task(context)
    worker = threading.Thread(target=invoke, daemon=True, name="gemini-api-call")
    worker.start()
    while True:
        _check_task(context)
        try:
            succeeded, result = result_queue.get(timeout=0.05)
            break
        except queue.Empty:
            continue
    _check_task(context)
    if not succeeded:
        if isinstance(result, BaseException):
            raise result
        raise RuntimeError("La llamada a Gemini termino sin un resultado valido.")
    return result


def _generate_single_model(
    client: genai.Client,
    contents: list[object],
    config: types.GenerateContentConfig,
    context: TaskContext,
):
    """Make exactly one request to the fixed model; any failure is fatal."""
    model = FIXED_GEMINI_MODEL
    if context.activity is not None:
        context.activity(f"Usando exclusivamente Gemini {model}; sin fallback ni reintentos.")
    started_at = time.monotonic()
    LOGGER.info("Solicitud única a Gemini iniciada: modelo=%s", model)
    try:
        response = _run_api_call(
            lambda: client.models.generate_content(model=model, contents=contents, config=config),
            context,
        )
    except (TaskTimeout, TaskCancelled):
        raise
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        LOGGER.error(
            "Fallo definitivo de Gemini: modelo=%s tipo=%s duracion=%.2fs; no habrá reintento ni fallback.",
            model, type(exc).__name__, elapsed,
        )
        raise GeminiFatalError(
            f"Falló la única solicitud a {model} ({type(exc).__name__}). "
            "BabyJarvis se detendrá sin reintentar ni cambiar de modelo."
        ) from exc
    LOGGER.info("Solicitud única completada: modelo=%s duracion=%.2fs", model, time.monotonic() - started_at)
    return response


def _image_contents(prompt: str, frame: ScreenFrame) -> list[object]:
    image = types.Part.from_bytes(data=frame.png_bytes, mime_type="image/png")
    return [prompt, image]


def ask_gemini(
    client: genai.Client,
    task: str,
    frame: ScreenFrame,
    previous_actions: list[str],
    context: TaskContext,
) -> AgentDecision:
    def generate(contents: object) -> str:
        if not isinstance(contents, list) or len(contents) != 2:
            raise ValueError("El contrato visual envio un contenido invalido.")
        prompt_part, image_part = contents
        if (
            not isinstance(prompt_part, dict)
            or prompt_part.get("type") != "text"
            or not isinstance(prompt_part.get("text"), str)
            or not isinstance(image_part, dict)
            or image_part.get("type") != "image"
            or not isinstance(image_part.get("data"), str)
        ):
            raise ValueError("El contrato visual envio una imagen invalida.")
        image_bytes = base64.b64decode(image_part["data"], validate=True)
        response = _generate_single_model(
            client,
            [
                prompt_part["text"],
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=image_part.get("mime_type", "image/png"),
                ),
            ],
            types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=500,
            ),
            context,
        )
        response_text = (response.text or "").strip()
        _task_activity(
            context,
            "RESPUESTA COMPLETA DE GEMINI (salida recibida):\n"
            + _redact_sensitive(response_text),
        )
        return response_text

    try:
        return suggest_cursor_action(
            generate,
            FIXED_GEMINI_MODEL,
            task,
            frame,
            history=tuple(previous_actions),
            cursor_attempted=context.cursor_attempted,
            win_r_attempted=context.win_r_attempted,
            shell_opened=context.shell_opened,
        )
    except GeminiFatalError:
        raise
    except (ValueError, RuntimeError, TypeError) as exc:
        raise GeminiFatalError(
            f"Gemini devolvió una respuesta inválida o coordenadas fuera de la captura "
            f"({frame.width}x{frame.height}). Se descarta la respuesta y se detiene todo: {exc}"
        ) from exc


def _redacted_actions(actions: list[CursorAction]) -> list[dict[str, object]]:
    records = []
    for action in actions:
        record: dict[str, object] = {"type": action.action}
        if action.action == "write":
            record["characters"] = len(action.text)
        elif action.action == "press":
            record["keys"] = action.keys
        elif action.action in {"move", "click", "double_click", "scroll"}:
            record.update({"x": action.x, "y": action.y})
            if action.action == "click":
                record["button"] = action.button
            if action.action == "scroll":
                record["delta"] = action.scroll_delta
        elif action.action == "drag":
            record.update({"start_x": action.start_x, "start_y": action.start_y,
                           "end_x": action.end_x, "end_y": action.end_y})
        elif action.action == "wait":
            record["seconds"] = action.duration
        records.append(record)
    return records


def _needs_authorization(actions: list[CursorAction]) -> bool:
    return any(
        action.action in {"click", "double_click", "drag", "write", "press", "scroll"}
        for action in actions
    )


def _authorization_summary(actions: list[CursorAction], frame: ScreenFrame) -> str:
    lines = ["Lote validado completo; no se ejecutara sin aprobacion:"]
    for index, action in enumerate(actions, 1):
        if action.action in {"move", "click", "double_click", "scroll"}:
            desktop_x, desktop_y = screen_point_to_desktop(frame, action.x, action.y)
            line = f"{index}. {action.action}: imagen ({action.x}, {action.y}) -> escritorio ({desktop_x}, {desktop_y})"
            if action.action == "click":
                line += f", boton {action.button}"
            if action.action == "scroll":
                line += f", delta {action.scroll_delta}"
        elif action.action == "drag":
            start = screen_point_to_desktop(frame, action.start_x, action.start_y)
            end = screen_point_to_desktop(frame, action.end_x, action.end_y)
            line = f"{index}. arrastrar: {start} -> {end}"
        elif action.action == "write":
            line = f"{index}. escribir {json.dumps(action.text, ensure_ascii=False)}"
        elif action.action == "press":
            line = f"{index}. teclas: {'+'.join(action.keys)}"
        elif action.action == "wait":
            line = f"{index}. esperar {action.duration} s"
        else:
            line = f"{index}. {action.action}"
        lines.append(line)
    return "\n".join(lines)


def _request_authorization(
    actions: list[CursorAction], frame: ScreenFrame, context: TaskContext
) -> bool:
    if not sys.stdin.isatty() or os.name != "nt":
        LOGGER.warning("Lote rechazado: la consola no permite confirmacion interactiva.")
        print("[Seguridad] No hay una consola interactiva; el lote se descarto.")
        return False
    import msvcrt

    summary = _authorization_summary(actions, frame)
    sys.__stdout__.write(summary + "\n")
    sys.__stdout__.flush()
    LOGGER.info(
        "Se solicito autorizacion para un lote: %s",
        json.dumps(_redacted_actions(actions), ensure_ascii=False),
    )
    print("Autorizar el lote completo? Escribe S o N y presiona Enter (30 s): ", end="", flush=True)
    answer = ""
    expires = min(time.monotonic() + AUTHORIZATION_TIMEOUT_SECONDS, context.deadline)
    while time.monotonic() < expires:
        _check_task(context)
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in {"\r", "\n"}:
                print()
                return answer.casefold() in {"s", "si", "y", "yes"}
            if char == "\x1b":
                print()
                return False
            if char == "\x03":
                context.cancelled.set()
                raise TaskCancelled("Cancelacion solicitada desde el teclado.")
            if char == "\b":
                answer = answer[:-1]
            elif char.isprintable() and len(answer) < 8:
                answer += char
                sys.__stdout__.write(char)
                sys.__stdout__.flush()
        else:
            time.sleep(0.05)
    print()
    LOGGER.warning("Lote rechazado por falta de confirmacion dentro del plazo.")
    print("[Seguridad] No se recibio autorizacion; se descarto el lote.")
    return False


def _update_click_state(
    actions: list[CursorAction],
    last_point: tuple[int, int] | None,
    count: int,
) -> tuple[tuple[int, int] | None, int]:
    for action in actions:
        if action.action == "click":
            point = (action.x, action.y)
            count = count + 1 if point == last_point else 1
            last_point = point
        else:
            last_point, count = None, 0
    return last_point, count


def _repeated_click_point(
    actions: list[CursorAction],
    last_point: tuple[int, int] | None,
    count: int,
) -> tuple[int, int] | None:
    for action in actions:
        if action.action == "click":
            point = (action.x, action.y)
            count = count + 1 if point == last_point else 1
            last_point = point
            if count > 2:
                return point
        else:
            last_point, count = None, 0
    return None


def _is_windows_run_shortcut(action: CursorAction) -> bool:
    return (
        action.action == "press"
        and len(action.keys) == 2
        and {key.casefold() for key in action.keys} == {"windows", "r"}
    )


def _validate_strategy_actions(actions: list[CursorAction], context: TaskContext) -> None:
    """Enforce the cursor -> Win+R -> CMD/PowerShell order in code."""
    if not context.cursor_attempted:
        if not actions or actions[0].action not in {"click", "double_click"}:
            raise GeminiFatalError(
                "La primera acción debe ser un clic o doble clic visible con el cursor."
            )
        if any(
            action.action == "press"
            and any(key.casefold() != "enter" for key in action.keys)
            for action in actions[1:]
        ):
            raise GeminiFatalError(
                "No se permiten atajos de teclado en el primer lote; primero debe observarse el clic."
            )
    for action in actions:
        if _is_windows_run_shortcut(action) and not context.cursor_attempted:
            raise GeminiFatalError("Win+R fue propuesto antes del intento obligatorio con el cursor.")
        if action.action != "write":
            continue
        text = action.text.strip()
        lowered = text.casefold()
        is_shell_launcher = lowered in {"cmd", "powershell", "pwsh"}
        is_shell_command = bool(re.match(r"(?i)^(?:start|start-process)\b", text))
        if is_shell_launcher and not context.win_r_attempted:
            raise GeminiFatalError("CMD/PowerShell fue propuesto antes de intentar Win+R.")
        if is_shell_command:
            if not context.shell_opened:
                raise GeminiFatalError("Se rechazó un comando porque CMD/PowerShell no fue abierto mediante Win+R.")
            if not re.fullmatch(r"(?i)(?:start|start-process)\s+[A-Za-z0-9_.-]+", text):
                raise GeminiFatalError("Solo se permiten comandos simples start <aplicacion> o Start-Process <aplicacion>.")
        elif context.shell_opened:
            raise GeminiFatalError("Con CMD/PowerShell abierto solo se permite un comando simple de lanzamiento.")
        elif context.win_r_attempted and not is_shell_launcher:
            if not re.fullmatch(r"[A-Za-z0-9_.:/\\-]+", text):
                raise GeminiFatalError("En Ejecutar solo se permite un nombre o ruta simple de aplicación.")


def _record_strategy_progress(action: CursorAction, context: TaskContext) -> None:
    """Advance strategy flags only after the corresponding input was executed."""
    if action.action in {"click", "double_click"}:
        context.cursor_attempted = True
    if _is_windows_run_shortcut(action):
        context.win_r_attempted = True
    if action.action == "write" and context.win_r_attempted and action.text.strip().casefold() in {"cmd", "powershell", "pwsh"}:
        context.shell_launcher_pending = True
    if action.action == "press" and "enter" in {key.casefold() for key in action.keys} and context.shell_launcher_pending:
        context.shell_opened = True
        context.shell_launcher_pending = False


def _execute_action(action: CursorAction, frame: ScreenFrame, context: TaskContext) -> str:
    _check_task(context)
    if action.action == "move":
        x, y = screen_point_to_desktop(frame, action.x, action.y)
        move_cursor_smoothly(x, y, duration=action.duration, cancel_check=lambda: _check_task(context))
        return f"Cursor movido a ({x}, {y})."
    if action.action in {"click", "double_click"}:
        x, y = screen_point_to_desktop(frame, action.x, action.y)
        move_cursor_smoothly(x, y, duration=0.15, cancel_check=lambda: _check_task(context))
        _check_task(context)
        if action.action == "click":
            click_cursor(action.button)
            return f"Clic {action.button} en ({x}, {y})."
        double_click_cursor()
        return f"Doble clic en ({x}, {y})."
    if action.action == "drag":
        start = screen_point_to_desktop(frame, action.start_x, action.start_y)
        end = screen_point_to_desktop(frame, action.end_x, action.end_y)
        move_cursor_smoothly(*start, duration=0.15, cancel_check=lambda: _check_task(context))
        _check_task(context)
        mouse_button_down("left")
        try:
            move_cursor_smoothly(*end, duration=action.duration, cancel_check=lambda: _check_task(context))
        finally:
            mouse_button_up("left")
        return f"Arrastre desde {start} hasta {end}."
    if action.action == "write":
        type_text(action.text, cancel_check=lambda: _check_task(context))
        return f"Texto escrito ({len(action.text)} caracteres)."
    if action.action == "press":
        if len(action.keys) == 1:
            press_key(action.keys[0])
        else:
            press_key_combo(action.keys)
        return f"Teclas presionadas: {'+'.join(action.keys)}."
    if action.action == "scroll":
        x, y = screen_point_to_desktop(frame, action.x, action.y)
        move_cursor_smoothly(x, y, duration=0.1, cancel_check=lambda: _check_task(context))
        _check_task(context)
        scroll_cursor(action.scroll_delta)
        return f"Desplazamiento aplicado en ({x}, {y})."
    if action.action == "wait":
        _sleep_for_task(action.duration, context)
        return f"Espera de {action.duration} s completada."
    raise ValueError(f"Accion no permitida: {action.action!r}.")


def _verify_task(client: genai.Client, task: str, context: TaskContext) -> tuple[bool, str]:
    """Verify completion with the same fixed model; no secondary model is used."""
    frame = context.capture() if context.capture is not None else capture_screen()
    context.geometry = frame.geometry
    width, height = frame.width, frame.height
    prompt = f"""Evalua exclusivamente la captura actual frente al objetivo del usuario.
Devuelve JSON exacto: {{"completed":true|false,"evidence":"breve evidencia visible"}}.
No des por terminado el objetivo por afirmaciones anteriores del asistente; verifica evidencia
visible en esta imagen. Si no puedes confirmarlo inequívocamente, completed debe ser false.
La imagen conserva proporcion y mide {width}x{height}.
Objetivo del usuario: {json.dumps(task, ensure_ascii=False)}"""
    response = _generate_single_model(
        client,
        _image_contents(prompt, frame),
        types.GenerateContentConfig(response_mime_type="application/json", temperature=0, max_output_tokens=200),
        context,
    )
    try:
        result = json.loads((response.text or "").strip())
    except json.JSONDecodeError as exc:
        raise GeminiFatalError(
            f"La verificación de {FIXED_GEMINI_MODEL} devolvió JSON inválido; se detiene todo."
        ) from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"completed", "evidence"}
        or not isinstance(result["completed"], bool)
        or not isinstance(result["evidence"], str)
    ):
        raise GeminiFatalError(
            f"La verificación de {FIXED_GEMINI_MODEL} incumplió el contrato; se detiene todo."
        )
    evidence = result["evidence"].strip()
    completed = result["completed"] and bool(evidence)
    if result["completed"] and not evidence:
        LOGGER.warning("El verificador no devolvio evidencia; no se acepta como exito.")
    LOGGER.info("Verificacion visual con el modelo unico (%s): completed=%s", FIXED_GEMINI_MODEL, completed)
    return completed, evidence


def run_task(
    client: genai.Client,
    task: str,
    *,
    control: TaskControl | None = None,
) -> bool:
    """Ejecuta decisiones cortas, validadas y autorizadas sobre capturas reales."""
    task_control = control or TaskControl()
    context = TaskContext(
        deadline=time.monotonic() + MAX_TASK_DURATION_SECONDS,
        geometry=get_screen_geometry(),
        cancelled=task_control.cancelled,
        paused=task_control.paused,
        activity=task_control.activity,
        status=task_control.status,
        capture=task_control.capture,
        propose_batch=task_control.propose_batch,
    )
    task_control.context = context
    LOGGER.info("Inicio de tarea; se omite el texto de la instruccion del registro.")
    actions_done = 0
    decision_rounds = 0
    history: list[str] = []
    last_click_point: tuple[int, int] | None = None
    consecutive_clicks_at_point = 0
    point_reanalyzed: tuple[int, int] | None = None

    while decision_rounds < MAX_DECISION_ROUNDS:
        _check_task(context)
        decision_rounds += 1
        _task_status(context, "thinking")
        _task_activity(context, "Capturando pantalla y consultando a Gemini...")
        frame = context.capture() if context.capture is not None else capture_screen()
        context.geometry = frame.geometry
        LOGGER.info(
            "Captura: imagen=%sx%s escritorio=%sx%s origen=(%s,%s) DPI=%s/%s",
            frame.width, frame.height,
            frame.geometry.width, frame.geometry.height,
            frame.geometry.left, frame.geometry.top,
            frame.geometry.dpi_awareness, frame.geometry.system_dpi,
        )
        decision = ask_gemini(client, task, frame, history, context)
        actions = decision.actions
        _task_activity(
            context,
            f"DECISIÓN INTERPRETADA DE GEMINI: estado={decision.status}; "
            f"mensaje={_redact_sensitive(decision.message or '(sin mensaje)')}; "
            f"acciones={json.dumps(_redacted_actions(list(actions)), ensure_ascii=False)}",
        )
        LOGGER.info(
            "Estado propuesto por Gemini: %s; acciones: %s",
            decision.status,
            json.dumps(_redacted_actions(list(actions)), ensure_ascii=False),
        )
        if decision.status == "error":
            LOGGER.warning("Gemini informo un obstaculo; se evaluara la siguiente etapa permitida: %s", _redact_sensitive(decision.message))
            if context.shell_opened:
                raise GeminiFatalError("CMD/PowerShell no resolvió la tarea; no existe una acción nativa validada. Se detiene todo.")
            if not context.cursor_attempted:
                raise GeminiFatalError("Gemini no pudo identificar un control visible para el primer intento con el cursor.")
            history.append(
                "Obstáculo observado: "
                f"{_redact_sensitive(decision.message)}. Cambia únicamente a la siguiente etapa: Win+R después del clic; luego CMD/PowerShell si Win+R falla."
            )
            _task_activity(context, "Obstáculo detectado. Se permite avanzar solo a la siguiente estrategia validada.")
            continue
        if decision.status == "success":
            _task_status(context, "thinking")
            _task_activity(context, "Comprobando el resultado con una captura nueva...")
            completed, evidence = _verify_task(client, task, context)
            if completed:
                LOGGER.info("Exito confirmado mediante verificacion visual independiente.")
                _task_activity(context, f"Resultado verificado visualmente: {evidence}")
                return True
            history.append(
                "Gemini informo success, pero la verificacion visual independiente "
                "no confirmo el objetivo. El método anterior no consiguió el objetivo. "
                "Reobserva la pantalla y cambia de estrategia: primero cursor si aún no "
                "se probó; después Win+R; luego CMD/PowerShell con lanzamiento seguro; "
                "por último API nativa de Windows. No repitas el mismo clic sin cambios."
            )
            LOGGER.warning(
                "El estado success de Gemini no fue confirmado; se reanalizara la pantalla."
            )
            continue
        if decision.status != "continue" or not actions:
            raise GeminiFatalError("Gemini devolvio una decision continue sin acciones; se detiene todo.")
        _validate_strategy_actions(list(actions), context)
        if len(actions) > MAX_ACTIONS - actions_done:
            LOGGER.warning("Lote completo rechazado: supera el limite global de acciones.")
            _task_activity(context, "El lote supera el límite de acciones restante y se descartó.")
            return False

        repeated = _repeated_click_point(actions, last_click_point, consecutive_clicks_at_point)
        if repeated is not None:
            if point_reanalyzed == repeated:
                LOGGER.warning("Tarea detenida tras repetir el pixel (%s,%s) despues de reanalizar.", *repeated)
                _task_activity(context, "La tarea se detuvo tras repetir el mismo píxel después de reanalizar.")
                raise GeminiFatalError(
                    f"Se repitió el mismo clic ({repeated[0]},{repeated[1]}) sin progreso; se detiene todo."
                )
            point_reanalyzed = repeated
            history.append("El lote completo se descarto por proponer mas de dos clics consecutivos en el mismo pixel. Reanaliza la nueva captura y elige una accion distinta.")
            LOGGER.warning("Lote completo descartado por clics repetidos en (%s,%s).", *repeated)
            _task_activity(context, "Lote descartado por clics repetidos; se volverá a observar la pantalla.")
            continue

        authorization_required = _needs_authorization(actions)
        if context.propose_batch is not None:
            _task_status(context, "proposed")
            authorized = context.propose_batch(
                tuple(actions), frame, authorization_required, context
            )
            if not authorized:
                LOGGER.info("Lote descartado: autorizacion denegada o ausente.")
                return False
        elif authorization_required:
            LOGGER.info(
                "Modo autónomo sin interfaz de servicio: lote validado para ejecución automática."
            )
            _task_activity(
                context,
                "Modo autónomo activo: no se solicita aprobación manual. "
                "La cancelación y la parada de emergencia siguen disponibles.",
            )
        _check_task(context)

        _task_status(context, "executing")
        for action in actions:
            _check_task(context)
            result = _execute_action(action, frame, context)
            _record_strategy_progress(action, context)
            last_click_point, consecutive_clicks_at_point = _update_click_state(
                [action], last_click_point, consecutive_clicks_at_point
            )
            if point_reanalyzed is not None and action.action != "click":
                point_reanalyzed = None
            elif action.action == "click" and (action.x, action.y) != point_reanalyzed:
                point_reanalyzed = None
            actions_done += 1
            history.append(result)
            LOGGER.info("Accion ejecutada: tipo=%s; detalle=%s", action.action, _redacted_actions([action])[0])
            _task_activity(context, f"Acción {actions_done}/{MAX_ACTIONS}: {result}")
            if action.action == "press":
                pressed = {key.casefold() for key in action.keys}
                windows_keys = {"win", "windows", "super", "meta", "leftwin", "rightwin"}
                if pressed.intersection(windows_keys) and "s" in pressed:
                    _task_activity(context, "Esperando a que Windows Search termine de abrirse...")
                    _sleep_for_task(2.0, context)
            _sleep_for_task(0.15, context)

    LOGGER.error("Tarea detenida al alcanzar %s rondas de decision.", MAX_DECISION_ROUNDS)
    raise GeminiFatalError(f"Se alcanzó el límite de {MAX_DECISION_ROUNDS} decisiones; se detiene todo sin declarar éxito.")


def main() -> None:
    """Bucle principal de conversación."""
    setup_logging()
    LOGGER.info("Inicializando cliente Gemini.")
    try:
        client = create_client()
    except Exception as exc:
        _log_exception("No se pudo inicializar el asistente.", exc)
        print(f"No se pudo iniciar el asistente: {_redact_sensitive(str(exc))}")
        return

    LOGGER.info("Cliente Gemini listo; modelo configurado: %s", MODEL)
    print(f"Asistente visual listo (modelo: {MODEL}).")
    print("Las capturas de pantalla se envían a Gemini para analizar la interfaz.")
    print("Escribe una instrucción o 'salir'. Mueve el cursor a la esquina superior izquierda para abortar.")
    while True:
        try:
            task = input("\nTú: ").strip()
            if task.casefold() in {"salir", "exit", "quit"}:
                print("Asistente cerrado.")
                LOGGER.info("Cierre solicitado por el usuario.")
                return
            if not task:
                continue
            LOGGER.info("Instruccion recibida; contenido omitido del registro.")
            run_task(client, task)
        except TaskCancelled as exc:
            LOGGER.warning("Tarea cancelada: %s", _redact_sensitive(str(exc)))
            print(f"\nTarea cancelada: {_redact_sensitive(str(exc))}")
        except TaskTimeout as exc:
            LOGGER.warning("Tarea agotó su tiempo máximo: %s", _redact_sensitive(str(exc)))
            print(f"\nTarea detenida por tiempo límite: {_redact_sensitive(str(exc))}")
        except KeyboardInterrupt:
            print("\nAsistente cerrado.")
            LOGGER.info("Cierre solicitado con Ctrl+C.")
            return
        except Exception as exc:
            _log_exception("Error durante la tarea.", exc)
            print(f"No se pudo completar la instruccion: {_redact_sensitive(str(exc))}")


if __name__ == "__main__":
    main()
