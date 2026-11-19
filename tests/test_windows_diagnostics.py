from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows_diagnostics import collectors
from windows_diagnostics.analyzer import (
    analyze_memory_trends,
    analyze_processes,
    analyze_startup,
    classify_process,
    rank_process_memory,
    summarize_temperatures,
)
from windows_diagnostics.benchmarks import run_benchmark
from windows_diagnostics.reports import (
    build_report,
    compare_to_baseline,
    export_report,
    load_comparison_baseline,
)


class ProcessRiskTests(unittest.TestCase):
    def test_unknown_unsigned_process_is_not_automatically_malware(self) -> None:
        result = classify_process(
            {
                "name": "local-tool.exe",
                "path": r"C:\Program Files\LocalTool\local-tool.exe",
                "signature": {"status": "NotSigned", "signer": None},
                "cpu_percent": 1,
                "memory_bytes": 1000,
            }
        )

        self.assertEqual(result["risk"], "informativo")
        self.assertIn("No se observaron indicadores destacados", result["reasons"][0])

    def test_windows_lookalike_in_temporary_directory_is_high_risk(self) -> None:
        result = classify_process(
            {
                "name": "svchost.exe",
                "path": r"C:\Users\user\AppData\Local\Temp\svchost.exe",
                "signature": {"status": "NotSigned"},
                "cpu_percent": 0,
                "memory_bytes": 0,
            }
        )

        self.assertEqual(result["risk"], "alto")
        self.assertGreaterEqual(len(result["reasons"]), 2)

    def test_lookalike_spelling_outside_windows_is_flagged(self) -> None:
        result = classify_process(
            {
                "name": "svch0st.exe",
                "path": r"C:\Users\user\AppData\Roaming\svch0st.exe",
                "signature": {"status": "Valid", "signer": "Unknown publisher"},
            }
        )

        self.assertEqual(result["risk"], "alto")
        self.assertIn("svchost.exe", result["reasons"][0])

    def test_startup_duplicates_are_reviewable_without_malware_claim(self) -> None:
        entries = [
            {"kind": "Inicio", "name": "Agent", "location": "agent.exe"},
            {"kind": "Tarea", "name": "Agent", "location": "agent.exe"},
        ]

        result = analyze_startup(entries)

        self.assertTrue(all(item["duplicate"] for item in result))
        self.assertTrue(all(item["risk"] == "medio" for item in result))

    def test_incomplete_audit_is_reported_as_incomplete_not_clean(self) -> None:
        result = analyze_startup(
            [{"kind": "Aviso", "name": "Servicios", "collection_error": True, "error": "Acceso denegado"}]
        )

        self.assertEqual(result[0]["risk"], "advertencia")


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_rejects_duration_and_load_outside_safety_limits(self) -> None:
        with self.assertRaises(ValueError):
            run_benchmark(duration_seconds=31)
        with self.assertRaises(ValueError):
            run_benchmark(max_cpu_percent=100)

    @patch("windows_diagnostics.benchmarks._current_max_temperature", return_value=None)
    def test_disk_benchmark_reads_and_writes_a_bounded_temporary_file(self, _temperature) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "windows_diagnostics.benchmarks.tempfile.gettempdir", return_value=temp_dir
        ):
            result = run_benchmark("disk", duration_seconds=3, disk_mb=1)

            self.assertTrue(result["completed"])
            self.assertEqual(result["disk_mib_written"], 1)
            self.assertEqual(result["disk_mib_read"], 1)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    @patch("windows_diagnostics.benchmarks._current_max_temperature", return_value=96)
    def test_high_temperature_stops_the_cpu_test(self, _temperature) -> None:
        result = run_benchmark("cpu", duration_seconds=5, temperature_limit_c=90)

        self.assertTrue(result["cancelled"])
        self.assertTrue(result["warnings"])


class ReportTests(unittest.TestCase):
    def test_report_contains_recommendations_and_exports_all_formats(self) -> None:
        report = build_report(
            {
                "cpu": {"utilization_percent": 5, "physical_cores": 2, "logical_threads": 4},
                "memory": {"percent": 20},
                "disks": [],
                "temperatures": [],
                "temperature_note": "No disponible",
            },
            [
                {
                    "name": "browser.exe",
                    "pid": 42,
                    "path": r"C:\Program Files\Browser\browser.exe",
                    "memory_bytes": 100 * 1024**2,
                    "memory_percent_total": 1.2,
                    "risk": "informativo",
                }
            ],
            [],
        )
        self.assertTrue(report["recommendations"])
        self.assertEqual(report["memory_analysis"]["top_processes"][0]["pid"], 42)

        with tempfile.TemporaryDirectory() as directory:
            for file_format in ("txt", "json", "html"):
                destination = Path(directory) / f"report.{file_format}"
                export_report(report, destination, file_format)
                self.assertGreater(destination.stat().st_size, 0)
                if file_format == "txt":
                    self.assertIn("browser.exe", destination.read_text(encoding="utf-8"))

    def test_incomplete_audit_does_not_report_a_clean_state(self) -> None:
        report = build_report(
            {
                "cpu": {"utilization_percent": 5},
                "memory": {"percent": 20},
                "disks": [],
                "temperatures": [],
            },
            [],
            [{"collection_error": True, "error": "Acceso denegado"}],
        )

        self.assertEqual(report["summary"]["status"], "Información incompleta")

    def test_flagged_processes_require_manual_review_in_overall_status(self) -> None:
        report = build_report(
            {
                "cpu": {"utilization_percent": 5},
                "memory": {"percent": 20},
                "disks": [],
                "temperatures": [],
            },
            [{"name": "review.exe", "pid": 42, "risk": "medio", "reasons": ["ubicación inusual"]}],
            [],
        )

        self.assertEqual(report["summary"]["status"], "Requiere revisión")

    def test_potential_cpu_throttling_requires_cpu_temperature_and_low_clock(self) -> None:
        snapshot = {
            "cpu": {"frequency_mhz": 800, "frequency_max_mhz": 2000},
            "memory": {"percent": 20},
            "disks": [],
            "temperatures": [
                {"name": "CPU Package", "group": "CPU", "celsius": 92},
                {"name": "SSD", "group": "Storage", "celsius": 95},
            ],
        }

        report = build_report(snapshot, [], [])

        self.assertTrue(
            any("posible throttling" in item["detail"] for item in report["summary"]["problems"])
        )

    def test_previous_txt_report_produces_percentage_point_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_path = Path(directory) / "previous.txt"
            previous_path.write_text(
                "Generado: 2026-01-01\nCPU: 16.4% (4 núcleos)\nRAM: 76.8% utilizada\n",
                encoding="utf-8",
            )
            baseline = load_comparison_baseline(previous_path)
            comparison = compare_to_baseline(
                {"cpu": {"utilization_percent": 20.0}, "memory": {"percent": 70.0}},
                baseline,
            )

        self.assertEqual(comparison["delta_percentage_points"]["cpu_percent"], 3.6)
        self.assertEqual(comparison["delta_percentage_points"]["memory_percent"], -6.8)


class TrendAndSensorTests(unittest.TestCase):
    def test_memory_growth_requires_a_sustained_trend(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = [
            {
                "captured_at": (start + timedelta(seconds=index * 5)).isoformat(),
                "process_memory": [
                    {
                        "pid": 7,
                        "create_time": 100.0,
                        "name": "growing.exe",
                        "memory_bytes": (100 + 2 * index) * 1024**2,
                    }
                ],
            }
            for index in range(13)
        ]

        trends = analyze_memory_trends(history)

        self.assertEqual(len(trends), 1)
        self.assertIn("no confirma una fuga", trends[0]["classification"])

    def test_high_memory_without_a_temporal_increase_is_not_a_leak_signal(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = [
            {
                "captured_at": (start + timedelta(seconds=index * 5)).isoformat(),
                "process_memory": [
                    {"pid": 7, "create_time": 100.0, "name": "stable.exe", "memory_bytes": 2 * 1024**3}
                ],
            }
            for index in range(20)
        ]

        self.assertEqual(analyze_memory_trends(history), [])

    def test_temperature_summary_uses_recorded_minimum_and_maximum(self) -> None:
        history = [
            {
                "captured_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
                "temperatures": [{
                    "identifier": "cpu:0", "name": "CPU Package", "group": "CPU",
                    "celsius": 48, "minimum": 44, "maximum": 76,
                }],
            },
            {
                "captured_at": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc).isoformat(),
                "temperatures": [{
                    "identifier": "cpu:0", "name": "CPU Package", "group": "CPU",
                    "celsius": 71, "minimum": 41, "maximum": 83,
                }],
            },
        ]

        result = summarize_temperatures(history)[0]

        self.assertEqual(result["minimum_celsius"], 41)
        self.assertEqual(result["maximum_celsius"], 83)
        self.assertEqual(result["current_celsius"], 71)

    @patch("windows_diagnostics.collectors.sys.platform", "win32")
    @patch(
        "windows_diagnostics.collectors._powershell_json",
        return_value={
            "available": True,
            "sensors": [
                {
                    "hardware": "CPU Package",
                    "hardware_type": "Cpu",
                    "name": "CPU Package",
                    "sensor_type": "Temperature",
                    "value": 57.0,
                    "minimum": 44.0,
                    "maximum": 76.0,
                    "source": "LibreHardwareMonitorLib",
                }
            ],
            "error": None,
        },
    )
    def test_lhm_sensor_values_are_taken_from_the_library_response(self, _powershell) -> None:
        sensors, error = collectors._read_lhm_sensors()

        self.assertIsNone(error)
        self.assertEqual(sensors[0]["value"], 57.0)
        self.assertEqual(sensors[0]["minimum"], 44.0)
        self.assertEqual(sensors[0]["maximum"], 76.0)

    @patch("windows_diagnostics.collectors.sys.platform", "win32")
    @patch("windows_diagnostics.collectors.shutil.which", return_value=None)
    @patch(
        "windows_diagnostics.collectors._powershell_json",
        return_value={
            "Name": "Intel(R) Graphics",
            "AdapterCompatibility": "Intel Corporation",
            "VideoProcessor": "Intel Graphics",
            "DriverVersion": "1.0",
        },
    )
    def test_gpu_sensor_values_include_utilization_temperature_and_vram(self, _powershell, _which) -> None:
        gpus = collectors._read_gpu(
            [
                {
                    "hardware": "Intel(R) Graphics",
                    "hardware_type": "GpuIntel",
                    "name": "GPU Core",
                    "sensor_type": "Temperature",
                    "value": 62,
                },
                {
                    "hardware": "Intel(R) Graphics",
                    "hardware_type": "GpuIntel",
                    "name": "GPU Core Load",
                    "sensor_type": "Load",
                    "value": 37,
                },
                {
                    "hardware": "Intel(R) Graphics",
                    "hardware_type": "GpuIntel",
                    "name": "GPU Memory Used",
                    "sensor_type": "Data",
                    "value": 1.5,
                },
            ]
        )

        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["temperature_c"], 62)
        self.assertEqual(gpus[0]["utilization_percent"], 37)
        self.assertEqual(gpus[0]["vram_used_mb"], 1536)

    @patch("windows_diagnostics.collectors.sys.platform", "win32")
    @patch(
        "windows_diagnostics.collectors._powershell_json",
        return_value=[
            {
                "path": r"C:\Program Files\App\app.exe",
                "status": "Valid",
                "signer": "Publisher",
                "certificate_thumbprint": "ABC",
                "manufacturer": "Company",
                "file_version": "1.2",
            }
        ],
    )
    def test_signature_metadata_is_preserved_for_process_analysis(self, _powershell) -> None:
        signatures = collectors._signature_status([r"C:\Program Files\App\app.exe"])

        signature = signatures[r"c:\program files\app\app.exe"]
        self.assertEqual(signature["manufacturer"], "Company")
        self.assertEqual(signature["certificate_thumbprint"], "ABC")
        self.assertEqual(signature["file_version"], "1.2")

    def test_process_memory_ranking_is_descending(self) -> None:
        ranked = rank_process_memory(
            [
                {"pid": 1, "memory_bytes": 50},
                {"pid": 2, "memory_bytes": 150},
            ]
        )

        self.assertEqual([item["pid"] for item in ranked], [2, 1])


if __name__ == "__main__":
    unittest.main()
