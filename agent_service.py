"""Serialized background service for Jarvis desktop tasks."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from gemini_agent import (
    MAX_TASK_DURATION_SECONDS,
    FIXED_GEMINI_MODEL,
    TaskCancelled,
    TaskContext,
    TaskControl,
    MODEL_MANAGER,
    _authorization_summary,
    _redacted_actions,
    _redact_sensitive,
    create_client,
    run_task,
    set_primary_model,
)
from gemini_model_manager import ModelDiagnosticResult
from screen_vision import CursorAction, ScreenFrame, capture_screen


class AgentState(str, Enum):
    INITIALIZING = "initializing"
    IDLE = "idle"
    QUEUED = "queued"
    THINKING = "thinking"
    PROPOSED = "proposed"
    WAITING_AUTHORIZATION = "waiting_authorization"
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass(frozen=True)
class StatusEvent:
    state: AgentState
    task_id: str | None = None


@dataclass(frozen=True)
class ActivityEvent:
    message: str
    task_id: str | None = None


@dataclass(frozen=True)
class ActionProposalEvent:
    task_id: str
    batch_id: str
    actions: tuple[CursorAction, ...]
    summary: str
    requires_authorization: bool
    expires_at: float | None


@dataclass(frozen=True)
class ScreenshotEvent:
    task_id: str
    frame: ScreenFrame


@dataclass(frozen=True)
class CaptureRequestEvent:
    request_id: str
    reply: queue.Queue[ScreenFrame | BaseException]


@dataclass(frozen=True)
class ErrorEvent:
    message: str
    task_id: str | None = None


@dataclass(frozen=True)
class TaskFinishedEvent:
    task_id: str
    success: bool


@dataclass(frozen=True)
class ModelProgressEvent:
    operation: str
    completed: int
    total: int | None
    model: str


@dataclass(frozen=True)
class ModelsDiscoveredEvent:
    models: tuple[str, ...]


@dataclass(frozen=True)
class ModelDiagnosticsCompletedEvent:
    results: tuple[ModelDiagnosticResult, ...]
    cancelled: bool


@dataclass(frozen=True)
class PrimaryModelSelectedEvent:
    model: str


AgentEvent = (
    StatusEvent
    | ActivityEvent
    | ActionProposalEvent
    | ScreenshotEvent
    | CaptureRequestEvent
    | ErrorEvent
    | TaskFinishedEvent
    | ModelProgressEvent
    | ModelsDiscoveredEvent
    | ModelDiagnosticsCompletedEvent
    | PrimaryModelSelectedEvent
)


@dataclass(frozen=True)
class _Task:
    task_id: str
    instruction: str


@dataclass(frozen=True)
class _AuthorizationResponse:
    batch_id: str
    actions: tuple[CursorAction, ...]
    approved: bool


@dataclass(frozen=True)
class _ModelJob:
    operation: str
    cancelled: threading.Event
    models: tuple[str, ...] = ()


CAPTURE_ARCHIVE_DIR = Path(__file__).resolve().parent / "debug_captures"
LOGGER = logging.getLogger("agent_service")


class AgentService:
    """Owns the sole worker that may control the desktop."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], object] = create_client,
        task_runner: Callable[..., bool] = run_task,
        task_duration: float = MAX_TASK_DURATION_SECONDS,
        model_manager=MODEL_MANAGER,
    ) -> None:
        self.events: queue.Queue[AgentEvent] = queue.Queue()
        self._tasks: queue.Queue[_Task | _ModelJob] = queue.Queue()
        self._authorization_responses: queue.Queue[_AuthorizationResponse] = queue.Queue()
        self._client_factory = client_factory
        self._task_runner = task_runner
        self._task_duration = task_duration
        self._model_manager = model_manager
        self._shutdown = threading.Event()
        self._lock = threading.Lock()
        self._active_task: _Task | None = None
        self._active_control: TaskControl | None = None
        self._active_batch_id: str | None = None
        self._worker_state = AgentState.THINKING
        self._resume_state = AgentState.THINKING
        self._worker: threading.Thread | None = None
        self._startup_error: str | None = None
        self._model_job_cancelled: threading.Event | None = None
        self._model_job_queued = False
        self._capture_sequence = 0

    @property
    def worker_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._worker is not None:
                return
            self.events.put(StatusEvent(AgentState.INITIALIZING))
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="jarvis-agent-worker",
                daemon=True,
            )
            self._worker.start()

    def submit_task(self, instruction: str) -> str:
        normalized = instruction.strip()
        if not normalized:
            raise ValueError("La instrucción no puede estar vacía.")
        if self._shutdown.is_set():
            raise RuntimeError("El servicio está detenido.")
        if self._startup_error is not None:
            raise RuntimeError("El agente no pudo inicializarse.")
        task = _Task(uuid.uuid4().hex, normalized)
        self._tasks.put(task)
        self.events.put(StatusEvent(AgentState.QUEUED, task.task_id))
        self.events.put(ActivityEvent("Instrucción agregada a la cola.", task.task_id))
        return task.task_id

    def discover_models(self) -> None:
        """Queue a read-only discovery of models exposed by the configured Gemini API key."""
        self._queue_model_job("discover")

    def test_model_compatibility(self) -> None:
        raise RuntimeError(
            f"El modelo está fijado en {FIXED_GEMINI_MODEL}; no se ejecutan pruebas sobre otros modelos."
        )

    def cancel_model_tests(self) -> bool:
        with self._lock:
            event = self._model_job_cancelled
        if event is None or event.is_set():
            return False
        event.set()
        return True

    def select_primary_model(self, model: str) -> None:
        with self._lock:
            if (
                self._active_task is not None
                or self._active_control is not None
                or self._model_job_queued
                or not self._tasks.empty()
            ):
                raise RuntimeError(
                    "No se puede cambiar el modelo mientras hay tareas o diagnósticos pendientes."
                )
        if model != FIXED_GEMINI_MODEL:
            raise ValueError(f"BabyJarvis usa exclusivamente {FIXED_GEMINI_MODEL}; no se permite cambiarlo.")
        set_primary_model(FIXED_GEMINI_MODEL)
        self.events.put(PrimaryModelSelectedEvent(FIXED_GEMINI_MODEL))

    def model_report(self) -> dict[str, object] | None:
        report = self._model_manager.fresh_report()
        if report is None:
            return None
        report["discovered_models"] = self._model_manager.fresh_discovered_models()
        if not self._model_manager.results_are_fresh():
            report["results"] = []
            report["selected_primary"] = None
        return report

    @property
    def selected_model(self) -> str:
        return FIXED_GEMINI_MODEL

    def _queue_model_job(self, operation: str, models: list[str] | None = None) -> None:
        with self._lock:
            if self._shutdown.is_set():
                raise RuntimeError("El servicio está detenido.")
            if self._startup_error is not None:
                raise RuntimeError("Gemini no pudo inicializarse.")
            if self._model_job_queued:
                raise RuntimeError("Ya hay un diagnóstico de modelos en curso o en cola.")
            cancelled = threading.Event()
            self._model_job_cancelled = cancelled
            self._model_job_queued = True
            self._tasks.put(_ModelJob(operation, cancelled, tuple(models or ())))
        self.events.put(
            ActivityEvent(
                "Búsqueda de modelos agregada a la cola."
                if operation == "discover"
                else "Prueba de compatibilidad agregada a la cola."
            )
        )

    def authorize(
        self, batch_id: str, actions: tuple[CursorAction, ...], approved: bool
    ) -> None:
        self._authorization_responses.put(
            _AuthorizationResponse(batch_id, actions, approved)
        )

    def pause(self) -> bool:
        with self._lock:
            control = self._active_control
            task = self._active_task
            if control is None or task is None or control.cancelled.is_set():
                return False
            self._resume_state = self._worker_state
            control.paused.set()
            self._worker_state = AgentState.PAUSED
        self.events.put(StatusEvent(AgentState.PAUSED, task.task_id))
        self.events.put(ActivityEvent("Tarea pausada.", task.task_id))
        return True

    def resume(self) -> bool:
        with self._lock:
            control = self._active_control
            task = self._active_task
            if control is None or task is None or not control.paused.is_set():
                return False
            control.paused.clear()
            self._worker_state = self._resume_state
            resumed_state = self._worker_state
        self.events.put(StatusEvent(resumed_state, task.task_id))
        self.events.put(ActivityEvent("Tarea reanudada.", task.task_id))
        return True

    def emergency_stop(self) -> None:
        with self._lock:
            task = self._active_task
            control = self._active_control
            if control is not None:
                control.cancelled.set()
                control.paused.clear()
            diagnostic_cancel = self._model_job_cancelled
            if diagnostic_cancel is not None:
                diagnostic_cancel.set()
            self._active_batch_id = None
        discarded = self._discard_queued_tasks()
        if task is not None:
            self.events.put(StatusEvent(AgentState.STOPPED, task.task_id))
            self.events.put(ActivityEvent("Parada de emergencia solicitada.", task.task_id))
        for queued in discarded:
            if isinstance(queued, _ModelJob):
                queued.cancelled.set()
                with self._lock:
                    if self._model_job_cancelled is queued.cancelled:
                        self._model_job_cancelled = None
                        self._model_job_queued = False
                self.events.put(ModelDiagnosticsCompletedEvent((), True))
                continue
            self.events.put(StatusEvent(AgentState.STOPPED, queued.task_id))
            self.events.put(ActivityEvent("Instrucción descartada por parada de emergencia.", queued.task_id))
        self._discard_authorization_responses()

    def shutdown(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        self.emergency_stop()
        self._shutdown.set()
        if wait and self._worker is not None:
            self._worker.join(timeout)

    def _discard_queued_tasks(self) -> list[_Task | _ModelJob]:
        discarded: list[_Task | _ModelJob] = []
        while True:
            try:
                discarded.append(self._tasks.get_nowait())
            except queue.Empty:
                return discarded

    def _discard_authorization_responses(self) -> None:
        while True:
            try:
                self._authorization_responses.get_nowait()
            except queue.Empty:
                return

    def _archive_trace_event(self, task_id: str, event: str, details: dict[str, object]) -> None:
        """Append a privacy-conscious, per-task timeline for post-run debugging."""
        try:
            task_dir = CAPTURE_ARCHIVE_DIR / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now().astimezone().isoformat(),
                "task_id": task_id,
                "event": event,
                "details": details,
            }
            with (task_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            LOGGER.warning("No se pudo archivar el evento de tarea %s: %s", task_id[:8], exc)

    def _emit_activity(self, task_id: str, message: str) -> None:
        safe_message = _redact_sensitive(message)
        self._archive_trace_event(task_id, "activity", {"message": safe_message})
        logging.getLogger("gemini_agent").info(
            "[tarea %s] %s", task_id[:8], safe_message
        )
        self.events.put(ActivityEvent(safe_message, task_id))

    def _set_status(self, task_id: str, value: str) -> None:
        self._archive_trace_event(task_id, "status", {"state": value})
        state = AgentState(value)
        if state != AgentState.PAUSED:
            with self._lock:
                self._worker_state = state
        self.events.put(StatusEvent(state, task_id))

    def _capture(self, task: _Task, context: TaskContext) -> ScreenFrame:
        reply: queue.Queue[ScreenFrame | BaseException] = queue.Queue(maxsize=1)
        self.events.put(CaptureRequestEvent(uuid.uuid4().hex, reply))
        while True:
            from gemini_agent import _check_task

            _check_task(context)
            try:
                result = reply.get(timeout=0.05)
                break
            except queue.Empty:
                continue
        if isinstance(result, BaseException):
            raise result
        self._archive_capture(task, result)
        self.events.put(ScreenshotEvent(task.task_id, result))
        return result

    def _archive_capture(self, task: _Task, frame: ScreenFrame) -> None:
        """Persist every task screenshot locally for post-run inspection."""
        with self._lock:
            self._capture_sequence += 1
            sequence = self._capture_sequence
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        stem = f"{timestamp}_{sequence:05d}_{task.task_id[:8]}"
        try:
            CAPTURE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            image_path = CAPTURE_ARCHIVE_DIR / f"{stem}.png"
            metadata_path = CAPTURE_ARCHIVE_DIR / f"{stem}.json"
            image_path.write_bytes(frame.png_bytes)
            metadata = {
                "task_id": task.task_id,
                "sequence": sequence,
                "captured_at": frame.captured_at,
                "saved_at": datetime.now().astimezone().isoformat(),
                "image": image_path.name,
                "width": frame.width,
                "height": frame.height,
                "desktop": {
                    "left": frame.geometry.left,
                    "top": frame.geometry.top,
                    "width": frame.geometry.width,
                    "height": frame.geometry.height,
                    "dpi_awareness": frame.geometry.dpi_awareness,
                    "system_dpi": frame.geometry.system_dpi,
                },
            }
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            self._archive_trace_event(task.task_id, "screenshot", {
                "image": image_path.name,
                "metadata": metadata_path.name,
                "width": frame.width,
                "height": frame.height,
                "desktop_origin": [frame.geometry.left, frame.geometry.top],
            })
            LOGGER.info("Captura archivada: %s (%sx%s)", image_path, frame.width, frame.height)
        except OSError as exc:
            LOGGER.warning("No se pudo archivar la captura: %s", exc)

    def _propose_batch(
        self,
        task: _Task,
        actions: tuple[CursorAction, ...],
        frame: ScreenFrame,
        requires_authorization: bool,
        context: TaskContext,
    ) -> bool:
        """Publish the proposed batch and execute autonomously without approval UI."""
        batch_id = uuid.uuid4().hex
        self.events.put(StatusEvent(AgentState.PROPOSED, task.task_id))
        summary = _authorization_summary(list(actions), frame).split("\n", 1)[-1]
        title = "Modo autónomo: lote validado para ejecución automática:\n"
        self._archive_trace_event(task.task_id, "action_batch_proposed", {
            "batch_id": batch_id,
            "autonomous": True,
            "requires_authorization_before_override": requires_authorization,
            "actions": _redacted_actions(list(actions)),
        })
        self.events.put(ActionProposalEvent(
            task_id=task.task_id,
            batch_id=batch_id,
            actions=actions,
            summary=title + summary,
            requires_authorization=False,
            expires_at=None,
        ))
        if requires_authorization:
            self._emit_activity(task.task_id, "Modo autónomo activo: lote ejecutado sin aprobación manual. La parada de emergencia sigue disponible.")
        else:
            self._emit_activity(task.task_id, "Lote validado; ejecución autónoma.")
        return not self._shutdown.is_set() and not context.cancelled.is_set()

    def _worker_loop(self) -> None:
        try:
            client = self._client_factory()
        except Exception as exc:
            self._startup_error = _redact_sensitive(str(exc))
            self.events.put(ErrorEvent(self._startup_error))
            self.events.put(StatusEvent(AgentState.ERROR))
            for task in self._discard_queued_tasks():
                if isinstance(task, _ModelJob):
                    task.cancelled.set()
                    continue
                self.events.put(StatusEvent(AgentState.STOPPED, task.task_id))
                self.events.put(
                    ActivityEvent(
                        "Instrucción descartada porque Gemini no pudo inicializarse.",
                        task.task_id,
                    )
                )
            return

        self.events.put(StatusEvent(AgentState.IDLE))
        while not self._shutdown.is_set():
            try:
                task = self._tasks.get(timeout=0.05)
            except queue.Empty:
                continue
            if isinstance(task, _ModelJob):
                self._run_model_job(client, task)
                continue
            control = TaskControl()
            with self._lock:
                self._active_task = task
                self._active_control = control
            control.activity = lambda message: self._emit_activity(task.task_id, message)
            control.status = lambda status: self._set_status(task.task_id, status)
            control.capture = lambda: self._capture_for_task(task, control)
            control.propose_batch = lambda actions, frame, required, context: self._propose_batch(
                task, actions, frame, required, context
            )
            self.events.put(StatusEvent(AgentState.THINKING, task.task_id))
            with self._lock:
                self._worker_state = AgentState.THINKING
            try:
                self._emit_activity(
                    task.task_id,
                    "ENTRADA DEL USUARIO:\n" + _redact_sensitive(task.instruction),
                )
                self._emit_activity(
                    task.task_id,
                    f"INICIO DE PROCESAMIENTO: modelo principal={self.selected_model}",
                )
                succeeded = self._task_runner(client, task.instruction, control=control)
                if control.cancelled.is_set():
                    self.events.put(StatusEvent(AgentState.STOPPED, task.task_id))
                elif succeeded:
                    self._archive_trace_event(task.task_id, "task_finished", {"success": True})
                    self.events.put(StatusEvent(AgentState.COMPLETED, task.task_id))
                else:
                    self._archive_trace_event(task.task_id, "task_finished", {"success": False})
                    self.events.put(StatusEvent(AgentState.ERROR, task.task_id))
                self.events.put(TaskFinishedEvent(task.task_id, succeeded))
            except TaskCancelled:
                self._archive_trace_event(task.task_id, "task_cancelled", {"reason": "cancelled_or_emergency_stop"})
                self.events.put(StatusEvent(AgentState.STOPPED, task.task_id))
                self.events.put(TaskFinishedEvent(task.task_id, False))
            except Exception as exc:
                trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                message = _redact_sensitive(
                    f"{type(exc).__name__}: {exc}\nTRAZA COMPLETA:\n{trace}"
                )
                logging.getLogger("gemini_agent").error(
                    "[tarea %s] Fallo fatal; se detiene el servicio completo:\n%s",
                    task.task_id[:8], message,
                )
                self._archive_trace_event(
                    task.task_id, "task_error",
                    {"error": message, "exception_type": type(exc).__name__, "service_stopped": True},
                )
                self.events.put(ErrorEvent(message, task.task_id))
                self.events.put(StatusEvent(AgentState.ERROR, task.task_id))
                self.events.put(TaskFinishedEvent(task.task_id, False))
                self._startup_error = _redact_sensitive(str(exc))
                self._shutdown.set()
                for queued in self._discard_queued_tasks():
                    if isinstance(queued, _ModelJob):
                        queued.cancelled.set()
                        continue
                    self._archive_trace_event(queued.task_id, "task_discarded", {"reason": "fatal_agent_error"})
                    self.events.put(StatusEvent(AgentState.STOPPED, queued.task_id))
                    self.events.put(ActivityEvent("Instrucción descartada porque un error fatal detuvo BabyJarvis.", queued.task_id))
                    self.events.put(TaskFinishedEvent(queued.task_id, False))
            finally:
                with self._lock:
                    self._active_task = None
                    self._active_control = None
                    self._active_batch_id = None
                self._discard_authorization_responses()

    def _capture_for_task(self, task: _Task, control: TaskControl) -> ScreenFrame:
        context = control.context
        if context is None:
            raise RuntimeError("El contexto de tarea no está disponible para capturar.")
        return self._capture(task, context)

    def _run_model_job(self, client: object, job: _ModelJob) -> None:
        try:
            progress = lambda operation, completed, total, model: self.events.put(
                ModelProgressEvent(operation, completed, total, model)
            )
            if job.operation == "discover":
                models = self._model_manager.discover_models(
                    client,
                    cancelled=job.cancelled,
                    progress=progress,
                )
                self.events.put(ModelsDiscoveredEvent(tuple(models)))
                if not job.cancelled.is_set():
                    self.events.put(
                        ActivityEvent(f"Se encontraron {len(models)} modelos compatibles con generateContent.")
                    )
            else:
                results = self._model_manager.test_models(
                    client,
                    job.models,
                    cancelled=job.cancelled,
                    progress=progress,
                )
                self.events.put(
                    ModelDiagnosticsCompletedEvent(
                        tuple(results), job.cancelled.is_set()
                    )
                )
        except Exception as exc:
            self.events.put(ErrorEvent(_redact_sensitive(str(exc))))
        finally:
            with self._lock:
                self._model_job_cancelled = None
                self._model_job_queued = False
