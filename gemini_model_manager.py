"""Model discovery, compatibility probes, persistence, and fallback ordering."""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import time
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

import httpx
from google.genai import errors, types

from desktop_geometry import ScreenGeometry
from screen_vision import ScreenFrame, suggest_cursor_action


_DEFAULT_TTL_HOURS = 168
_LIST_PAGE_SIZE = 100
_REPORT_VERSION = 2
_RESULT_CLASSIFICATIONS = {
    "compatible_text_image",
    "text_only",
    "unavailable",
    "timeout",
    "quota",
    "auth_error",
    "unknown_error",
    "cancelled",
}


@dataclass(frozen=True)
class ModelDiagnosticResult:
    model: str
    classification: str
    duration_seconds: float
    text_duration_seconds: float
    image_duration_seconds: float
    reason: str = ""


ProgressCallback = Callable[[str, int, int | None, str], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _model_name(model: object) -> str:
    name = getattr(model, "name", None)
    if not isinstance(name, str) or not name.strip():
        return ""
    return name.strip().removeprefix("models/")


def _supports_generate_content(model: object) -> bool:
    actions = getattr(model, "supported_actions", None) or ()
    if not any(
        isinstance(action, str) and action.casefold() == "generatecontent"
        for action in actions
    ):
        return False
    metadata = " ".join(
        str(getattr(model, attribute, "") or "")
        for attribute in ("name", "display_name", "description")
    ).casefold()
    task_specific_markers = (
        "tts",
        "text-to-speech",
        "text to speech",
        "transcrib",
        "embedding",
        "music generation",
        "audio generation",
        "speech generation",
        "image generation",
        "generative image",
        "preview image",
        "flash image",
        "pro image",
    )
    if any(marker in metadata for marker in task_specific_markers):
        return False
    description = str(getattr(model, "description", "") or "").casefold().rstrip(".")
    return not re.search(r"\bimage(?:\s+preview)?$", description)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return (
        struct.pack(">I", len(data))
        + payload
        + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    )


def _synthetic_png() -> bytes:
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    scanlines = b"\x00\xff\x00\x00\x00\xff\x00" * 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def _error_classification(error: BaseException) -> tuple[str, str]:
    if isinstance(error, httpx.TimeoutException):
        return "timeout", type(error).__name__
    if isinstance(error, errors.APIError):
        code = getattr(error, "code", None)
        if code == 404:
            return "unavailable", "El modelo no está disponible para esta API Key."
        error_text = str(error).casefold()
        if code == 400 and "response modalities" in error_text:
            return (
                "unavailable",
                "El modelo no admite respuestas textuales para Jarvis.",
            )
        if code == 429:
            return "quota", "Gemini devolvió HTTP 429; se detuvieron las pruebas."
        if code in {401, 403}:
            return "auth_error", f"Gemini rechazó la autorización (HTTP {code})."
        detail = _redact_key(str(error))
        return "unknown_error", detail[:300]
    detail = _redact_key(str(error))
    return "unknown_error", detail[:300] or type(error).__name__


def _redact_key(value: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        value = value.replace(api_key, "[REDACTED]")
    return re.sub(r"\bAIza[0-9A-Za-z_-]{20,}\b", "[REDACTED]", value)


def _valid_action_response(text: str, model: str) -> bool:
    frame = ScreenFrame(
        png_bytes=_synthetic_png(),
        width=2,
        height=2,
        geometry=ScreenGeometry(0, 0, 2, 2),
        captured_at=time.time(),
    )
    try:
        decision = suggest_cursor_action(
            lambda _contents: text,
            model,
            "Verificación automática de compatibilidad; no ejecutar la acción.",
            frame,
        )
    except (RuntimeError, ValueError, TypeError):
        return False
    return decision.status == "continue" and bool(decision.actions)


class GeminiModelManager:
    def __init__(
        self,
        results_path: Path | None = None,
        *,
        ttl_hours: float | None = None,
    ) -> None:
        self.results_path = results_path or (
            Path(__file__).resolve().parent
            / "diagnostics"
            / "gemini_model_diagnostics.json"
        )
        self.ttl_hours = (
            ttl_hours
            if ttl_hours is not None
            else float(os.getenv("GEMINI_MODEL_DIAGNOSTICS_TTL_HOURS", "168"))
        )
        if self.ttl_hours <= 0:
            raise ValueError("La caducidad de diagnósticos debe ser positiva.")

    def discover_models(
        self,
        client: object,
        *,
        cancelled: Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[str]:
        cancel_event = cancelled or Event()
        pager = client.models.list(config={"page_size": _LIST_PAGE_SIZE})
        candidates: list[str] = []
        seen: set[str] = set()
        for model in pager:
            if cancel_event.is_set():
                break
            name = _model_name(model)
            if name and name not in seen:
                seen.add(name)
                candidates.append(name)
                if progress is not None:
                    progress("discover", len(candidates), None, name)
        self._save_discovery(candidates)
        return candidates

    def test_models(
        self,
        client: object,
        models: Iterable[str],
        *,
        cancelled: Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[ModelDiagnosticResult]:
        cancel_event = cancelled or Event()
        model_names = list(dict.fromkeys(name for name in models if name))
        results: list[ModelDiagnosticResult] = []
        for index, name in enumerate(model_names, 1):
            if cancel_event.is_set():
                results.append(self._result(name, "cancelled", 0, 0, 0, "Cancelado por el usuario."))
                break
            if progress is not None:
                progress("test", index - 1, len(model_names), name)
            result = self._test_one(client, name, cancel_event)
            results.append(result)
            if progress is not None:
                progress("test", index, len(model_names), name)
            if result.classification == "quota":
                for remaining in model_names[index:]:
                    results.append(
                        self._result(
                            remaining,
                            "quota",
                            0,
                            0,
                            0,
                            "Prueba omitida tras detectar límite/cuota HTTP 429.",
                        )
                    )
                break
        self._save_results(results)
        return results

    def _test_one(self, client: object, model: str, cancelled: Event) -> ModelDiagnosticResult:
        started = time.monotonic()
        text_duration = 0.0
        image_duration = 0.0
        phase = "text"
        request_started = started
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
            max_output_tokens=128,
        )
        prompt = (
            "Compatibilidad de Jarvis: responde solo con este JSON válido, "
            '{"status":"continue","actions":[{"type":"wait","seconds":1}]}'
        )
        try:
            text_started = time.monotonic()
            text_response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            text_duration = time.monotonic() - text_started
            text = (getattr(text_response, "text", None) or "").strip()
            if not text or not _valid_action_response(text, model):
                return self._result(
                    model,
                    "unknown_error",
                    time.monotonic() - started,
                    text_duration,
                    0,
                    "La respuesta de texto no cumple el contrato JSON de acciones.",
                )
            if cancelled.is_set():
                return self._result(
                    model, "cancelled", time.monotonic() - started,
                    text_duration, 0, "Cancelado por el usuario."
                )

            phase = "image"
            request_started = time.monotonic()
            image_started = time.monotonic()
            image_response = client.models.generate_content(
                model=model,
                contents=[
                    prompt,
                    types.Part.from_bytes(data=_synthetic_png(), mime_type="image/png"),
                ],
                config=config,
            )
            image_duration = time.monotonic() - image_started
            if cancelled.is_set():
                return self._result(
                    model,
                    "cancelled",
                    time.monotonic() - started,
                    text_duration,
                    image_duration,
                    "Cancelado por el usuario.",
                )

            image_text = (getattr(image_response, "text", None) or "").strip()
            if not image_text or not _valid_action_response(image_text, model):
                return self._result(
                    model,
                    "text_only",
                    time.monotonic() - started,
                    text_duration,
                    image_duration,
                    "La respuesta con imagen no produjo una acción JSON válida.",
                )
            return self._result(
                model,
                "compatible_text_image",
                time.monotonic() - started,
                text_duration,
                image_duration,
            )
        except Exception as exc:
            elapsed = time.monotonic() - request_started
            if phase == "text":
                text_duration = elapsed
            else:
                image_duration = elapsed
            classification, reason = _error_classification(exc)
            if (
                phase == "image"
                and isinstance(exc, errors.APIError)
                and getattr(exc, "code", None) == 400
            ):
                classification = "text_only"
                reason = "El modelo rechazó la solicitud de texto e imagen."
            return self._result(
                model,
                classification,
                time.monotonic() - started,
                text_duration,
                image_duration,
                reason,
            )

    @staticmethod
    def _result(
        model: str,
        classification: str,
        duration: float,
        text_duration: float,
        image_duration: float,
        reason: str = "",
    ) -> ModelDiagnosticResult:
        if classification not in _RESULT_CLASSIFICATIONS:
            raise ValueError(f"Clasificación de modelo desconocida: {classification}.")
        return ModelDiagnosticResult(
            model=model,
            classification=classification,
            duration_seconds=round(duration, 3),
            text_duration_seconds=round(text_duration, 3),
            image_duration_seconds=round(image_duration, 3),
            reason=reason,
        )

    def fresh_report(self) -> dict[str, object] | None:
        try:
            report = json.loads(self.results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(report, dict):
            return None
        if report.get("report_version") != _REPORT_VERSION:
            return None
        now = _utc_now()
        try:
            results_expiry = datetime.fromisoformat(
                report.get("results_expires_at", report.get("expires_at", ""))
            )
        except (ValueError, TypeError):
            results_expiry = None
        try:
            discovery_expiry = datetime.fromisoformat(
                report.get("discovery_expires_at", "")
            )
        except (ValueError, TypeError):
            discovery_expiry = None
        results_fresh = (
            results_expiry is not None
            and results_expiry > now
            and isinstance(report.get("results"), list)
            and bool(report["results"])
        )
        discovery_fresh = (
            discovery_expiry is not None
            and discovery_expiry > now
            and isinstance(report.get("discovered_models"), list)
        )
        if not results_fresh and not discovery_fresh:
            return None
        return report

    def results_are_fresh(self) -> bool:
        report = self.fresh_report()
        if report is None:
            return False
        try:
            expiry = datetime.fromisoformat(
                report.get("results_expires_at", report.get("expires_at", ""))
            )
        except (ValueError, TypeError):
            return False
        return expiry > _utc_now()

    def fresh_discovered_models(self) -> list[str]:
        report = self.fresh_report()
        if report is None:
            return []
        try:
            expiry = datetime.fromisoformat(
                report.get("discovery_expires_at", "")
            )
        except (ValueError, TypeError):
            return []
        models = report.get("discovered_models", [])
        if expiry <= _utc_now() or not isinstance(models, list):
            return []
        return [model for model in models if isinstance(model, str)]

    def fresh_results(self) -> list[ModelDiagnosticResult]:
        report = self.fresh_report()
        if report is None or not self.results_are_fresh():
            return []
        results = report.get("results", [])
        if not isinstance(results, list):
            return []
        valid = []
        for result in results:
            if not isinstance(result, dict):
                continue
            try:
                parsed = ModelDiagnosticResult(**result)
            except TypeError:
                continue
            if parsed.classification == "compatible_text_image":
                valid.append(parsed)
        return valid

    def candidate_models(self, primary: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                [primary]
                + [result.model for result in self.fresh_results() if result.model != primary]
            )
        )

    def selected_primary(self, default: str) -> str:
        report = self.fresh_report()
        if report is None:
            return default
        selected = report.get("selected_primary")
        return selected.strip() if isinstance(selected, str) and selected.strip() else default

    def select_primary(self, model: str) -> None:
        normalized = model.strip()
        if not normalized:
            raise ValueError("El identificador del modelo no puede estar vacío.")
        report = self.fresh_report()
        if report is None:
            raise RuntimeError("Primero debes buscar los modelos disponibles.")
        discovered = report.get("discovered_models", [])
        if not isinstance(discovered, list) or normalized not in discovered:
            raise ValueError("Selecciona un modelo devuelto por la búsqueda de Gemini.")
        report["selected_primary"] = normalized
        self._write_report(report)

    def _save_discovery(self, models: list[str]) -> None:
        report = self._read_report_unchecked()
        if report.get("report_version") != _REPORT_VERSION:
            report = {}
        now = _utc_now()
        report.update(
            {
                "discovery_checked_at": now.isoformat(),
                "report_version": _REPORT_VERSION,
                "discovery_expires_at": (
                    now + timedelta(hours=self.ttl_hours)
                ).isoformat(),
                "ttl_hours": self.ttl_hours,
                "discovered_models": models,
            }
        )
        report.setdefault("results", [])
        self._write_report(report)

    def _save_results(self, results: list[ModelDiagnosticResult]) -> None:
        report = self._read_report_unchecked()
        if report.get("report_version") != _REPORT_VERSION:
            report = {}
        now = _utc_now()
        report.update(
            {
                "checked_at": now.isoformat(),
                "report_version": _REPORT_VERSION,
                "results_expires_at": (
                    now + timedelta(hours=self.ttl_hours)
                ).isoformat(),
                "ttl_hours": self.ttl_hours,
                "results": [asdict(result) for result in results],
            }
        )
        report["expires_at"] = report["results_expires_at"]
        report.setdefault("discovered_models", [result.model for result in results])
        selected = report.get("selected_primary")
        if selected not in {
            result.model
            for result in results
            if result.classification == "compatible_text_image"
        }:
            report["selected_primary"] = None
        self._write_report(report)

    def _read_report_unchecked(self) -> dict[str, object]:
        try:
            report = json.loads(self.results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return report if isinstance(report, dict) else {}

    def _write_report(self, report: dict[str, object]) -> None:
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=self.results_path.name,
            suffix=".tmp",
            dir=self.results_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary_name, self.results_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
