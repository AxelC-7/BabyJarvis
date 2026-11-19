import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from google.genai import errors

from gemini_model_manager import (
    GeminiModelManager,
    _synthetic_png,
    _valid_action_response,
)


_VALID_ACTION_JSON = (
    '{"status":"continue","actions":[{"type":"wait","seconds":1}]}'
)


class _PagedModels:
    def __init__(self, pages):
        self.pages = pages
        self.iterated_pages = 0

    def __iter__(self):
        for page in self.pages:
            self.iterated_pages += 1
            yield from page


class GeminiModelManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "diagnostics.json"
        self.manager = GeminiModelManager(self.path, ttl_hours=24)

    def test_discovery_uses_paged_sdk_results_and_filters_generate_content(self) -> None:
        pager = _PagedModels(
            [
                [
                    SimpleNamespace(
                        name="models/text-model",
                        supported_actions=["generateContent"],
                    ),
                    SimpleNamespace(
                        name="models/embedding-model",
                        display_name="Embedding Model",
                        supported_actions=["embedContent"],
                    ),
                    SimpleNamespace(
                        name="models/audio-model",
                        display_name="Speech TTS",
                        supported_actions=["generateContent"],
                    ),
                    SimpleNamespace(
                        name="models/image-model",
                        description="Preview Image Generation",
                        supported_actions=["generateContent"],
                    ),
                    SimpleNamespace(
                        name="models/dedicated-image-output",
                        description="Gemini Flash Image.",
                        supported_actions=["generateContent"],
                    ),
                ],
                [
                    SimpleNamespace(
                        name="models/vision-model",
                        description="Supports image input for visual understanding.",
                        supported_actions=["generateContent"],
                    ),
                    SimpleNamespace(
                        name="models/text-model",
                        supported_actions=["generateContent"],
                    ),
                ],
            ]
        )
        client = SimpleNamespace(models=SimpleNamespace(list=Mock(return_value=pager)))

        models = self.manager.discover_models(client)

        self.assertEqual(models, ["text-model", "vision-model"])
        self.assertEqual(pager.iterated_pages, 2)
        client.models.list.assert_called_once_with(config={"page_size": 100})
        report = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(report["discovered_models"], models)
        self.assertNotIn("api_key", json.dumps(report).casefold())
        self.assertEqual(report["report_version"], 2)

    def test_audio_only_text_output_error_is_unavailable_for_jarvis(self) -> None:
        audio_output_only = errors.APIError(
            code=400,
            response_json={
                "error": {
                    "code": 400,
                    "message": (
                        "The requested combination of response modalities (TEXT) "
                        "is not supported. The model accepts AUDIO only."
                    ),
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
        client = SimpleNamespace(
            models=SimpleNamespace(generate_content=Mock(side_effect=audio_output_only))
        )

        result = self.manager.test_models(client, ["audio-only"])[0]

        self.assertEqual(result.classification, "unavailable")
        self.assertIn("textuales", result.reason)

    def test_text_and_synthetic_image_json_contract_marks_model_compatible(self) -> None:
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(
                    return_value=SimpleNamespace(text=_VALID_ACTION_JSON)
                )
            )
        )

        result = self.manager.test_models(client, ["vision-model"])[0]

        self.assertEqual(result.classification, "compatible_text_image")
        self.assertEqual(client.models.generate_content.call_count, 2)
        second_contents = client.models.generate_content.call_args_list[1].kwargs["contents"]
        self.assertEqual(second_contents[1].inline_data.data, _synthetic_png())
        self.assertTrue(_valid_action_response(_VALID_ACTION_JSON, "vision-model"))

    def test_model_that_rejects_image_is_classified_text_only(self) -> None:
        image_unsupported = errors.APIError(
            code=400,
            response_json={
                "error": {
                    "code": 400,
                    "message": "This model does not support image input",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(
                    side_effect=[
                        SimpleNamespace(text=_VALID_ACTION_JSON),
                        image_unsupported,
                    ]
                )
            )
        )

        result = self.manager.test_models(client, ["text-only"])[0]

        self.assertEqual(result.classification, "text_only")
        self.assertIn("image", result.reason.casefold())

    def test_server_error_during_image_probe_is_not_misclassified_as_text_only(self) -> None:
        server_error = errors.APIError(
            code=503,
            response_json={
                "error": {
                    "code": 503,
                    "message": "temporarily unavailable",
                    "status": "UNAVAILABLE",
                }
            },
        )
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(
                    side_effect=[SimpleNamespace(text=_VALID_ACTION_JSON), server_error]
                )
            )
        )

        result = self.manager.test_models(client, ["model-a"])[0]

        self.assertEqual(result.classification, "unknown_error")

    def test_model_error_classifications_include_timeout_quota_and_auth(self) -> None:
        cases = (
            (httpx.ReadTimeout("timeout"), "timeout"),
            (
                errors.APIError(429, {"error": {"message": "quota"}}),
                "quota",
            ),
            (
                errors.APIError(403, {"error": {"message": "denied"}}),
                "auth_error",
            ),
            (
                errors.APIError(404, {"error": {"message": "not found"}}),
                "unavailable",
            ),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                client = SimpleNamespace(
                    models=SimpleNamespace(
                        generate_content=Mock(side_effect=error)
                    )
                )
                result = self.manager.test_models(client, ["test-model"])[0]
                self.assertEqual(result.classification, expected)

    def test_quota_error_stops_probing_other_models(self) -> None:
        quota = errors.APIError(429, {"error": {"message": "quota"}})
        client = SimpleNamespace(
            models=SimpleNamespace(generate_content=Mock(side_effect=quota))
        )

        results = self.manager.test_models(client, ["one", "two", "three"])

        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual([item.classification for item in results], ["quota"] * 3)

    def test_cancelled_diagnostics_do_not_start_model_requests(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock()))

        results = self.manager.test_models(
            client,
            ["one", "two"],
            cancelled=cancelled,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].classification, "cancelled")
        client.models.generate_content.assert_not_called()

    def test_compatible_models_expire_and_cannot_be_selected(self) -> None:
        self.manager._save_discovery(["one"])
        from gemini_model_manager import ModelDiagnosticResult

        self.manager._save_results(
            [
                ModelDiagnosticResult("one", "compatible_text_image", 0.1, 0.05, 0.05)
            ]
        )
        self.manager.select_primary("one")
        self.assertEqual(self.manager.candidate_models("default"), ("default", "one"))
        report = json.loads(self.path.read_text(encoding="utf-8"))
        report["results_expires_at"] = "2000-01-01T00:00:00+00:00"
        report["expires_at"] = report["results_expires_at"]
        self.path.write_text(json.dumps(report), encoding="utf-8")

        self.assertIsNotNone(self.manager.fresh_report())
        self.assertFalse(self.manager.results_are_fresh())
        self.assertEqual(self.manager.candidate_models("default"), ("default",))
        with self.assertRaisesRegex(RuntimeError, "vencidos o ausentes"):
            self.manager.select_primary("one")

    def test_discovery_does_not_upgrade_stale_compatibility_results(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "report_version": 0,
                    "discovered_models": ["old-model"],
                    "results": [
                        {
                            "model": "old-model",
                            "classification": "compatible_text_image",
                            "duration_seconds": 0.1,
                            "text_duration_seconds": 0.05,
                            "image_duration_seconds": 0.05,
                            "reason": "",
                        }
                    ],
                    "results_expires_at": "2099-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        self.manager._save_discovery(["new-model"])

        report = self.manager.fresh_report()
        self.assertIsNotNone(report)
        self.assertEqual(report["discovered_models"], ["new-model"])
        self.assertEqual(report["results"], [])
        self.assertEqual(self.manager.candidate_models("primary"), ("primary",))

    def test_only_compatible_unexpired_models_can_become_primary(self) -> None:
        from gemini_model_manager import ModelDiagnosticResult

        self.manager._save_results(
            [
                ModelDiagnosticResult("vision", "compatible_text_image", 0.2, 0.1, 0.1),
                ModelDiagnosticResult("text", "text_only", 0.2, 0.1, 0.1, "no image"),
            ]
        )
        self.manager.select_primary("vision")
        self.assertEqual(self.manager.selected_primary("default"), "vision")
        with self.assertRaisesRegex(ValueError, "superado texto e imagen"):
            self.manager.select_primary("text")


if __name__ == "__main__":
    unittest.main()
