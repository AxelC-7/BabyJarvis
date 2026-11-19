from __future__ import annotations

import math
import os
import tempfile
import threading
import time
from contextlib import nullcontext
from typing import Callable

import psutil
from . import collectors

ProgressCallback = Callable[[float, float | None], None]


def run_benchmark(
    mode: str = "cpu",
    duration_seconds: int = 10,
    max_cpu_percent: int = 50,
    memory_mb: int = 64,
    disk_mb: int = 64,
    stop_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
    temperature_limit_c: float = 90,
) -> dict[str, object]:
    """Run bounded, cancellable microbenchmarks; never deliberately exceed moderate load."""
    if mode not in {"cpu", "memory", "disk", "stability"}:
        raise ValueError("Modo de prueba no reconocido.")
    if not 1 <= duration_seconds <= 30:
        raise ValueError("La duración debe estar entre 1 y 30 segundos.")
    if not 10 <= max_cpu_percent <= 70:
        raise ValueError("La carga máxima permitida debe estar entre 10% y 70%.")
    if not 1 <= memory_mb <= 256:
        raise ValueError("La prueba de memoria está limitada a 256 MB.")
    if not 1 <= disk_mb <= 256:
        raise ValueError("La prueba de disco está limitada a 256 MB.")
    stop_event = stop_event or threading.Event()
    started = time.monotonic()
    deadline = started + duration_seconds
    warnings: list[str] = []
    result: dict[str, object] = {
        "mode": mode,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "requested_duration_seconds": duration_seconds,
        "max_cpu_percent": max_cpu_percent,
        "completed": False,
        "cancelled": False,
        "warnings": warnings,
    }
    work_units = 0
    disk_path: str | None = None
    memory_buffer: bytearray | None = None
    last_temperature_check = 0.0
    temperature: float | None = None
    try:
        if mode == "memory":
            memory_buffer = bytearray(memory_mb * 1024 * 1024)
        if mode == "disk":
            handle = tempfile.NamedTemporaryFile(prefix="jarvis-diagnostic-", delete=False)
            disk_path = handle.name
            handle.close()
            result["disk_test_path"] = "archivo temporal, eliminado al finalizar"

        with (open(disk_path, "w+b") if disk_path else nullcontext()) as disk_file:
            while time.monotonic() < deadline and not stop_event.is_set():
                tick_start = time.monotonic()
                if mode in {"cpu", "stability"}:
                    work_deadline = tick_start + 0.1
                    while time.monotonic() < work_deadline:
                        work_units += math.isqrt(work_units + 1) + 1
                    active = min(0.7, max_cpu_percent / 100)
                    stop_event.wait(0.1 * (1 - active) / active)
                elif mode == "memory" and memory_buffer is not None:
                    block = bytes(range(256)) * 4096
                    offset = work_units % len(memory_buffer)
                    amount = min(len(block), len(memory_buffer) - offset)
                    memory_buffer[offset : offset + amount] = block[:amount]
                    work_units += amount
                elif mode == "disk" and disk_file:
                    block = bytes(1024 * 1024)
                    remaining = disk_mb * 1024 * 1024 - disk_file.tell()
                    disk_file.write(block[: min(len(block), remaining)])
                    disk_file.flush()
                    if disk_file.tell() >= disk_mb * 1024 * 1024:
                        os.fsync(disk_file.fileno())
                        write_bytes = disk_file.tell()
                        disk_file.seek(0)
                        read_bytes = 0
                        while read_bytes < write_bytes and time.monotonic() < deadline and not stop_event.is_set():
                            chunk = disk_file.read(min(len(block), write_bytes - read_bytes))
                            if not chunk:
                                break
                            read_bytes += len(chunk)
                        work_units = write_bytes
                        result["disk_read_complete"] = read_bytes == write_bytes
                        if read_bytes < write_bytes:
                            warnings.append(
                                "La lectura se detuvo al alcanzar el límite de tiempo o al cancelar."
                            )
                        result["disk_mib_read"] = round(read_bytes / 1024**2, 2)
                        result["disk_mib_read_per_second"] = round(
                            read_bytes / 1024**2 / max(0.001, time.monotonic() - started), 2
                        )
                        break
                elapsed = time.monotonic() - started
                if time.monotonic() - last_temperature_check >= 1:
                    temperature = _current_max_temperature()
                    last_temperature_check = time.monotonic()
                    if temperature is not None and temperature >= temperature_limit_c:
                        warnings.append(
                            f"Prueba detenida: temperatura observada de {temperature:.1f} °C "
                            f"alcanzó el límite de {temperature_limit_c:.1f} °C."
                        )
                        stop_event.set()
                if on_progress:
                    on_progress(min(1.0, elapsed / duration_seconds), temperature)
                if mode == "memory":
                    stop_event.wait(0.02)
        elapsed = max(0.001, time.monotonic() - started)
        result.update(
            {
                "elapsed_seconds": round(elapsed, 2),
                "work_units": work_units,
                "completed": not stop_event.is_set(),
                "cancelled": stop_event.is_set(),
            }
        )
        if mode == "disk":
            result["disk_mib_written"] = round(work_units / 1024**2, 2)
            result["disk_mib_per_second"] = round(work_units / 1024**2 / elapsed, 2)
        elif mode == "memory":
            result["memory_mib_touched"] = round(work_units / 1024**2, 2)
            result["memory_mib_per_second"] = round(work_units / 1024**2 / elapsed, 2)
        elif mode in {"cpu", "stability"}:
            result["cpu_work_units"] = work_units
        return result
    finally:
        memory_buffer = None
        if disk_path:
            try:
                os.remove(disk_path)
            except FileNotFoundError:
                pass


def _current_max_temperature() -> float | None:
    sensors, _note = collectors._read_temperatures()
    values = [sensor["celsius"] for sensor in sensors if isinstance(sensor.get("celsius"), (int, float))]
    return max(values) if values else None
