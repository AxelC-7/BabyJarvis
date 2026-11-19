from __future__ import annotations

import ntpath
import os
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any


_SYSTEM_EXECUTABLES = {
    "svchost.exe",
    "csrss.exe",
    "lsass.exe",
    "services.exe",
    "winlogon.exe",
    "smss.exe",
    "wininit.exe",
    "spoolsv.exe",
    "taskhostw.exe",
    "dwm.exe",
    "explorer.exe",
    "runtimebroker.exe",
}
_SUSPICIOUS_DIRS = (
    "\\temp\\",
    "\\tmp\\",
    "\\downloads\\",
    "\\recycle.bin\\",
    "\\appdata\\local\\temp\\",
)
_WINDOWS_NAMES = tuple(sorted(_SYSTEM_EXECUTABLES))


def _system_name_lookalike(filename: str) -> str | None:
    candidate = ntpath.basename(filename).casefold()
    stem, extension = ntpath.splitext(candidate)
    if extension != ".exe" or not stem:
        return None
    normalized = stem.translate(str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s"}))
    for legitimate in _WINDOWS_NAMES:
        target = ntpath.splitext(legitimate)[0]
        if stem != target and SequenceMatcher(None, normalized, target).ratio() >= 0.82:
            return legitimate
    return None


def _location_risk(path: str | None) -> tuple[str | None, str | None]:
    if not path:
        return "informativo", "Windows no permitió consultar la ruta del ejecutable."
    normalized = os.path.expandvars(path).replace("/", "\\").casefold()
    filename = ntpath.basename(normalized)
    windir = os.path.expandvars(os.environ.get("windir", "C:\\Windows")).replace("/", "\\").casefold()
    if filename in _SYSTEM_EXECUTABLES and not (
        normalized.startswith(windir + "\\")
        or normalized.startswith("\\\\?\\")
    ):
        return "alto", "El nombre imita un componente de Windows, pero la ruta no está en Windows."
    similar_name = _system_name_lookalike(filename)
    if similar_name and not normalized.startswith(windir + "\\"):
        return "alto", f"El nombre se parece a {similar_name}, pero se ejecuta desde una ruta externa a Windows."
    if any(part in normalized for part in _SUSPICIOUS_DIRS):
        return "medio", "El ejecutable se inicia desde una carpeta temporal, descargas o reciclaje."
    return None, None


def classify_process(process: dict[str, Any]) -> dict[str, Any]:
    """Assign explainable risk indicators; an unknown or unsigned process is not malware."""
    signals: list[tuple[str, str]] = []
    level = "informativo"
    location_level, location_reason = _location_risk(process.get("path"))
    if location_level and location_reason:
        signals.append((location_level, location_reason))
        if location_level == "alto":
            level = "alto"
        elif location_level == "medio":
            level = "medio"
    normalized_path = (process.get("path") or "").replace("/", "\\").casefold()
    if any(part in normalized_path for part in _SUSPICIOUS_DIRS) and not any(
        reason.startswith("El ejecutable se inicia") for _, reason in signals
    ):
        signals.append(("medio", "El ejecutable también se encuentra en una carpeta temporal o de descargas."))

    cpu = process.get("cpu_percent")
    if isinstance(cpu, (int, float)) and cpu >= 85:
        signals.append(("advertencia", f"Consumo elevado de CPU ({cpu:.1f}%)."))
    memory = process.get("memory_bytes")
    if isinstance(memory, (int, float)) and memory >= 1024**3:
        signals.append(("advertencia", f"Uso de memoria elevado ({memory / 1024**3:.1f} GiB)."))
    endpoints = process.get("network_endpoints") or []
    if len(endpoints) > 10:
        signals.append(
            ("informativo", f"Se observaron {len(endpoints)} conexiones remotas; no se evalúa su reputación ni legitimidad.")
        )
    if endpoints and any(part in normalized_path for part in _SUSPICIOUS_DIRS):
        signals.append(
            ("advertencia", "El proceso se ejecuta desde una ubicación inusual y mantiene conexiones remotas; no se evaluó la reputación de los destinos.")
        )

    signature = process.get("signature") or {}
    signature_status = str(signature.get("status", "No verificada")).casefold()
    path = process.get("path")
    normalized = (path or "").replace("/", "\\").casefold()
    in_system_path = normalized.startswith(
        os.environ.get("windir", "C:\\Windows").replace("/", "\\").casefold() + "\\"
    )
    unsigned_or_invalid = signature_status in {"notsigned", "hashmismatch", "nottrusted", "error"}
    if unsigned_or_invalid and path and not in_system_path:
        if any(
            reason.startswith(("El ejecutable se inicia", "El ejecutable también", "El nombre se parece"))
            for _, reason in signals
        ):
            signals.append(
                ("medio", "La firma no es válida o no existe y el ejecutable está en una ubicación inusual.")
            )
            level = "alto" if level == "alto" else "medio"
    if not path:
        signals.append(("informativo", "Ruta o firma no verificable con los permisos actuales."))
    if not signals:
        signals.append(("informativo", "No se observaron indicadores destacados en esta revisión."))
    return {
        "risk": level,
        "reasons": [reason for _, reason in signals],
        "signals": [{"level": item_level, "reason": reason} for item_level, reason in signals],
    }


def analyze_processes(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    analyzed = []
    for process in processes:
        item = dict(process)
        item.update(classify_process(item))
        analyzed.append(item)
    return sorted(
        analyzed,
        key=lambda item: (
            {"alto": 0, "medio": 1, "informativo": 2}.get(item["risk"], 3),
            -(item.get("cpu_percent") or 0),
        ),
    )


def rank_process_memory(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        processes,
        key=lambda item: item.get("memory_bytes") or 0,
        reverse=True,
    )


def analyze_memory_trends(
    history: list[dict[str, Any]],
    *,
    minimum_samples: int = 6,
    minimum_duration_seconds: int = 60,
    minimum_growth_bytes: int = 16 * 1024**2,
    minimum_slope_mib_per_minute: float = 8.0,
) -> list[dict[str, Any]]:
    """Find sustained growth patterns; a trend is not proof of a memory leak."""
    by_process: dict[tuple[int, float], list[tuple[float, str, int]]] = {}
    for record in history:
        timestamp = record.get("captured_at")
        try:
            when = datetime.fromisoformat(str(timestamp)).timestamp()
        except (TypeError, ValueError, OSError):
            continue
        for sample in record.get("process_memory", []):
            pid = sample.get("pid")
            created = sample.get("create_time")
            memory_bytes = sample.get("memory_bytes")
            if not isinstance(pid, int) or not isinstance(created, (int, float)):
                continue
            if not isinstance(memory_bytes, (int, float)):
                continue
            key = (pid, float(created))
            by_process.setdefault(key, []).append((when, str(sample.get("name") or pid), int(memory_bytes)))

    trends = []
    for (pid, created), samples in by_process.items():
        samples.sort(key=lambda item: item[0])
        if len(samples) < minimum_samples:
            continue
        first_time, name, first_memory = samples[0]
        last_time, _last_name, last_memory = samples[-1]
        duration = last_time - first_time
        growth = last_memory - first_memory
        if duration < minimum_duration_seconds or growth < minimum_growth_bytes:
            continue
        mean_time = sum(sample[0] for sample in samples) / len(samples)
        mean_memory = sum(sample[2] for sample in samples) / len(samples)
        variance = sum((sample[0] - mean_time) ** 2 for sample in samples)
        if variance <= 0:
            continue
        slope = (
            sum((sample[0] - mean_time) * (sample[2] - mean_memory) for sample in samples)
            / variance
            * 60
            / 1024**2
        )
        rising_steps = sum(
            1 for previous, current in zip(samples, samples[1:])
            if current[2] > previous[2]
        )
        if slope < minimum_slope_mib_per_minute or rising_steps < (len(samples) - 1) * 0.7:
            continue
        trends.append(
            {
                "pid": pid,
                "create_time": created,
                "name": name,
                "samples": len(samples),
                "duration_seconds": round(duration, 1),
                "start_memory_bytes": first_memory,
                "end_memory_bytes": last_memory,
                "growth_bytes": growth,
                "growth_mib_per_minute": round(slope, 2),
                "classification": "posible tendencia de crecimiento; no confirma una fuga de memoria",
            }
        )
    return sorted(trends, key=lambda item: item["growth_mib_per_minute"], reverse=True)


def summarize_temperatures(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[tuple[float, float, float, float]]] = {}
    for record in history:
        try:
            when = datetime.fromisoformat(str(record.get("captured_at"))).timestamp()
        except (TypeError, ValueError, OSError):
            continue
        for sensor in record.get("temperatures", []):
            value = sensor.get("celsius")
            if not isinstance(value, (int, float)):
                continue
            key = (
                str(sensor.get("identifier") or ""),
                str(sensor.get("name") or "Sensor"),
                str(sensor.get("group") or ""),
            )
            minimum = sensor.get("minimum")
            maximum = sensor.get("maximum")
            grouped.setdefault(key, []).append(
                (
                    when,
                    float(value),
                    float(minimum) if isinstance(minimum, (int, float)) else float(value),
                    float(maximum) if isinstance(maximum, (int, float)) else float(value),
                )
            )
    summaries = []
    for (identifier, name, group), values in grouped.items():
        summaries.append(
            {
                "identifier": identifier or None,
                "name": name,
                "group": group,
                "current_celsius": values[-1][1],
                "minimum_celsius": min(sample[2] for sample in values),
                "maximum_celsius": max(sample[3] for sample in values),
                "samples": len(values),
                "first_seen": datetime.fromtimestamp(values[0][0]).astimezone().isoformat(timespec="seconds"),
                "last_seen": datetime.fromtimestamp(values[-1][0]).astimezone().isoformat(timespec="seconds"),
            }
        )
    return summaries


def analyze_startup(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag entries for review without treating unknown publishers as malicious."""
    analyzed = []
    for entry in items:
        item = dict(entry)
        location = str(item.get("location") or "").casefold().replace("/", "\\")
        name = str(item.get("name") or "")
        reasons: list[str] = []
        risk = "informativo"
        if item.get("collection_error"):
            risk = "advertencia"
            reasons.append(f"Consulta incompleta: {item.get('error', 'sin detalle')}")
        elif any(part in location for part in _SUSPICIOUS_DIRS):
            reasons.append("La acción apunta a una ubicación temporal o de descargas.")
            risk = "medio"
        if not location.strip() and not item.get("collection_error"):
            reasons.append("No se pudo determinar la ruta o acción configurada.")
        if not reasons:
            reasons.append("Entrada registrada; el nombre desconocido por sí solo no implica riesgo.")
        item.update({"risk": risk, "reasons": reasons, "duplicate": False})
        analyzed.append(item)

    keys: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(analyzed):
        if item.get("collection_error"):
            continue
        key = (
            re.sub(r"\s+", " ", str(item.get("name") or "").strip().casefold()),
            re.sub(r"\s+", " ", str(item.get("location") or "").strip().casefold()),
        )
        if key != ("", ""):
            keys.setdefault(key, []).append(index)
    for indexes in keys.values():
        if len(indexes) > 1:
            for index in indexes:
                analyzed[index]["duplicate"] = True
                analyzed[index]["reasons"].append("Nombre y acción duplicados en más de una entrada.")
                analyzed[index]["risk"] = "medio"
    return analyzed


def summarize(snapshot: dict[str, Any], processes: list[dict[str, Any]], startup: list[dict[str, Any]]) -> dict[str, Any]:
    problems: list[dict[str, str]] = []
    cpu = snapshot.get("cpu", {}).get("utilization_percent")
    if isinstance(cpu, (int, float)) and cpu >= 90:
        problems.append({"severity": "advertencia", "resource": "CPU", "detail": f"Uso actual elevado: {cpu:.1f}%."})
    memory = snapshot.get("memory", {})
    if memory.get("percent", 0) >= 90:
        problems.append({"severity": "advertencia", "resource": "RAM", "detail": f"Uso actual elevado: {memory['percent']:.1f}%."})
    for disk in snapshot.get("disks", []):
        if disk.get("percent", 0) >= 90:
            problems.append({"severity": "advertencia", "resource": f"Disco {disk.get('mountpoint')}", "detail": f"Espacio utilizado: {disk['percent']:.1f}%."})
    for sensor in snapshot.get("temperatures", []):
        value = sensor.get("celsius")
        critical = sensor.get("critical")
        limit = critical if isinstance(critical, (int, float)) else 90
        if isinstance(value, (int, float)) and value >= limit:
            problems.append({"severity": "advertencia", "resource": f"Temperatura {sensor.get('name')}", "detail": f"{value:.1f} °C supera el umbral configurado de {limit:.1f} °C."})
    current_frequency = snapshot.get("cpu", {}).get("frequency_mhz")
    maximum_frequency = snapshot.get("cpu", {}).get("frequency_max_mhz")
    hottest_cpu = max(
        (
            sensor.get("celsius")
            for sensor in snapshot.get("temperatures", [])
            if isinstance(sensor.get("celsius"), (int, float))
            and any(
                token in f"{sensor.get('name', '')} {sensor.get('group', '')}".casefold()
                for token in ("cpu", "processor")
            )
        ),
        default=None,
    )
    if (
        isinstance(hottest_cpu, (int, float)) and hottest_cpu >= 85
        and isinstance(current_frequency, (int, float))
        and isinstance(maximum_frequency, (int, float)) and maximum_frequency > 0
        and current_frequency < maximum_frequency * 0.7
    ):
        problems.append({
            "severity": "posible",
            "resource": "CPU",
            "detail": "Frecuencia observada baja junto con temperatura elevada: posible throttling; correlación no confirma su causa.",
        })
    risky_processes = [item for item in processes if item.get("risk") in {"alto", "medio"}]
    if risky_processes:
        problems.append({"severity": "revisar", "resource": "Procesos", "detail": f"{len(risky_processes)} proceso(s) con señales que justifican revisión manual; no son una confirmación de malware."})
    review_entries = [
        item for item in startup
        if item.get("risk") in {"medio", "advertencia"} or item.get("duplicate")
    ]
    if review_entries:
        problems.append({"severity": "revisar", "resource": "Inicio y tareas", "detail": f"{len(review_entries)} entrada(s) con ubicación inusual, duplicada o con datos no disponibles."})
    incomplete = any(item.get("collection_error") for item in startup)
    if any(item["severity"] == "advertencia" for item in problems):
        status = "Requiere revisión"
    elif incomplete:
        status = "Información incompleta"
    elif any(item["severity"] in {"revisar", "posible"} for item in problems):
        status = "Requiere revisión"
    else:
        status = "Sin problemas confirmados"
    return {"status": status, "problems": problems}
