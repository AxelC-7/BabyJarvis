"""Serialized background service for Jarvis desktop tasks."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from gemini_agent import (
    AUTHORIZATION_TIMEOUT_SECONDS,
    MAX_TASK_DURATION_SECONDS,
    TaskCancelled,
    TaskContext,
    TaskControl,
    _authorization_summary,
    _redact_sensitive,
    create_client,
    run_task,
)
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


AgentEvent = (
    StatusEvent
    | ActivityEvent
    | ActionProposalEvent
    | ScreenshotEvent
    | CaptureRequestEvent
    | ErrorEvent
    | TaskFinishedEvent
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


class AgentService:
    """Owns the sole worker that may control the desktop."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], object] = create_client,
        task_runner: Callable[..., bool] = run_task,
        authorization_timeout: float = AUTHORIZATION_TIMEOUT_SECONDS,
        task_duration: float = MAX_TASK_DURATION_SECONDS,
    ) -> None:
        self.events: queue.Queue[AgentEvent] = queue.Queue()
        self._tasks: queue.Queue[_Task] = queue.Queue()
        self._authorization_responses: queue.Queue[_AuthorizationResponse] = queue.Queue()
        self._client_factory = client_factory
        self._task_runner = task_runner
        self._authorization_timeout = authorization_timeout
        self._task_duration = task_duration
        self._shutdown = threading.Event()
        self._lock = threading.Lock()
        self._active_task: _Task | None = None
        self._active_control: TaskControl | None = None
        self._active_batch_id: str | None = None
        self._worker_state = AgentState.THINKING
        self._resume_state = AgentState.THINKING
        self._worker: threading.Thread | None = None
        self._startup_error: str | None = None

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
            self._active_batch_id = None
        discarded = self._discard_queued_tasks()
        if task is not None:
            self.events.put(StatusEvent(AgentState.STOPPED, task.task_id))
            self.events.put(ActivityEvent("Parada de emergencia solicitada.", task.task_id))
        for queued in discarded:
            self.events.put(StatusEvent(AgentState.STOPPED, queued.task_id))
            self.events.put(ActivityEvent("Instrucción descartada por parada de emergencia.", queued.task_id))
        self._discard_authorization_responses()

    def shutdown(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        self.emergency_stop()
        self._shutdown.set()
        if wait and self._worker is not None:
            self._worker.join(timeout)

    def _discard_queued_tasks(self) -> list[_Task]:
        discarded: list[_Task] = []
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

    def _emit_activity(self, task_id: str, message: str) -> None:
        self.events.put(ActivityEvent(message, task_id))

    def _set_status(self, task_id: str, value: str) -> None:
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
        self.events.put(ScreenshotEvent(task.task_id, result))
        return result

    def _propose_batch(
        self,
        task: _Task,
        actions: tuple[CursorAction, ...],
        frame: ScreenFrame,
        requires_authorization: bool,
        context: TaskContext,
    ) -> bool:
        batch_id = uuid.uuid4().hex
        expires_at = (
            min(time.monotonic() + self._authorization_timeout, context.deadline)
            if requires_authorization
            else None
        )
        self.events.put(StatusEvent(AgentState.PROPOSED, task.task_id))
        summary = _authorization_summary(list(actions), frame).split("\n", 1)[-1]
        title = (
            "Lote validado; requiere autorización:\n"
            if requires_authorization
            else "Lote validado; permitido por la política actual:\n"
        )
        self.events.put(
            ActionProposalEvent(
                task_id=task.task_id,
                batch_id=batch_id,
                actions=actions,
                summary=title + summary,
                requires_authorization=requires_authorization,
                expires_at=expires_at,
            )
        )
        if not requires_authorization:
            return True

        with self._lock:
            self._active_batch_id = batch_id
        self.events.put(StatusEvent(AgentState.WAITING_AUTHORIZATION, task.task_id))
        self._emit_activity(task.task_id, "Esperando autorización para el lote mostrado.")
        try:
            while expires_at is not None and time.monotonic() < expires_at:
                from gemini_agent import _check_task

                _check_task(context, respect_pause=False)
                if context.paused.is_set():
                    time.sleep(0.05)
                    continue
                try:
                    response = self._authorization_responses.get(timeout=0.05)
                except queue.Empty:
                    continue
                if (
                    response.batch_id != batch_id
                    or response.actions != actions
                    or time.monotonic() >= expires_at
                ):
                    self._emit_activity(
                        task.task_id,
                        "Se ignoró una autorización vencida o asociada a otro lote.",
                    )
                    continue
                self._emit_activity(
                    task.task_id,
                    "Lote autorizado." if response.approved else "Lote rechazado.",
                )
                return response.approved
        finally:
            with self._lock:
                if self._active_batch_id == batch_id:
                    self._active_batch_id = None
        self._emit_activity(task.task_id, "La autorización venció; el lote se descartó.")
        return False

    def _worker_loop(self) -> None:
        try:
            client = self._client_factory()
        except Exception as exc:
            self._startup_error = _redact_sensitive(str(exc))
            self.events.put(ErrorEvent(self._startup_error))
            self.events.put(StatusEvent(AgentState.ERROR))
            for task in self._discard_queued_tasks():
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
                succeeded = self._task_runner(client, task.instruction, control=control)
                if control.cancelled.is_set():
                    self.events.put(StatusEvent(AgentState.STOPPED, task.task_id))
                elif succeeded:
                    self.events.put(StatusEvent(AgentState.COMPLETED, task.task_id))
                else:
                    self.events.put(StatusEvent(AgentState.ERROR, task.task_id))
                self.events.put(TaskFinishedEvent(task.task_id, succeeded))
            except TaskCancelled:
                self.events.put(StatusEvent(AgentState.STOPPED, task.task_id))
                self.events.put(TaskFinishedEvent(task.task_id, False))
            except Exception as exc:
                message = _redact_sensitive(str(exc)) or type(exc).__name__
                self.events.put(ErrorEvent(message, task.task_id))
                self.events.put(StatusEvent(AgentState.ERROR, task.task_id))
                self.events.put(TaskFinishedEvent(task.task_id, False))
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
