"""Minimal Gemini Interactions API client implemented with Python's stdlib."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class GeminiAPIError(RuntimeError):
    """A safe-to-display Gemini API failure, without request credentials."""


def _extract_output_text(response: object) -> str:
    if not isinstance(response, dict):
        raise GeminiAPIError("Gemini devolvió una respuesta con formato inválido.")

    direct_text = response.get("output_text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    output_parts: list[str] = []
    steps = response.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            content = step.get("content")
            if isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    ):
                        output_parts.append(part["text"])
    if output_parts:
        return "\n".join(output_parts).strip()
    raise GeminiAPIError("Gemini devolvió una respuesta sin texto.")


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 30,
        max_attempts: int = 3,
    ) -> None:
        if not api_key.strip():
            raise ValueError("GEMINI_API_KEY no puede estar vacía.")
        if not model.strip():
            raise ValueError("GEMINI_MODEL no puede estar vacío.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser positivo.")
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts debe estar entre 1 y 5.")

        self._api_key = api_key.strip()
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts

    def generate(self, content: object) -> str:
        """Create a stateless Interaction from text or multimodal content."""
        body = json.dumps(
            {"model": self.model, "input": content, "store": False},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            _INTERACTIONS_URL,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-goog-api-key": self._api_key,
            },
            method="POST",
        )

        for attempt in range(self.max_attempts):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    response_body = response.read()
                try:
                    payload = json.loads(response_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GeminiAPIError(
                        "Gemini devolvió una respuesta que no es JSON válido."
                    ) from exc
                return _extract_output_text(payload)
            except HTTPError as exc:
                if exc.code in _TRANSIENT_STATUS_CODES and attempt + 1 < self.max_attempts:
                    time.sleep(1 + attempt)
                    continue
                detail = self._http_error_detail(exc)
                raise GeminiAPIError(f"Gemini respondió con HTTP {exc.code}: {detail}") from exc
            except (TimeoutError, URLError) as exc:
                if self._is_timeout(exc):
                    raise GeminiAPIError(
                        "Gemini tardó demasiado en responder. "
                        "No se ejecutó ninguna acción; inténtalo de nuevo más tarde."
                    ) from exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(1 + attempt)
                    continue
                raise GeminiAPIError(
                    "No se pudo conectar con Gemini después de varios intentos."
                ) from exc
            except OSError as exc:
                if attempt + 1 < self.max_attempts:
                    time.sleep(1 + attempt)
                    continue
                raise GeminiAPIError(
                    "Falló la comunicación HTTPS con Gemini después de varios intentos."
                ) from exc
        raise GeminiAPIError("Gemini no está disponible temporalmente.")

    @staticmethod
    def _is_timeout(error: BaseException) -> bool:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, TimeoutError) or "timeout" in type(current).__name__.casefold():
                return True
            current = (
                getattr(current, "reason", None)
                or current.__cause__
                or current.__context__
            )
        return False

    @staticmethod
    def _http_error_detail(error: HTTPError) -> str:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return "la solicitud no pudo completarse."
        if isinstance(payload, dict):
            error_data = payload.get("error")
            message = error_data.get("message") if isinstance(error_data, dict) else None
            if isinstance(message, str) and message.strip():
                return message.strip()
        return "la solicitud no pudo completarse."
