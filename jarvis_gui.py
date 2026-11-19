"""Tkinter control panel for the Jarvis desktop agent."""

from __future__ import annotations

import base64
import ctypes
import queue
import tkinter as tk
import time
from ctypes import wintypes
from tkinter import scrolledtext, ttk

from agent_service import (
    ActionProposalEvent,
    ActivityEvent,
    AgentService,
    AgentState,
    CaptureRequestEvent,
    ErrorEvent,
    ModelDiagnosticsCompletedEvent,
    ModelProgressEvent,
    ModelsDiscoveredEvent,
    PrimaryModelSelectedEvent,
    ScreenshotEvent,
    StatusEvent,
)
from desktop_geometry import (
    get_primary_screen_geometry,
    rectangle_intersects_geometry,
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
        self._discovered_models: list[str] = []
        self._model_results: dict[str, object] = {}

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

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        self.control_tab = ttk.Frame(self.notebook)
        self.models_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.control_tab, text="Control")
        self.notebook.add(self.models_tab, text="Modelos Gemini")
        self.control_tab.columnconfigure(0, weight=1)
        self.control_tab.rowconfigure(0, weight=2)
        self.control_tab.rowconfigure(1, weight=1)

        content = ttk.Panedwindow(self.control_tab, orient=tk.HORIZONTAL)
        content.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
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

        screenshot_frame = ttk.LabelFrame(self.control_tab, text="Captura del monitor principal")
        screenshot_frame.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)
        screenshot_frame.columnconfigure(0, weight=1)
        screenshot_frame.rowconfigure(0, weight=1)
        self.screenshot_label = ttk.Label(
            screenshot_frame, text="Aún no hay capturas.", anchor="center"
        )
        self.screenshot_label.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self._build_model_widgets()
        self._load_model_report()

    def _build_model_widgets(self) -> None:
        self.models_tab.columnconfigure(0, weight=1)
        self.models_tab.rowconfigure(3, weight=1)
        actions = ttk.Frame(self.models_tab)
        actions.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ttk.Button(
            actions,
            text="Buscar modelos disponibles",
            command=self._discover_models,
        ).pack(side="left", padx=3)
        ttk.Button(
            actions,
            text="Probar compatibilidad",
            command=self._test_models,
        ).pack(side="left", padx=3)
        ttk.Button(
            actions,
            text="Cancelar pruebas",
            command=self._cancel_model_tests,
        ).pack(side="left", padx=3)
        self.model_progress_text = tk.StringVar(value="Sin diagnóstico activo.")
        ttk.Label(self.models_tab, textvariable=self.model_progress_text).grid(
            row=1, column=0, sticky="ew", padx=10
        )
        self.model_progress = ttk.Progressbar(
            self.models_tab, mode="determinate", maximum=1
        )
        self.model_progress.grid(row=2, column=0, sticky="ew", padx=10, pady=4)

        table_frame = ttk.Frame(self.models_tab)
        table_frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=6)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("model", "status", "duration", "reason")
        self.model_table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=8,
        )
        for column, heading, width in (
            ("model", "Modelo", 230),
            ("status", "Compatibilidad", 180),
            ("duration", "Duración (s)", 100),
            ("reason", "Motivo", 380),
        ):
            self.model_table.heading(column, text=heading)
            self.model_table.column(column, width=width, stretch=column == "reason")
        scrollbar = ttk.Scrollbar(
            table_frame, orient=tk.VERTICAL, command=self.model_table.yview
        )
        self.model_table.configure(yscrollcommand=scrollbar.set)
        self.model_table.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        selection = ttk.Frame(self.models_tab)
        selection.grid(row=4, column=0, sticky="ew", padx=8, pady=8)
        selection.columnconfigure(1, weight=1)
        ttk.Label(selection, text="Modelo principal:").grid(
            row=0, column=0, padx=4
        )
        self.primary_model = tk.StringVar()
        self.primary_model_combo = ttk.Combobox(
            selection,
            textvariable=self.primary_model,
            state="readonly",
        )
        self.primary_model_combo.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(
            selection,
            text="Usar como principal",
            command=self._select_primary_model,
        ).grid(row=0, column=2, padx=4)
        self.selected_model_text = tk.StringVar(
            value=f"Configurado: {self.service.selected_model}"
        )
        ttk.Label(selection, textvariable=self.selected_model_text).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=4, pady=(6, 0)
        )

    def _load_model_report(self) -> None:
        report = self.service.model_report()
        if report is None:
            return
        discovered = report.get("discovered_models", [])
        if isinstance(discovered, list):
            self._discovered_models = [
                name for name in discovered if isinstance(name, str)
            ]
        results = report.get("results", [])
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict) and isinstance(result.get("model"), str):
                    self._model_results[result["model"]] = result
        self._render_model_table()
        selected = report.get("selected_primary")
        self.selected_model_text.set(
            f"Configurado: {selected or self.service.selected_model}"
        )

    def _render_model_table(self) -> None:
        for item in self.model_table.get_children():
            self.model_table.delete(item)
        names = list(dict.fromkeys(self._discovered_models + list(self._model_results)))
        compatible = []
        for model in names:
            result = self._model_results.get(model)
            if result is None:
                status, duration, reason = "Encontrado; pendiente", "", ""
            else:
                status = str(result.get("classification", "Error desconocido"))
                duration = str(result.get("duration_seconds", ""))
                reason = str(result.get("reason", ""))
                if status == "compatible_text_image":
                    compatible.append(model)
            self.model_table.insert(
                "", tk.END, values=(model, status, duration, reason)
            )
        self.primary_model_combo.configure(values=compatible)
        if self.primary_model.get() not in compatible:
            self.primary_model.set(compatible[0] if compatible else "")

    def _discover_models(self) -> None:
        try:
            self.service.discover_models()
            self.model_progress_text.set("Buscando todos los modelos disponibles...")
            self.model_progress.configure(mode="indeterminate")
            self.model_progress.start(12)
        except Exception as exc:
            self._append_activity(f"No se pudo iniciar la búsqueda: {exc}")

    def _test_models(self) -> None:
        try:
            self.service.test_model_compatibility()
            self.model_progress_text.set("Probando texto, imagen y contrato JSON...")
            self.model_progress.configure(mode="indeterminate")
            self.model_progress.start(12)
        except Exception as exc:
            self._append_activity(f"No se pudieron iniciar las pruebas: {exc}")

    def _cancel_model_tests(self) -> None:
        if self.service.cancel_model_tests():
            self.model_progress_text.set("Cancelación solicitada...")

    def _select_primary_model(self) -> None:
        model = self.primary_model.get().strip()
        if not model:
            return
        try:
            self.service.select_primary_model(model)
        except Exception as exc:
            self._append_activity(f"No se pudo seleccionar el modelo: {exc}")

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
        try:
            while True:
                try:
                    event = self.service.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(event)
                except Exception as exc:
                    self._append_activity(
                        f"Error procesando evento ({type(exc).__name__}); "
                        "Jarvis continúa abierto."
                    )
        finally:
            if not self._closing:
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
            self.model_progress.stop()
            self._append_activity(f"ERROR: {event.message}")
            self.state_label.configure(text="Estado: Error")
            self.model_progress_text.set("La operación terminó con error.")
        elif isinstance(event, ModelProgressEvent):
            self._handle_model_progress(event)
        elif isinstance(event, ModelsDiscoveredEvent):
            self.model_progress.stop()
            self.model_progress.configure(mode="determinate", value=0, maximum=1)
            self._discovered_models = list(event.models)
            self._render_model_table()
            self.model_progress_text.set(
                f"Encontrados {len(event.models)} modelos con generateContent."
            )
        elif isinstance(event, ModelDiagnosticsCompletedEvent):
            self.model_progress.stop()
            self.model_progress.configure(mode="determinate", value=1, maximum=1)
            self._model_results = {
                result.model: {
                    "model": result.model,
                    "classification": result.classification,
                    "duration_seconds": result.duration_seconds,
                    "reason": result.reason,
                }
                for result in event.results
            }
            self._render_model_table()
            suffix = " (cancelado)" if event.cancelled else ""
            self.model_progress_text.set(
                f"Pruebas terminadas{suffix}: {len(event.results)} resultado(s)."
            )
        elif isinstance(event, PrimaryModelSelectedEvent):
            self.selected_model_text.set(f"Configurado: {event.model}")
            self._append_activity(f"Modelo principal configurado: {event.model}")

    def _handle_model_progress(self, event: ModelProgressEvent) -> None:
        if event.total is None:
            self.model_progress_text.set(
                f"Buscando modelos... {event.completed} encontrado(s)"
            )
            return
        self.model_progress.stop()
        self.model_progress.configure(
            mode="determinate",
            maximum=max(1, event.total),
            value=event.completed,
        )
        self.model_progress_text.set(
            f"Probando {event.completed}/{event.total}: {event.model}"
        )

    def _show_proposal(self, event: ActionProposalEvent) -> None:
        self._pending_proposal = event if event.requires_authorization else None
        self.proposal.configure(state="normal")
        self.proposal.delete("1.0", tk.END)
        if event.requires_authorization:
            heading = (
                f"Lote {event.batch_id[:8]} — requiere autorización\n"
                "La aprobación no caduca automáticamente y solo aplica a este lote.\n\n"
            )
        else:
            heading = (
                f"Lote {event.batch_id[:8]} — modo autónomo: ejecución automática\n\n"
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
            else "Gemini propuso un lote para ejecución autónoma."
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
        hide_window = self._window_overlaps_primary_monitor()
        if hide_window:
            self.root.withdraw()

        def capture_after_hide() -> None:
            try:
                frame = capture_screen()
            except Exception as exc:
                event.reply.put(exc)
            else:
                event.reply.put(frame)
            finally:
                if hide_window and not self._closing:
                    self.root.deiconify()

        self.root.after(150 if hide_window else 0, capture_after_hide)

    def _window_overlaps_primary_monitor(self) -> bool:
        try:
            hwnd = self.root.winfo_id()
            rect = wintypes.RECT()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetWindowRect.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.RECT),
            ]
            user32.GetWindowRect.restype = wintypes.BOOL
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            return rectangle_intersects_geometry(
                rect.left,
                rect.top,
                rect.right,
                rect.bottom,
                get_primary_screen_geometry(),
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return True

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
