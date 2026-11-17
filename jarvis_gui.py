"""Tkinter control panel for the Jarvis desktop agent."""

from __future__ import annotations

import base64
import queue
import tkinter as tk
import time
from tkinter import scrolledtext, ttk

from agent_service import (
    ActionProposalEvent,
    ActivityEvent,
    AgentService,
    AgentState,
    CaptureRequestEvent,
    ErrorEvent,
    ScreenshotEvent,
    StatusEvent,
)
from screen_vision import ScreenFrame, capture_screen


_STATE_LABELS = {
    AgentState.INITIALIZING: "Iniciando",
    AgentState.IDLE: "Listo",
    AgentState.QUEUED: "En cola",
    AgentState.THINKING: "Analizando",
    AgentState.PROPOSED: "Acciones propuestas",
    AgentState.WAITING_AUTHORIZATION: "Esperando autorización",
    AgentState.EXECUTING: "Ejecutando",
    AgentState.PAUSED: "Pausado",
    AgentState.COMPLETED: "Completado (verificado)",
    AgentState.ERROR: "Error",
    AgentState.STOPPED: "Detenido",
}


class JarvisApp:
    def __init__(self, root: tk.Tk, service: AgentService | None = None) -> None:
        self.root = root
        self.service = service or AgentService()
        self._state = AgentState.INITIALIZING
        self._active_task_id: str | None = None
        self._pending_proposal: ActionProposalEvent | None = None
        self._photo: tk.PhotoImage | None = None
        self._closing = False

        root.title("Jarvis - Control de escritorio")
        root.geometry("1000x700")
        root.minsize(760, 560)
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._build_widgets()
        self.service.start()
        self.root.after(50, self._poll_events)

    def _build_widgets(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        self.root.rowconfigure(3, weight=1)

        input_frame = ttk.LabelFrame(self.root, text="Instrucción")
        input_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        input_frame.columnconfigure(0, weight=1)
        self.instruction = ttk.Entry(input_frame)
        self.instruction.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        self.instruction.bind("<Return>", self._submit)
        self.submit_button = ttk.Button(
            input_frame, text="Agregar tarea", command=self._submit
        )
        self.submit_button.grid(row=0, column=1, padx=6, pady=6)

        controls = ttk.Frame(self.root)
        controls.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        self.state_label = ttk.Label(controls, text="Estado: Iniciando")
        self.state_label.pack(side="left", padx=(0, 14))
        self.pause_button = ttk.Button(
            controls, text="Pausar", command=self.service.pause
        )
        self.pause_button.pack(side="left", padx=3)
        self.resume_button = ttk.Button(
            controls, text="Reanudar", command=self.service.resume
        )
        self.resume_button.pack(side="left", padx=3)
        self.stop_button = ttk.Button(
            controls,
            text="Parada de emergencia",
            command=self._emergency_stop,
        )
        self.stop_button.pack(side="right", padx=3)

        content = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        content.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        activity_frame = ttk.LabelFrame(content, text="Actividad e historial")
        proposal_frame = ttk.LabelFrame(content, text="Lote propuesto")
        content.add(activity_frame, weight=3)
        content.add(proposal_frame, weight=2)
        activity_frame.rowconfigure(0, weight=1)
        activity_frame.columnconfigure(0, weight=1)
        self.activity = scrolledtext.ScrolledText(
            activity_frame, state="disabled", wrap=tk.WORD, height=12
        )
        self.activity.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        proposal_frame.rowconfigure(0, weight=1)
        proposal_frame.columnconfigure(0, weight=1)
        self.proposal = scrolledtext.ScrolledText(
            proposal_frame, state="disabled", wrap=tk.WORD, height=12
        )
        self.proposal.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        approval_controls = ttk.Frame(proposal_frame)
        approval_controls.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))
        self.approve_button = ttk.Button(
            approval_controls,
            text="Autorizar lote",
            command=lambda: self._respond_to_proposal(True),
            state="disabled",
        )
        self.approve_button.pack(side="left", padx=3)
        self.reject_button = ttk.Button(
            approval_controls,
            text="Rechazar lote",
            command=lambda: self._respond_to_proposal(False),
            state="disabled",
        )
        self.reject_button.pack(side="left", padx=3)

        screenshot_frame = ttk.LabelFrame(self.root, text="Captura utilizada por Jarvis")
        screenshot_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=(5, 10))
        screenshot_frame.columnconfigure(0, weight=1)
        screenshot_frame.rowconfigure(0, weight=1)
        self.screenshot_label = ttk.Label(
            screenshot_frame, text="Aún no hay capturas.", anchor="center"
        )
        self.screenshot_label.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

    def _submit(self, _event: object = None) -> None:
        instruction = self.instruction.get().strip()
        if not instruction:
            return
        try:
            task_id = self.service.submit_task(instruction)
        except Exception as exc:
            self._append_activity(f"Error al agregar instrucción: {exc}")
            return
        self.instruction.delete(0, tk.END)
        self._active_task_id = self._active_task_id or task_id
        self._append_activity(f"Tarea {task_id[:8]} agregada a la cola.")

    def _poll_events(self) -> None:
        if self._closing:
            return
        while True:
            try:
                event = self.service.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(50, self._poll_events)

    def _handle_event(self, event: object) -> None:
        if isinstance(event, StatusEvent):
            if event.state == AgentState.IDLE:
                self._active_task_id = None
            elif event.state == AgentState.QUEUED:
                if self._active_task_id not in {None, event.task_id}:
                    return
                self._active_task_id = event.task_id
            elif event.task_id is not None:
                if event.state == AgentState.THINKING and (
                    self._active_task_id is None
                    or self._state in {
                        AgentState.COMPLETED,
                        AgentState.ERROR,
                        AgentState.STOPPED,
                    }
                ):
                    self._active_task_id = event.task_id
                elif (
                    self._active_task_id is not None
                    and self._active_task_id != event.task_id
                ):
                    return
                else:
                    self._active_task_id = event.task_id
            self._state = event.state
            self.state_label.configure(text=f"Estado: {_STATE_LABELS[event.state]}")
            if event.state in {
                AgentState.COMPLETED,
                AgentState.ERROR,
                AgentState.STOPPED,
            }:
                self._pending_proposal = None
                self._set_authorization_buttons(False)
                self._active_task_id = None
        elif isinstance(event, ActivityEvent):
            self._append_activity(event.message)
        elif isinstance(event, ActionProposalEvent):
            self._show_proposal(event)
        elif isinstance(event, ScreenshotEvent):
            self._show_screenshot(event.frame)
        elif isinstance(event, CaptureRequestEvent):
            self._capture_without_jarvis(event)
        elif isinstance(event, ErrorEvent):
            self._append_activity(f"ERROR: {event.message}")
            self.state_label.configure(text="Estado: Error")

    def _show_proposal(self, event: ActionProposalEvent) -> None:
        self._pending_proposal = event if event.requires_authorization else None
        self.proposal.configure(state="normal")
        self.proposal.delete("1.0", tk.END)
        if event.requires_authorization:
            heading = (
                f"Lote {event.batch_id[:8]} — requiere autorización\n"
                "La aprobación aplica solo a estas acciones y caduca.\n\n"
            )
        else:
            heading = (
                f"Lote {event.batch_id[:8]} — permitido por la política actual\n\n"
            )
        self.proposal.insert("1.0", heading + event.summary)
        self.proposal.configure(state="disabled")
        self._set_authorization_buttons(event.requires_authorization)
        if event.requires_authorization and event.expires_at is not None:
            delay = max(1, round((event.expires_at - time.monotonic()) * 1000))
            self.root.after(
                delay,
                lambda batch_id=event.batch_id: self._expire_proposal(batch_id),
            )
        self._append_activity(
            "Gemini propuso un lote que requiere autorización."
            if event.requires_authorization
            else "Gemini propuso un lote permitido por la política actual."
        )

    def _set_authorization_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.approve_button.configure(state=state)
        self.reject_button.configure(state=state)

    def _respond_to_proposal(self, approved: bool) -> None:
        proposal = self._pending_proposal
        if proposal is None:
            return
        if proposal.expires_at is not None:
            if time.monotonic() >= proposal.expires_at:
                self._expire_proposal(proposal.batch_id)
                return
        self._pending_proposal = None
        self._set_authorization_buttons(False)
        self.service.authorize(
            proposal.batch_id,
            proposal.actions,
            approved,
        )

    def _expire_proposal(self, batch_id: str) -> None:
        proposal = self._pending_proposal
        if proposal is None or proposal.batch_id != batch_id:
            return
        if proposal.expires_at is not None and time.monotonic() < proposal.expires_at:
            return
        self._pending_proposal = None
        self._set_authorization_buttons(False)
        self._append_activity("La autorización venció; no se envió.")

    def _capture_without_jarvis(self, event: CaptureRequestEvent) -> None:
        self.root.withdraw()

        def capture_after_hide() -> None:
            try:
                frame = capture_screen()
            except Exception as exc:
                event.reply.put(exc)
            else:
                event.reply.put(frame)
            finally:
                if not self._closing:
                    self.root.deiconify()

        self.root.after(150, capture_after_hide)

    def _show_screenshot(self, frame: ScreenFrame) -> None:
        try:
            image = tk.PhotoImage(
                data=base64.b64encode(frame.png_bytes).decode("ascii")
            )
            factor = max(
                1,
                (frame.width + 699) // 700,
                (frame.height + 399) // 400,
            )
            if factor > 1:
                image = image.subsample(factor, factor)
        except tk.TclError as exc:
            self._append_activity(f"No se pudo mostrar la captura: {exc}")
            return
        self._photo = image
        self.screenshot_label.configure(
            image=image,
            text=f"{frame.width}x{frame.height}",
            compound=tk.TOP,
        )

    def _append_activity(self, message: str) -> None:
        self.activity.configure(state="normal")
        self.activity.insert(tk.END, message + "\n")
        self.activity.see(tk.END)
        self.activity.configure(state="disabled")

    def _emergency_stop(self) -> None:
        self._pending_proposal = None
        self._set_authorization_buttons(False)
        self.service.emergency_stop()

    def _close(self) -> None:
        self._closing = True
        self.service.shutdown()
        self.root.destroy()


def main() -> None:
    from gemini_agent import setup_logging

    setup_logging()
    root = tk.Tk()
    JarvisApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
