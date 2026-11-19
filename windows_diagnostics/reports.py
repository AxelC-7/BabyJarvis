from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .analyzer import analyze_memory_trends, rank_process_memory, summarize, summarize_temperatures


def build_report(
    snapshot: dict[str, Any],
    processes: list[dict[str, Any]],
    startup: list[dict[str, Any]],
    benchmarks: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = summarize(snapshot, processes, startup)
    history_rows = list(history or [])
    if history_rows and history_rows[-1].get("captured_at") == snapshot.get("captured_at"):
        history_rows[-1] = snapshot
    else:
        history_rows.append(snapshot)
    ranked = rank_process_memory(processes)
    total_memory = snapshot.get("memory", {}).get("total_bytes")
    total_rss = sum(
        item.get("memory_bytes") or 0
        for item in ranked
        if isinstance(item.get("memory_bytes"), (int, float))
    )
    memory_summary = {
        "total_bytes": total_memory,
        "used_percent": snapshot.get("memory", {}).get("percent"),
        "process_rss_bytes": total_rss,
        "process_rss_percent_of_total": (
            round(total_rss / total_memory * 100, 2)
            if isinstance(total_memory, (int, float)) and total_memory
            else None
        ),
        "top_processes": [
            {
                "name": item.get("name"),
                "pid": item.get("pid"),
                "path": item.get("path"),
                "memory_bytes": item.get("memory_bytes"),
                "memory_mib": round(item["memory_bytes"] / 1024**2, 1)
                if isinstance(item.get("memory_bytes"), (int, float))
                else None,
                "memory_percent_total": (
                    round(item["memory_percent_total"], 2)
                    if isinstance(item.get("memory_percent_total"), (int, float))
                    else None
                ),
                "risk": item.get("risk"),
                "signature": item.get("signature"),
                "network_endpoints": item.get("network_endpoints", []),
            }
            for item in ranked[:10]
        ],
        "growth_trends": analyze_memory_trends(history_rows),
        "note": (
            "El porcentaje individual se calcula con RSS respecto de la RAM física. "
            "La suma de RSS puede contar páginas compartidas más de una vez; una tendencia "
            "de crecimiento es una señal para investigar, no prueba de una fuga."
        ),
    }
    recommendations = []
    if summary["problems"]:
        recommendations.extend(
            [
                "Revise las advertencias y confirme su causa antes de actuar.",
                "Compruebe ventilación, aplicaciones activas y actualizaciones del fabricante si hay carga o temperatura elevadas.",
            ]
        )
    else:
        recommendations.append(
            "No se encontraron umbrales de advertencia superados en esta captura; esto no certifica la ausencia de problemas."
        )
    if memory_summary["used_percent"] is not None and memory_summary["used_percent"] >= 75:
        recommendations.append(
            "Revise la tabla de procesos ordenada por RAM y compare muestras sucesivas; el consumo alto aislado no demuestra una fuga."
        )
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summary,
        "comparison": comparison,
        "recommendations": recommendations,
        "system": snapshot,
        "processes": processes,
        "memory_analysis": memory_summary,
        "temperature_summary": summarize_temperatures(history_rows),
        "startup_audit": startup,
        "benchmarks": benchmarks or [],
        "temperature_history": [
            {
                "captured_at": item.get("captured_at"),
                "cpu": item.get("cpu"),
                "memory": item.get("memory"),
                "temperatures": item.get("temperatures"),
            }
            for item in history_rows
        ],
        "limitations": [
            "La falta de una firma o fabricante no confirma que un proceso sea malicioso.",
            "Los contadores de tráfico por proceso no están disponibles mediante las interfaces estándar utilizadas; se muestran conexiones remotas observadas.",
            "La temperatura, desgaste de batería, VRAM y sensores dependen del hardware y de sus controladores.",
            "La configuración específica de recuperación automática de servicios no se determina; la auditoría enumera servicios, tareas y entradas de inicio visibles.",
            "Los porcentajes de RAM por proceso se basan en RSS y no equivalen a memoria privada; páginas compartidas pueden aparecer en varios procesos.",
            "Los elementos señalados requieren revisión humana; esta herramienta no detiene ni modifica procesos, servicios, tareas ni el registro.",
        ],
    }


def load_comparison_baseline(path: str | Path) -> dict[str, Any]:
    """Read the comparable CPU/RAM fields from a previous JSON or TXT report."""
    report_path = Path(path)
    if report_path.suffix.casefold() == ".json":
        data = json.loads(report_path.read_text(encoding="utf-8"))
        system = data.get("system", {})
        return {
            "path": str(report_path),
            "generated_at": data.get("generated_at"),
            "cpu_percent": system.get("cpu", {}).get("utilization_percent"),
            "memory_percent": system.get("memory", {}).get("percent"),
        }
    if report_path.suffix.casefold() != ".txt":
        raise ValueError("La comparación admite informes anteriores TXT o JSON.")
    content = report_path.read_text(encoding="utf-8")
    cpu = re.search(r"(?m)^CPU:\s*([0-9]+(?:[.,][0-9]+)?)%", content)
    memory = re.search(r"(?m)^RAM:\s*([0-9]+(?:[.,][0-9]+)?)%", content)
    generated = re.search(r"(?m)^Generado:\s*(.+)$", content)
    return {
        "path": str(report_path),
        "generated_at": generated.group(1).strip() if generated else None,
        "cpu_percent": float(cpu.group(1).replace(",", ".")) if cpu else None,
        "memory_percent": float(memory.group(1).replace(",", ".")) if memory else None,
    }


def compare_to_baseline(snapshot: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    current = {
        "cpu_percent": snapshot.get("cpu", {}).get("utilization_percent"),
        "memory_percent": snapshot.get("memory", {}).get("percent"),
    }
    deltas = {}
    for metric, value in current.items():
        old = baseline.get(metric)
        deltas[metric] = (
            round(value - old, 2)
            if isinstance(value, (int, float)) and isinstance(old, (int, float))
            else None
        )
    return {
        "previous_report": baseline.get("path"),
        "previous_generated_at": baseline.get("generated_at"),
        "previous": {
            "cpu_percent": baseline.get("cpu_percent"),
            "memory_percent": baseline.get("memory_percent"),
        },
        "current": current,
        "delta_percentage_points": deltas,
        "note": "La comparación usa capturas puntuales; diferencias de carga o momento del día no prueban una mejora o regresión sostenida.",
    }


def export_report(report: dict[str, Any], destination: str | Path, file_format: str) -> Path:
    path = Path(destination)
    if file_format == "json":
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    elif file_format == "txt":
        path.write_text(_to_text(report), encoding="utf-8")
    elif file_format == "html":
        path.write_text(_to_html(report), encoding="utf-8")
    else:
        raise ValueError("Formato no soportado: use txt, json o html.")
    return path


def _to_text(report: dict[str, Any]) -> str:
    system = report["system"]
    lines = [
        "INFORME DE DIAGNÓSTICO DE WINDOWS",
        f"Generado: {report['generated_at']}",
        f"Estado general: {report['summary']['status']}",
        "",
        "RECURSOS",
        f"CPU: {system['cpu'].get('utilization_percent')}% "
        f"({system['cpu'].get('physical_cores')} núcleos físicos / {system['cpu'].get('logical_threads')} hilos, "
        f"{system['cpu'].get('frequency_mhz') if system['cpu'].get('frequency_mhz') is not None else 'N/D'} MHz)",
        f"RAM: {system['memory'].get('percent')}% utilizada; "
        f"{system['memory'].get('used_bytes') if system['memory'].get('used_bytes') is not None else 'N/D'} bytes usados, "
        f"{system['memory'].get('available_bytes') if system['memory'].get('available_bytes') is not None else 'N/D'} bytes disponibles "
        f"de {system['memory'].get('total_bytes') if system['memory'].get('total_bytes') is not None else 'N/D'} bytes",
    ]
    for disk in system.get("disks", []):
        lines.append(
            f"Disco {disk.get('mountpoint')}: {disk.get('percent')}% utilizado, "
            f"{disk.get('free_bytes')} bytes libres"
        )
    for sensor in system.get("temperatures", []):
        lines.append(f"Temperatura {sensor.get('name')}: {sensor.get('celsius')} °C ({sensor.get('source')})")
    if not system.get("temperatures"):
        lines.append(f"Temperaturas no disponibles: {system.get('temperature_note')}")
    for gpu in system.get("gpus", []):
        adapter_memory = gpu.get("adapter_memory_bytes")
        adapter_memory_text = (
            f"; memoria de adaptador reportada {adapter_memory / 1024**2:.0f} MiB "
            "(sin distinguir dedicada/compartida)"
            if isinstance(adapter_memory, (int, float))
            else ""
        )
        lines.append(
            f"GPU {gpu.get('name')} ({gpu.get('manufacturer') or 'fabricante no disponible'}): "
            f"utilización {gpu.get('utilization_percent') if gpu.get('utilization_percent') is not None else 'N/D'}%, "
            f"temperatura {gpu.get('temperature_c') if gpu.get('temperature_c') is not None else 'N/D'} °C, "
            f"VRAM usada {gpu.get('vram_used_mb') if gpu.get('vram_used_mb') is not None else 'N/D'} MiB, "
            f"total {gpu.get('vram_total_mb') if gpu.get('vram_total_mb') is not None else 'N/D'} MiB"
            f"{adapter_memory_text}."
        )
    battery = system.get("battery")
    if battery:
        lines.append(
            f"Batería: carga {battery.get('percent') if battery.get('percent') is not None else 'N/D'}%, "
            f"capacidad de diseño {battery.get('design_capacity_mwh') if battery.get('design_capacity_mwh') is not None else 'N/D'} mWh, "
            f"carga completa {battery.get('full_charge_capacity_mwh') if battery.get('full_charge_capacity_mwh') is not None else 'N/D'} mWh, "
            f"desgaste estimado {battery.get('wear_percent') if battery.get('wear_percent') is not None else 'N/D'}%, "
            f"ciclos {battery.get('cycle_count') if battery.get('cycle_count') is not None else 'N/D'}."
        )
    lines.extend(["", "TEMPERATURAS MÍNIMAS, MÁXIMAS Y ACTUALES"])
    for sensor in report.get("temperature_summary", []):
        lines.append(
            f"- {sensor['name']} ({sensor['group']}): actual {sensor['current_celsius']:.1f} °C, "
            f"mínima {sensor['minimum_celsius']:.1f} °C, máxima {sensor['maximum_celsius']:.1f} °C "
            f"({sensor['samples']} muestras)."
        )
    if not report.get("temperature_summary"):
        lines.append("- Sin muestras de temperatura disponibles; no se estimaron valores.")
    lines.extend(["", "PROBLEMAS Y ADVERTENCIAS"])
    lines.extend(
        f"- [{problem['severity']}] {problem['resource']}: {problem['detail']}"
        for problem in report["summary"]["problems"]
    )
    lines.extend(["", "PROCESOS QUE MERECEN REVISIÓN"])
    for process in report["processes"]:
        if process.get("risk") in {"alto", "medio"}:
            signature = process.get("signature") or {}
            details = [
                f"fabricante {signature.get('manufacturer') or 'N/D'}",
                f"firma {signature.get('status') or 'N/D'}",
            ]
            if signature.get("signer"):
                details.append(f"firmante {signature['signer']}")
            if signature.get("certificate_issuer"):
                details.append(f"emisor {signature['certificate_issuer']}")
            if signature.get("certificate_expires"):
                details.append(f"certificado hasta {signature['certificate_expires']}")
            if signature.get("certificate_thumbprint"):
                details.append(f"huella {signature['certificate_thumbprint']}")
            if signature.get("file_version"):
                details.append(f"versión {signature['file_version']}")
            connections = process.get("network_endpoints") or []
            lines.append(
                f"- {process.get('name')} (PID {process.get('pid')}, riesgo {process.get('risk')}): "
                f"{'; '.join(process.get('reasons', []))} Ruta: {process.get('path') or 'no disponible'}; "
                f"{'; '.join(details)}; conexiones remotas: {', '.join(connections) or 'ninguna observada'}."
            )
    lines.extend(["", "PROCESOS CON MAYOR CONSUMO DE RAM"])
    lines.append(
        f"RAM del sistema: {report['memory_analysis'].get('used_percent')}% utilizada. "
        f"Suma de RSS observada: {report['memory_analysis'].get('process_rss_percent_of_total')}% de la RAM física "
        "(puede incluir páginas compartidas)."
    )
    for process in report["memory_analysis"]["top_processes"]:
        signature = process.get("signature") or {}
        signer = signature.get("manufacturer") or signature.get("signer")
        signature_text = f"; firma {signature.get('status') or 'N/D'}"
        if signer:
            signature_text += f", fabricante/firmante {signer}"
        lines.append(
            f"- {process.get('name')} (PID {process.get('pid')}): {process.get('memory_mib')} MiB "
            f"({process.get('memory_percent_total')}% de RAM), riesgo {process.get('risk') or 'informativo'}; "
            f"ruta: {process.get('path') or 'no disponible'}{signature_text}; "
            f"conexiones remotas observadas: {', '.join(process.get('network_endpoints') or []) or 'ninguna'}."
        )
    lines.append("Tendencias de crecimiento sostenido:")
    if report["memory_analysis"]["growth_trends"]:
        for trend in report["memory_analysis"]["growth_trends"]:
            lines.append(
                f"- {trend['name']} (PID {trend['pid']}): +{trend['growth_bytes'] / 1024**2:.1f} MiB "
                f"en {trend['duration_seconds']:.0f} s ({trend['growth_mib_per_minute']:.1f} MiB/min). "
                f"{trend['classification']}."
            )
    else:
        lines.append("- Sin tendencia de crecimiento que supere los criterios mínimos; se necesitan muestras sucesivas.")
    lines.append(report["memory_analysis"]["note"])
    comparison = report.get("comparison")
    if comparison:
        lines.extend(["", "COMPARACIÓN CON EL INFORME ANTERIOR"])
        lines.append(
            f"Anterior: {comparison.get('previous_generated_at') or comparison.get('previous_report')}."
        )
        for metric, label in (("cpu_percent", "CPU"), ("memory_percent", "RAM")):
            previous = comparison["previous"].get(metric)
            current = comparison["current"].get(metric)
            delta = comparison["delta_percentage_points"].get(metric)
            lines.append(
                f"- {label}: {previous if previous is not None else 'N/D'}% → "
                f"{current if current is not None else 'N/D'}% "
                f"(variación {delta:+.2f} puntos porcentuales)." if delta is not None
                else f"- {label}: sin ambas muestras para calcular variación."
            )
        lines.append(comparison["note"])
    lines.extend(["", "ELEMENTOS DE INICIO QUE MERECEN REVISIÓN"])
    for item in report["startup_audit"]:
        if item.get("risk") in {"medio", "advertencia"} or item.get("duplicate"):
            lines.append(
                f"- [{item.get('kind')}] {item.get('name')}: "
                f"{'; '.join(item.get('reasons', []))} Acción: {item.get('location')}"
            )
    lines.extend(["", "PRUEBAS DE RENDIMIENTO"])
    for test in report.get("benchmarks", []):
        lines.append(f"- {json.dumps(test, ensure_ascii=False)}")
    lines.extend(["", "RECOMENDACIONES"])
    lines.extend(f"- {recommendation}" for recommendation in report.get("recommendations", []))
    lines.extend(f"- Limitación: {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def _to_html(report: dict[str, Any]) -> str:
    body = html.escape(_to_text(report))
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Informe de diagnóstico</title><style>"
        "body{font:15px system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem}"
        "pre{white-space:pre-wrap;background:#f4f6f8;padding:1.2rem;border-radius:8px}"
        "</style></head><body><h1>Informe de diagnóstico de Windows</h1><pre>"
        f"{body}</pre></body></html>"
    )
