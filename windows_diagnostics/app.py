from __future__ import annotations

import json
import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Any, Callable

from . import collectors
from .analyzer import (
    analyze_memory_trends,
    analyze_processes,
    analyze_startup,
    rank_process_memory,
    summarize_temperatures,
)
from .benchmarks import run_benchmark
from .reports import (
    build_report,
    compare_to_baseline,
    export_report,
    load_comparison_baseline,
)


_LOGGER = logging.getLogger(__name__)
_MAX_HISTORY_FILE_BYTES = 50 * 1024 * 1024


class DiagnosticsApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Jarvis - Diagnóstico de Windows")
        self.root.geometry("1120x760")
        self.root.minsize(850, 580)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        data_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Jarvis" / "WindowsDiagnostics"
        data_root.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=data_root / "diagnostics.log",
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        self._history_path = data_root / "history.jsonl"
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.interval = tk.IntVar(value=5)
        self.history_minutes = tk.IntVar(value=60)
        self.threshold = tk.DoubleVar(value=90.0)
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="diagnostics")
        self._closing = False
        self._refresh_inflight = False
        self._monitoring = True
        self._processes: list[dict[str, Any]] = []
        self._startup: list[dict[str, Any]] = []
        self._snapshot: dict[str, Any] | None = None
        self._process_memory: list[dict[str, Any]] = []
        self._memory_trends: list[dict[str, Any]] = []
        self._history: list[dict[str, Any]] = self._load_history()
        self._benchmarks: list[dict[str, Any]] = []
        self._report: dict[str, Any] | None = None
        self._comparison_baseline: dict[str, Any] | None = None
        self._benchmark_stop: threading.Event | None = None
        self._temp_alert_sent = False
        self._previous_disk_counters: tuple[int, int, float] | None = None
        self.status_text = tk.StringVar(value="Recopilando información del equipo...")

        self._build_widgets()
        self.root.after(100, self._poll_events)
        self.root.after(250, self._refresh)

    def _build_widgets(self) -> None:
        header = ttk.Frame(self.root, padding=(10, 8))
        header.pack(fill="x")
        ttk.Label(header, textvariable=self.status_text).pack(side="left", fill="x", expand=True)
        ttk.Label(header, text="Umbral térmico °C:").pack(side="left", padx=(8, 3))
        ttk.Spinbox(header, from_=50, to=110, increment=1, textvariable=self.threshold, width=5).pack(side="left")
        self.monitor_button = ttk.Button(header, text="Pausar monitoreo", command=self._toggle_monitoring)
        self.monitor_button.pack(side="left", padx=(8, 0))

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.dashboard_tab = ttk.Frame(self.tabs, padding=8)
        self.process_tab = ttk.Frame(self.tabs, padding=8)
        self.memory_tab = ttk.Frame(self.tabs, padding=8)
        self.audit_tab = ttk.Frame(self.tabs, padding=8)
        self.tests_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.dashboard_tab, text="Resumen")
        self.tabs.add(self.process_tab, text="Procesos")
        self.tabs.add(self.memory_tab, text="Memoria")
        self.tabs.add(self.audit_tab, text="Servicios e inicio")
        self.tabs.add(self.tests_tab, text="Pruebas e informes")
        self._build_dashboard()
        self._build_processes()
        self._build_memory()
        self._build_audit()
        self._build_tests()

    def _build_dashboard(self) -> None:
        self.dashboard_tab.columnconfigure(0, weight=1)
        self.dashboard_tab.rowconfigure(2, weight=1)
        self.metrics = tk.StringVar(value="CPU: --     RAM: --     GPU: --     Disco: --")
        ttk.Label(self.dashboard_tab, textvariable=self.metrics, font=("Segoe UI", 13)).grid(
            row=0, column=0, sticky="ew", pady=(2, 8)
        )
        self.temperature_text = tk.StringVar(value="Temperaturas: buscando sensores...")
        ttk.Label(self.dashboard_tab, textvariable=self.temperature_text, wraplength=1000).grid(
            row=1, column=0, sticky="ew", pady=(0, 6)
        )
        self.chart = tk.Canvas(self.dashboard_tab, height=230, background="white", highlightthickness=1, highlightbackground="#d0d7de")
        self.chart.grid(row=2, column=0, sticky="nsew", pady=4)
        controls = ttk.Frame(self.dashboard_tab)
        controls.grid(row=3, column=0, sticky="ew", pady=(8, 2))
        ttk.Button(controls, text="Ejecutar diagnóstico completo", command=self._run_full_diagnostic).pack(side="left")
        ttk.Button(controls, text="Actualizar ahora", command=self._refresh).pack(side="left", padx=6)
        ttk.Label(controls, text="Intervalo (s):").pack(side="left", padx=(18, 3))
        ttk.Spinbox(controls, from_=5, to=60, textvariable=self.interval, width=5).pack(side="left")
        ttk.Label(controls, text="Retención (min):").pack(side="left", padx=(10, 3))
        ttk.Spinbox(controls, from_=5, to=120, increment=5, textvariable=self.history_minutes, width=5).pack(side="left")
        self.health_text = tk.StringVar(value="Ejecute un diagnóstico para obtener recomendaciones.")
        ttk.Label(self.dashboard_tab, textvariable=self.health_text, wraplength=1000, justify="left").grid(
            row=4, column=0, sticky="ew", pady=(6, 2)
        )

    def _build_processes(self) -> None:
        self.process_tab.columnconfigure(0, weight=1)
        self.process_tab.rowconfigure(1, weight=1)
        controls = ttk.Frame(self.process_tab)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(controls, text="Analizar todos los procesos", command=self._scan_processes).pack(side="left")
        ttk.Label(controls, text="La firma se consulta en Windows; una firma ausente no confirma malware.").pack(side="left", padx=10)
        columns = (
            "risk", "name", "pid", "cpu", "memory", "memory_percent", "user",
            "runtime", "started", "signature", "manufacturer", "version",
            "network", "path", "reasons",
        )
        self.process_table = ttk.Treeview(self.process_tab, columns=columns, show="headings", height=18)
        headings = {
            "risk": ("Riesgo", 85), "name": ("Proceso", 150), "pid": ("PID", 65),
            "cpu": ("CPU %", 70), "memory": ("RAM MiB", 85), "memory_percent": ("% RAM", 70),
            "user": ("Usuario", 130), "started": ("Inicio", 145),
            "runtime": ("Tiempo activo", 100), "signature": ("Firma", 100),
            "manufacturer": ("Fabricante / firmante", 220), "version": ("Versión", 120),
            "network": ("Conexiones remotas", 180),
            "path": ("Ruta", 270), "reasons": ("Indicadores / explicación", 350),
        }
        for column, (heading, width) in headings.items():
            self.process_table.heading(column, text=heading, command=lambda key=column: self._sort_table(self.process_table, key, False))
            self.process_table.column(column, width=width, minwidth=55, stretch=column in {"path", "reasons"})
        self._configure_table(self.process_tab, self.process_table)
        self.process_table.tag_configure("alto", background="#ffe3e3")
        self.process_table.tag_configure("medio", background="#fff4ce")

    def _build_memory(self) -> None:
        self.memory_tab.columnconfigure(0, weight=1)
        self.memory_tab.rowconfigure(1, weight=2)
        self.memory_tab.rowconfigure(3, weight=1)
        self.memory_summary = tk.StringVar(value="Esperando muestras de memoria por proceso...")
        ttk.Label(self.memory_tab, textvariable=self.memory_summary, wraplength=1000).grid(
            row=0, column=0, sticky="ew", pady=(0, 6)
        )
        columns = ("name", "pid", "memory", "percent", "trend", "path")
        self.memory_table = ttk.Treeview(self.memory_tab, columns=columns, show="headings", height=12)
        for column, heading, width in (
            ("name", "Proceso", 190), ("pid", "PID", 70), ("memory", "RAM MiB", 95),
            ("percent", "% de RAM total", 110), ("trend", "Tendencia", 310), ("path", "Ruta", 380),
        ):
            self.memory_table.heading(
                column,
                text=heading,
                command=lambda key=column: self._sort_table(self.memory_table, key, False),
            )
            self.memory_table.column(column, width=width, minwidth=60, stretch=column in {"trend", "path"})
        self._configure_table(self.memory_tab, self.memory_table)
        self.memory_table.tag_configure("crecimiento", background="#fff4ce")
        self.memory_table.bind("<<TreeviewSelect>>", self._select_memory_process)
        self._memory_item_keys: dict[str, tuple[int, float]] = {}
        self.memory_chart = tk.Canvas(
            self.memory_tab, height=180, background="white",
            highlightthickness=1, highlightbackground="#d0d7de",
        )
        self.memory_chart.grid(row=3, column=0, sticky="nsew", pady=(8, 0))

    def _build_audit(self) -> None:
        self.audit_tab.columnconfigure(0, weight=1)
        self.audit_tab.rowconfigure(1, weight=1)
        controls = ttk.Frame(self.audit_tab)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(controls, text="Revisar servicios, inicio y tareas", command=self._scan_startup).pack(side="left")
        ttk.Label(controls, text="Solo lectura: no se modifican servicios, tareas ni registro.").pack(side="left", padx=10)
        columns = ("risk", "kind", "name", "state", "user", "location", "reasons")
        self.audit_table = ttk.Treeview(self.audit_tab, columns=columns, show="headings", height=18)
        headings = {
            "risk": ("Revisión", 85), "kind": ("Tipo", 110), "name": ("Nombre", 220),
            "state": ("Estado", 95), "user": ("Usuario", 150), "location": ("Ruta / acción", 350),
            "reasons": ("Nota", 320),
        }
        for column, (heading, width) in headings.items():
            self.audit_table.heading(column, text=heading, command=lambda key=column: self._sort_table(self.audit_table, key, False))
            self.audit_table.column(column, width=width, minwidth=55, stretch=column in {"location", "reasons"})
        self._configure_table(self.audit_tab, self.audit_table)
        self.audit_table.tag_configure("medio", background="#fff4ce")
        self.audit_table.tag_configure("advertencia", background="#ffe3e3")

    def _build_tests(self) -> None:
        self.tests_tab.columnconfigure(0, weight=1)
        self.tests_tab.rowconfigure(3, weight=1)
        controls = ttk.LabelFrame(self.tests_tab, text="Prueba controlada (límites máximos: 30 s, 70% CPU, 256 MB)", padding=8)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="Prueba:").grid(row=0, column=0, padx=3)
        self.benchmark_mode = tk.StringVar(value="cpu")
        ttk.Combobox(controls, textvariable=self.benchmark_mode, values=("cpu", "memory", "disk", "stability"), state="readonly", width=12).grid(row=0, column=1, padx=3)
        ttk.Label(controls, text="Duración (s):").grid(row=0, column=2, padx=(12, 3))
        self.benchmark_duration = tk.IntVar(value=10)
        ttk.Spinbox(controls, from_=1, to=30, textvariable=self.benchmark_duration, width=5).grid(row=0, column=3, padx=3)
        ttk.Label(controls, text="CPU máx. %:").grid(row=0, column=4, padx=(12, 3))
        self.benchmark_cpu = tk.IntVar(value=50)
        ttk.Spinbox(controls, from_=10, to=70, increment=5, textvariable=self.benchmark_cpu, width=5).grid(row=0, column=5, padx=3)
        ttk.Label(controls, text="RAM / disco MB:").grid(row=0, column=6, padx=(12, 3))
        self.benchmark_size = tk.IntVar(value=64)
        ttk.Spinbox(controls, from_=1, to=256, increment=16, textvariable=self.benchmark_size, width=5).grid(row=0, column=7, padx=3)
        self.benchmark_button = ttk.Button(controls, text="Iniciar prueba", command=self._start_benchmark)
        self.benchmark_button.grid(row=0, column=8, padx=(12, 3))
        self.cancel_benchmark_button = ttk.Button(controls, text="Cancelar", command=self._cancel_benchmark, state="disabled")
        self.cancel_benchmark_button.grid(row=0, column=9, padx=3)
        self.benchmark_progress = ttk.Progressbar(self.tests_tab, mode="determinate", maximum=1)
        self.benchmark_progress.grid(row=1, column=0, sticky="ew", pady=4)
        self.benchmark_text = tk.StringVar(value="Las pruebas se detienen si un sensor accesible supera el umbral térmico configurado.")
        ttk.Label(self.tests_tab, textvariable=self.benchmark_text, wraplength=1000).grid(row=2, column=0, sticky="ew", pady=4)
        self.report_text = tk.Text(self.tests_tab, wrap="word", height=12, state="disabled")
        self.report_text.grid(row=3, column=0, sticky="nsew", pady=6)
        actions = ttk.Frame(self.tests_tab)
        actions.grid(row=4, column=0, sticky="ew")
        ttk.Button(actions, text="Cargar informe anterior (TXT/JSON)", command=self._choose_comparison_report).pack(side="left", padx=(0, 6))
        for extension in ("txt", "json", "html"):
            ttk.Button(actions, text=f"Exportar {extension.upper()}", command=lambda fmt=extension: self._export(fmt)).pack(side="left", padx=(0, 6))
        self.history_text = tk.StringVar(value=f"Historial periódico: {self._history_path}")
        ttk.Label(actions, textvariable=self.history_text).pack(side="left", padx=8)

    @staticmethod
    def _configure_table(parent: ttk.Widget, table: ttk.Treeview) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        table.grid(row=1, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(parent, orient="vertical", command=table.yview)
        horizontal = ttk.Scrollbar(parent, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal.grid(row=2, column=0, sticky="ew")

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            if self._history_path.exists():
                limit = self._history_limit()
                lines = self._history_path.read_text(encoding="utf-8").splitlines()[-limit:]
                return [json.loads(line) for line in lines if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            _LOGGER.warning("No se pudo leer el historial: %s", exc)
        return []

    def _history_limit(self) -> int:
        try:
            interval = max(5, min(60, int(self.interval.get())))
            retention = max(5, min(120, int(self.history_minutes.get())))
        except (tk.TclError, ValueError):
            interval, retention = 5, 60
        return max(5, min(1440, retention * 60 // interval))

    def _refresh(self) -> None:
        if self._closing:
            return
        if self._monitoring and not self._refresh_inflight:
            self._refresh_inflight = True
            def sample() -> dict[str, Any]:
                snapshot = collectors.collect_system_snapshot()
                snapshot["process_memory"] = collectors.collect_process_memory()
                return snapshot
            self._submit_job("snapshot", sample)
        try:
            delay = max(5, min(60, int(self.interval.get()))) * 1000
        except (tk.TclError, ValueError):
            delay = 5000
        self.root.after(delay, self._refresh)

    def _submit_job(self, event_name: str, function: Callable[[], Any]) -> None:
        def execute() -> None:
            try:
                self._events.put((event_name, function()))
            except Exception as exc:
                _LOGGER.exception("Falló la operación de diagnóstico %s", event_name)
                self._events.put((f"{event_name}_error", f"{type(exc).__name__}: {exc}"))
        self._executor.submit(execute)

    def _poll_events(self) -> None:
        if self._closing:
            return
        while True:
            try:
                name, value = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                if name == "snapshot":
                    self._refresh_inflight = False
                    self._accept_snapshot(value)
                elif name == "processes":
                    self._processes = analyze_processes(value)
                    self._process_memory = [
                        {
                            "pid": item.get("pid"),
                            "name": item.get("name"),
                            "path": item.get("path"),
                            "create_time": item.get("create_time"),
                            "memory_bytes": item.get("memory_bytes"),
                            "memory_percent_total": item.get("memory_percent_total"),
                        }
                        for item in self._processes
                    ]
                    self._memory_trends = analyze_memory_trends(self._history)
                    self._render_processes()
                    self._render_memory()
                    self.status_text.set(f"Inventario de {len(value)} procesos actualizado.")
                elif name == "startup":
                    self._startup = analyze_startup(value)
                    self._render_audit()
                    self.status_text.set(f"Revisión de {len(value)} servicios, tareas y entradas completada.")
                elif name == "full":
                    self._snapshot, raw_processes, raw_startup = value
                    self._snapshot["process_memory"] = [
                        {
                            "pid": item.get("pid"),
                            "name": item.get("name"),
                            "create_time": item.get("create_time"),
                            "memory_bytes": item.get("memory_bytes"),
                            "memory_percent_total": item.get("memory_percent_total"),
                        }
                        for item in raw_processes
                    ]
                    self._accept_snapshot(self._snapshot)
                    self._processes = analyze_processes(raw_processes)
                    self._startup = analyze_startup(raw_startup)
                    self._render_processes()
                    self._render_audit()
                    self._make_report()
                    self.status_text.set(f"Diagnóstico completo: {self._report['summary']['status']}.")
                elif name == "benchmark_progress":
                    progress, temperature = value
                    self.benchmark_progress.configure(value=progress)
                    if temperature is not None:
                        self.benchmark_text.set(f"Prueba activa. Temperatura observada: {temperature:.1f} °C")
                elif name == "benchmark":
                    self._benchmarks.append(value)
                    self.benchmark_button.configure(state="normal")
                    self.cancel_benchmark_button.configure(state="disabled")
                    self._benchmark_stop = None
                    self.benchmark_progress.configure(value=1 if value.get("completed") else 0)
                    self.benchmark_text.set(json.dumps(value, ensure_ascii=False))
                    self._make_report()
                elif name == "persist_error":
                    _LOGGER.error("No se pudo guardar historial: %s", value)
                elif name.endswith("_error"):
                    if name.startswith("snapshot"):
                        self._refresh_inflight = False
                    self.status_text.set(f"No se pudo completar {name[:-6]}: {value}")
                    if name.startswith("benchmark"):
                        self.benchmark_button.configure(state="normal")
                        self.cancel_benchmark_button.configure(state="disabled")
                    if name.startswith("full"):
                        messagebox.showerror("Diagnóstico fallido", str(value), parent=self.root)
            except Exception:
                _LOGGER.exception("Error mostrando el resultado %s", name)
        self.root.after(100, self._poll_events)

    def _accept_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self._process_memory = snapshot.get("process_memory", [])
        self._history.append(snapshot)
        self._history = self._history[-self._history_limit():]
        self._persist_history_snapshot(snapshot)
        self._memory_trends = analyze_memory_trends(self._history)
        self._render_snapshot()
        self._render_memory()
        self._render_processes()
        self._check_temperature_alert(snapshot)

    def _persist_history_snapshot(self, snapshot: dict[str, Any]) -> None:
        try:
            with self._history_path.open("a", encoding="utf-8") as history_file:
                history_file.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            if self._history_path.stat().st_size > _MAX_HISTORY_FILE_BYTES:
                temporary = self._history_path.with_suffix(".jsonl.tmp")
                with temporary.open("w", encoding="utf-8") as history_file:
                    for record in self._history:
                        history_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                temporary.replace(self._history_path)
        except OSError as exc:
            self._events.put(("persist_error", str(exc)))

    def _render_snapshot(self) -> None:
        if not self._snapshot:
            return
        snapshot = self._snapshot
        cpu = snapshot["cpu"]
        memory = snapshot["memory"]
        gpu = snapshot.get("gpus", [])
        gpu_labels = []
        for item in gpu:
            label = item.get("name", "GPU")
            if item.get("utilization_percent") is not None:
                label += f" {item['utilization_percent']:.0f}%"
            if item.get("temperature_c") is not None:
                label += f" {item['temperature_c']:.0f} °C"
            vram_total = item.get("vram_total_mb")
            vram_used = item.get("vram_used_mb")
            if vram_total is not None and vram_used is not None:
                label += f" VRAM {vram_used:.0f}/{vram_total:.0f} MiB"
            elif item.get("adapter_memory_bytes") is not None:
                label += (
                    f" memoria de adaptador reportada {item['adapter_memory_bytes'] / 1024**2:.0f} MiB "
                    "(sin distinguir dedicada/compartida)"
                )
            if item.get("power_watts") is not None:
                label += f" {item['power_watts']:.1f} W"
            gpu_labels.append(label)
        gpu_text = "; ".join(gpu_labels) or "No detectada"
        disks = snapshot.get("disks", [])
        disk_text = ", ".join(f"{item.get('mountpoint')}: {item.get('percent')}%" for item in disks[:3]) or "No disponible"
        frequency = cpu.get("frequency_mhz")
        frequency_text = (
            f"{frequency:.0f} MHz" if isinstance(frequency, (int, float))
            else "frecuencia N/D"
        )
        disk_rate = "lectura/escritura: N/D"
        counters = snapshot.get("disk_activity")
        now = datetime.now().timestamp()
        if counters:
            previous = self._previous_disk_counters
            if previous and now > previous[2]:
                elapsed = now - previous[2]
                read_rate = max(0, counters["read_bytes"] - previous[0]) / elapsed / 1024**2
                write_rate = max(0, counters["write_bytes"] - previous[1]) / elapsed / 1024**2
                disk_rate = f"lectura {read_rate:.1f} / escritura {write_rate:.1f} MiB/s"
            self._previous_disk_counters = (counters["read_bytes"], counters["write_bytes"], now)
        self.metrics.set(
            f"CPU: {cpu.get('utilization_percent', 0):.0f}% / {frequency_text} / "
            f"{cpu.get('physical_cores') or 'N/D'} núcleos, {cpu.get('logical_threads') or 'N/D'} hilos  |  "
            f"RAM: {memory.get('percent', 0):.0f}% ({self._format_bytes(memory.get('available_bytes'))} libre)  |  "
            f"GPU: {gpu_text}  |  Disco: {disk_text} ({disk_rate})"
        )
        sensors = snapshot.get("temperatures", [])
        if sensors:
            summaries = {
                item["identifier"]: item
                for item in summarize_temperatures(self._history)
            }
            temperature_labels = []
            for item in sensors:
                if not isinstance(item.get("celsius"), (int, float)):
                    continue
                summary = summaries.get(item.get("identifier"))
                current = item["celsius"]
                if summary:
                    temperature_labels.append(
                        f"{item.get('name')}: {current:.1f} °C "
                        f"(mín {summary['minimum_celsius']:.1f} / máx {summary['maximum_celsius']:.1f})"
                    )
                else:
                    temperature_labels.append(f"{item.get('name')}: {current:.1f} °C")
            self.temperature_text.set("Temperaturas reales: " + "  |  ".join(
                temperature_labels
            ) + f" [{sensors[0].get('source', 'sensor')}]")
        else:
            self.temperature_text.set(f"Temperaturas no disponibles: {snapshot.get('temperature_note') or 'el hardware no expone sensores accesibles.'}")
        health = []
        for disk in disks:
            if disk.get("percent", 0) >= 90:
                health.append(f"Disco {disk.get('mountpoint')} casi lleno ({disk['percent']:.0f}%).")
        if memory.get("percent", 0) >= 90:
            health.append("Consumo de RAM superior al 90%.")
        if cpu.get("utilization_percent", 0) >= 90:
            health.append("Uso de CPU superior al 90%.")
        battery = snapshot.get("battery")
        if battery is not None:
            wear = battery.get("wear_percent")
            wear_text = f"; desgaste estimado {wear:.1f}%" if isinstance(wear, (int, float)) else "; desgaste no expuesto"
            design = battery.get("design_capacity_mwh")
            full = battery.get("full_charge_capacity_mwh")
            cycles = battery.get("cycle_count")
            battery_temp = battery.get("temperature_c")
            capacity_text = (
                f"; capacidad diseño/carga completa {design:.0f}/{full:.0f} mWh"
                if isinstance(design, (int, float)) and isinstance(full, (int, float))
                else "; capacidad diseño/carga completa no disponible"
            )
            cycle_text = f"; ciclos {cycles:.0f}" if isinstance(cycles, (int, float)) else "; ciclos no disponibles"
            battery_temp_text = (
                f"; temperatura {battery_temp:.1f} °C"
                if isinstance(battery_temp, (int, float))
                else "; temperatura no disponible"
            )
            plugged = battery.get("plugged")
            power_state = (
                "conectada" if plugged is True
                else "en batería" if plugged is False
                else "estado de alimentación N/D"
            )
            health.append(
                f"Batería: {battery.get('percent') if battery.get('percent') is not None else 'N/D'}% ({power_state})"
                f"{wear_text}{capacity_text}{cycle_text}{battery_temp_text}."
            )
        self.health_text.set("Estado actual: " + ("; ".join(health) if health else "sin umbrales de alerta superados en esta muestra."))
        self._draw_chart()

    @staticmethod
    def _format_bytes(value: int | float | None) -> str:
        if value is None:
            return "no disponible"
        return f"{value / 1024**3:.1f} GiB"

    def _draw_chart(self) -> None:
        canvas = self.chart
        canvas.delete("all")
        width = max(300, canvas.winfo_width())
        height = max(120, canvas.winfo_height())
        left, top, right, bottom = 45, 15, width - 15, height - 35
        canvas.create_line(left, top, left, bottom, fill="#8c959f")
        canvas.create_line(left, bottom, right, bottom, fill="#8c959f")
        for percent in (0, 50, 100):
            y = bottom - (bottom - top) * percent / 100
            canvas.create_line(left, y, right, y, fill="#eaeef2")
            canvas.create_text(left - 8, y, text=str(percent), anchor="e", fill="#57606a")
        rows = self._history[-self._history_limit():]
        if len(rows) < 2:
            canvas.create_text((left + right) / 2, (top + bottom) / 2, text="Historial en recopilación...", fill="#57606a")
            return
        for key, color, label in (
            ("cpu", "#0969da", "CPU %"),
            ("memory", "#8250df", "RAM %"),
        ):
            points = []
            for index, row in enumerate(rows):
                group = row.get(key, {})
                value = group.get("utilization_percent") if key == "cpu" else group.get("percent")
                if isinstance(value, (int, float)):
                    x = left + index * (right - left) / (len(rows) - 1)
                    y = bottom - max(0, min(100, value)) * (bottom - top) / 100
                    points.extend((x, y))
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2)
            canvas.create_text(right - 5, top + (0 if key == "cpu" else 16), text=label, anchor="e", fill=color)
        temp_rows = [row for row in rows if row.get("temperatures")]
        if temp_rows:
            values = [max((s.get("celsius") for s in row.get("temperatures", []) if isinstance(s.get("celsius"), (int, float))), default=None) for row in rows]
            points = []
            for index, value in enumerate(values):
                if value is not None:
                    x = left + index * (right - left) / (len(rows) - 1)
                    y = bottom - max(0, min(100, value)) * (bottom - top) / 100
                    points.extend((x, y))
            if len(points) >= 4:
                canvas.create_line(*points, fill="#cf222e", width=2)
            canvas.create_text(right - 5, top + 32, text="Temperatura °C (escala 0–100)", anchor="e", fill="#cf222e")

    def _check_temperature_alert(self, snapshot: dict[str, Any]) -> None:
        threshold = self.threshold.get()
        hot = [
            item for item in snapshot.get("temperatures", [])
            if isinstance(item.get("celsius"), (int, float)) and item["celsius"] >= threshold
        ]
        if hot and not self._temp_alert_sent:
            self._temp_alert_sent = True
            names = ", ".join(f"{item.get('name')}: {item['celsius']:.1f} °C" for item in hot)
            self.status_text.set(f"ALERTA térmica: {names} (umbral {threshold:.0f} °C).")
            _LOGGER.warning("Umbral térmico superado: %s", names)
        elif not hot:
            self._temp_alert_sent = False

    def _toggle_monitoring(self) -> None:
        self._monitoring = not self._monitoring
        self.monitor_button.configure(text="Pausar monitoreo" if self._monitoring else "Reanudar monitoreo")
        self.status_text.set("Monitoreo periódico activo." if self._monitoring else "Monitoreo periódico pausado.")

    def _run_full_diagnostic(self) -> None:
        self.status_text.set("Diagnóstico completo en ejecución; la interfaz permanece disponible...")
        def collect() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
            snapshot = collectors.collect_system_snapshot()
            processes = collectors.collect_processes()
            snapshot["process_memory"] = [
                {
                    "pid": process.get("pid"),
                    "name": process.get("name"),
                    "create_time": process.get("create_time"),
                    "memory_bytes": process.get("memory_bytes"),
                    "memory_percent_total": process.get("memory_percent_total"),
                }
                for process in processes
            ]
            return snapshot, processes, collectors.collect_startup_audit()
        self._submit_job("full", collect)

    def _scan_processes(self) -> None:
        self.status_text.set("Consultando procesos, rutas, usuarios, conexiones y firmas...")
        self._submit_job("processes", collectors.collect_processes)

    def _scan_startup(self) -> None:
        self.status_text.set("Consultando servicios, entradas de inicio y tareas programadas...")
        self._submit_job("startup", collectors.collect_startup_audit)

    def _render_processes(self) -> None:
        self.process_table.delete(*self.process_table.get_children())
        trends = {
            (item["pid"], item["create_time"]): item
            for item in self._memory_trends
        }
        for item in self._processes:
            memory = item.get("memory_bytes")
            signature = item.get("signature") or {}
            trend = trends.get((item.get("pid"), item.get("create_time")))
            manufacturer = signature.get("manufacturer") or "No disponible"
            signer = signature.get("signer")
            if signer and signer != manufacturer:
                manufacturer += f" | {signer}"
            version = signature.get("file_version") or signature.get("product_version") or "N/D"
            if signature.get("certificate_issuer"):
                manufacturer += f" | cert. {signature['certificate_issuer']}"
            if signature.get("certificate_expires"):
                manufacturer += f" | hasta {signature['certificate_expires'][:10]}"
            self.process_table.insert(
                "", "end",
                values=(
                    item.get("risk"), item.get("name"), item.get("pid"),
                    f"{item.get('cpu_percent', 0):.1f}",
                    f"{memory / 1024**2:.1f}" if memory else "N/D",
                    f"{item['memory_percent_total']:.2f}" if isinstance(item.get("memory_percent_total"), (int, float)) else "N/D",
                    item.get("user") or "N/D", self._format_duration(item.get("runtime_seconds")),
                    item.get("started_at") or "N/D",
                    signature.get("status", "No verificada"),
                    manufacturer,
                    version,
                    ", ".join(item.get("network_endpoints", [])) or "—",
                    item.get("path") or "No disponible",
                    "; ".join(item.get("reasons", [])) + (
                        f"; RAM: +{trend['growth_mib_per_minute']:.1f} MiB/min (posible tendencia, no confirma fuga)"
                        if trend else ""
                    ),
                ),
                tags=(item.get("risk", ""),),
            )

    def _render_memory(self) -> None:
        self.memory_table.delete(*self.memory_table.get_children())
        total = self._snapshot.get("memory", {}).get("total_bytes") if self._snapshot else None
        used = self._snapshot.get("memory", {}).get("percent") if self._snapshot else None
        rss_total = sum(item.get("memory_bytes") or 0 for item in self._process_memory)
        coverage = (
            f"; suma RSS {rss_total / total * 100:.1f}% (puede contar páginas compartidas)"
            if isinstance(total, (int, float)) and total > 0 else ""
        )
        used_text = f"RAM del sistema: {used:.1f}% utilizada" if isinstance(used, (int, float)) else "RAM del sistema: N/D"
        self.memory_summary.set(f"{used_text}{coverage}")
        self._memory_item_keys.clear()
        known_paths = {
            (item.get("pid"), item.get("create_time")): item.get("path")
            for item in self._processes
        }
        trend_by_key = {
            (item["pid"], item["create_time"]): item
            for item in self._memory_trends
        }
        for item in rank_process_memory(self._process_memory)[:100]:
            trend = trend_by_key.get((item.get("pid"), item.get("create_time")))
            item_id = self.memory_table.insert(
                "", "end",
                values=(
                    item.get("name"),
                    item.get("pid"),
                    f"{item['memory_bytes'] / 1024**2:.1f}" if item.get("memory_bytes") is not None else "N/D",
                    f"{item['memory_percent_total']:.2f}" if isinstance(item.get("memory_percent_total"), (int, float)) else "N/D",
                    (
                        f"+{trend['growth_mib_per_minute']:.1f} MiB/min; {trend['classification']}"
                        if trend else "Sin tendencia suficiente"
                    ),
                    known_paths.get((item.get("pid"), item.get("create_time"))) or "N/D",
                ),
                tags=("crecimiento",) if trend else (),
            )
            if isinstance(item.get("pid"), int) and isinstance(item.get("create_time"), (int, float)):
                self._memory_item_keys[item_id] = (item["pid"], float(item["create_time"]))
        self._draw_memory_chart()

    def _select_memory_process(self, _event: object = None) -> None:
        selected = self.memory_table.selection()
        if not selected:
            return
        self._selected_memory_key = self._memory_item_keys.get(selected[0])
        self._draw_memory_chart()

    def _draw_memory_chart(self) -> None:
        canvas = self.memory_chart
        canvas.delete("all")
        width = max(300, canvas.winfo_width())
        height = max(120, canvas.winfo_height())
        left, top, right, bottom = 55, 12, width - 15, height - 25
        canvas.create_line(left, top, left, bottom, fill="#8c959f")
        canvas.create_line(left, bottom, right, bottom, fill="#8c959f")
        selected_key = getattr(self, "_selected_memory_key", None)
        if selected_key is None:
            canvas.create_text((left + right) / 2, (top + bottom) / 2, text="Selecciona un proceso para ver su evolución de RAM.")
            return
        pid, create_time = selected_key
        rows = self._history
        points = []
        readings = []
        for row in rows:
            process = next(
                (
                    item for item in row.get("process_memory", [])
                    if item.get("pid") == pid and item.get("create_time") == create_time
                ),
                None,
            )
            if process and isinstance(process.get("memory_bytes"), (int, float)):
                readings.append((row.get("captured_at"), process["memory_bytes"] / 1024**2))
        if len(readings) < 2:
            canvas.create_text((left + right) / 2, (top + bottom) / 2, text="Aún no hay suficientes muestras de este proceso.")
            return
        low = min(value for _when, value in readings)
        high = max(value for _when, value in readings)
        span = max(1.0, high - low)
        for index, (_when, value) in enumerate(readings):
            x = left + index * (right - left) / (len(readings) - 1)
            y = bottom - (value - low) * (bottom - top) / span
            points.extend((x, y))
        canvas.create_line(*points, fill="#8250df", width=2)
        canvas.create_text(left, top, text=f"PID {pid}: {low:.1f}–{high:.1f} MiB (RSS)", anchor="w", fill="#8250df")
        canvas.create_text(right, bottom + 12, text=f"{len(readings)} muestras / {self.history_minutes.get()} min", anchor="e")

    def _render_audit(self) -> None:
        self.audit_table.delete(*self.audit_table.get_children())
        for item in self._startup:
            self.audit_table.insert(
                "", "end",
                values=(
                    "Duplicada" if item.get("duplicate") else item.get("risk"),
                    item.get("kind"), item.get("name"), item.get("state"),
                    item.get("user"), item.get("location"),
                    "; ".join(item.get("reasons", [])),
                ),
                tags=(
                    "advertencia" if item.get("risk") == "advertencia"
                    else "medio" if item.get("risk") == "medio" or item.get("duplicate")
                    else "",
                ),
            )

    @staticmethod
    def _sort_table(table: ttk.Treeview, column: str, reverse: bool) -> None:
        values = [(table.set(item, column), item) for item in table.get_children("")]
        def key(row: tuple[str, str]) -> tuple[int, Any]:
            value = row[0]
            try:
                return (0, float(value))
            except (TypeError, ValueError):
                return (1, value.casefold())
        for index, (_, item) in enumerate(sorted(values, key=key, reverse=reverse)):
            table.move(item, "", index)
        table.heading(column, command=lambda: DiagnosticsApp._sort_table(table, column, not reverse))

    def _start_benchmark(self) -> None:
        try:
            mode = self.benchmark_mode.get()
            duration = int(self.benchmark_duration.get())
            max_cpu = int(self.benchmark_cpu.get())
            size = int(self.benchmark_size.get())
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("Parámetros inválidos", str(exc), parent=self.root)
            return
        self._benchmark_stop = threading.Event()
        temperature_limit = float(self.threshold.get())
        self.benchmark_button.configure(state="disabled")
        self.cancel_benchmark_button.configure(state="normal")
        self.benchmark_progress.configure(value=0)
        self.benchmark_text.set("Prueba en ejecución; límite térmico activado cuando hay sensores.")
        def progress(value: float, temperature: float | None) -> None:
            self._events.put(("benchmark_progress", (value, temperature)))
        def execute() -> dict[str, Any]:
            return run_benchmark(
                mode, duration, max_cpu, size, size, self._benchmark_stop,
                progress, temperature_limit,
            )
        self._submit_job("benchmark", execute)

    def _cancel_benchmark(self) -> None:
        if self._benchmark_stop:
            self._benchmark_stop.set()
            self.benchmark_text.set("Cancelación solicitada...")

    def _make_report(self) -> None:
        if not self._snapshot:
            return
        comparison = (
            compare_to_baseline(self._snapshot, self._comparison_baseline)
            if self._comparison_baseline else None
        )
        self._report = build_report(
            self._snapshot, self._processes, self._startup, self._benchmarks,
            self._history, comparison,
        )
        content = json.dumps(self._report, ensure_ascii=False, indent=2)
        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", "end")
        self.report_text.insert("1.0", content)
        self.report_text.configure(state="disabled")

    def _choose_comparison_report(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Seleccionar informe anterior",
            filetypes=[("Informes TXT o JSON", "*.txt *.json"), ("Todos los archivos", "*.*")],
        )
        if not path:
            return
        try:
            self._comparison_baseline = load_comparison_baseline(path)
            self.status_text.set(f"Informe anterior cargado para comparación: {path}")
            self._make_report()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _LOGGER.exception("No se pudo leer el informe anterior")
            messagebox.showerror("Informe inválido", str(exc), parent=self.root)

    def _export(self, file_format: str) -> None:
        if not self._report:
            if not self._snapshot:
                messagebox.showinfo("Informe pendiente", "Espere la primera lectura o ejecute un diagnóstico completo.", parent=self.root)
                return
        self._make_report()
        extension = file_format
        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title="Exportar informe",
            defaultextension=f".{extension}",
            filetypes=[(f"Informe {extension.upper()}", f"*.{extension}")],
            initialfile=f"diagnostico-windows-{datetime.now():%Y%m%d-%H%M%S}.{extension}",
        )
        if not destination:
            return
        try:
            export_report(self._report or {}, destination, file_format)
            self.status_text.set(f"Informe exportado: {destination}")
        except OSError as exc:
            _LOGGER.exception("No se pudo exportar el informe")
            messagebox.showerror("Error al exportar", str(exc), parent=self.root)

    def _close(self) -> None:
        self._closing = True
        if self._benchmark_stop:
            self._benchmark_stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None:
            return "N/D"
        seconds = max(0, int(seconds))
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours:02}:{minutes:02}" if days else f"{hours:02}:{minutes:02}"


def main() -> None:
    root = tk.Tk()
    DiagnosticsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
