from __future__ import annotations

import base64
import json
import logging
import ntpath
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

LOGGER = logging.getLogger(__name__)


def _powershell(script: str, *, timeout: int = 45) -> str:
    if sys.platform != "win32":
        raise OSError("Esta consulta solo está disponible en Windows.")
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not executable:
        raise RuntimeError("No se encontró PowerShell.")
    script = (
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding "
        "-ArgumentList $false; "
        + script
    )
    encoded = __import__("base64").b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"PowerShell finalizó con código {result.returncode}.")
    return result.stdout.strip()


def _powershell_json(script: str, *, timeout: int = 45) -> Any:
    output = _powershell(script, timeout=timeout)
    if not output:
        return []
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("PowerShell devolvió una respuesta JSON inválida.") from exc


def _read_lhm_sensors() -> tuple[list[dict[str, Any]], str | None]:
    """Read LibreHardwareMonitorLib sensors through its documented .NET API."""
    if sys.platform != "win32":
        return [], "La integración con LibreHardwareMonitor requiere Windows."
    script = r"""
$ErrorActionPreference = 'Stop'
$library = $env:LIBRE_HARDWARE_MONITOR_PATH
if ($library -and (Test-Path -LiteralPath $library -PathType Container)) {
  $library = Join-Path $library 'LibreHardwareMonitorLib.dll'
}
if (-not $library) {
  $process = Get-CimInstance Win32_Process -Filter "Name='LibreHardwareMonitor.exe'" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($process.ExecutablePath) {
    $library = Join-Path (Split-Path -Parent $process.ExecutablePath) 'LibreHardwareMonitorLib.dll'
  }
}
if (-not $library -or -not (Test-Path -LiteralPath $library -PathType Leaf)) {
  [pscustomobject]@{available=$false;sensors=@();error='No se encontró LibreHardwareMonitorLib.dll; inicie LibreHardwareMonitor o configure LIBRE_HARDWARE_MONITOR_PATH.'} | ConvertTo-Json -Compress
  exit 0
}
$computer = $null
$sensors = New-Object 'System.Collections.Generic.List[object]'
$failure = $null
function Read-HardwareSensors($hardware, $records) {
  $hardware.Update()
  foreach ($sensor in $hardware.Sensors) {
    if ($null -ne $sensor.Value) {
      $records.Add([pscustomobject]@{
        hardware=[string]$hardware.Name
        hardware_type=[string]$hardware.HardwareType
        identifier=[string]$sensor.Identifier
        name=[string]$sensor.Name
        sensor_type=[string]$sensor.SensorType
        value=[double]$sensor.Value
        minimum=if ($null -ne $sensor.Min) {[double]$sensor.Min} else {$null}
        maximum=if ($null -ne $sensor.Max) {[double]$sensor.Max} else {$null}
        source='LibreHardwareMonitorLib'
      })
    }
  }
  foreach ($child in $hardware.SubHardware) { Read-HardwareSensors $child $records }
}
try {
  [Reflection.Assembly]::LoadFrom($library) | Out-Null
  $computer = New-Object 'LibreHardwareMonitor.Hardware.Computer'
  $computer.IsCpuEnabled = $true
  $computer.IsGpuEnabled = $true
  $computer.IsMemoryEnabled = $true
  $computer.IsMotherboardEnabled = $true
  $computer.IsStorageEnabled = $true
  $computer.IsBatteryEnabled = $true
  $computer.IsPowerMonitorEnabled = $true
  $computer.Open()
  foreach ($hardware in $computer.Hardware) { Read-HardwareSensors $hardware $sensors }
} catch {
  $failure = $_.Exception.Message
} finally {
  if ($computer) { try { $computer.Close() } catch {} }
}
if ($failure) {
  [pscustomobject]@{available=$false;sensors=@();error=$failure} | ConvertTo-Json -Compress
} else {
  [pscustomobject]@{available=$true;sensors=@($sensors.ToArray());error=$null} | ConvertTo-Json -Compress -Depth 5
}
"""
    try:
        result = _powershell_json(script, timeout=20)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        LOGGER.info("LibreHardwareMonitor no disponible: %s", exc)
        return [], f"No se pudo consultar LibreHardwareMonitor: {exc}"
    if not isinstance(result, dict):
        return [], "LibreHardwareMonitor devolvió una respuesta inesperada."
    sensors = result.get("sensors") or []
    if isinstance(sensors, dict):
        sensors = [sensors]
    normalized = [dict(sensor) for sensor in sensors if isinstance(sensor, dict)]
    return normalized, result.get("error")


def _read_legacy_wmi_sensors() -> list[dict[str, Any]]:
    """Read sensors exposed by OpenHardwareMonitor's optional WMI provider."""
    script = r"""
$all = @()
foreach ($namespace in @('root\LibreHardwareMonitor','root\OpenHardwareMonitor')) {
  try {
    $all += Get-CimInstance -Namespace $namespace -ClassName Sensor -ErrorAction Stop |
      Where-Object {$null -ne $_.Value} |
      ForEach-Object {[pscustomobject]@{
        hardware=[string]$_.Parent; hardware_type='Unknown'; identifier=[string]$_.Identifier
        name=[string]$_.Name; sensor_type=[string]$_.SensorType; value=[double]$_.Value
        minimum=$_.Min; maximum=$_.Max; source=$namespace
      }}
  } catch {}
}
$all | ConvertTo-Json -Compress -Depth 4
"""
    try:
        result = _powershell_json(script, timeout=8)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        LOGGER.debug("No hay sensores WMI heredados: %s", exc)
        return []
    if isinstance(result, dict):
        result = [result]
    return [dict(sensor) for sensor in result if isinstance(sensor, dict)]


def _read_hardware_sensors() -> tuple[list[dict[str, Any]], str | None]:
    sensors: list[dict[str, Any]] = []
    try:
        readings = psutil.sensors_temperatures(fahrenheit=False)
    except (AttributeError, OSError, NotImplementedError):
        readings = {}
    for group, entries in readings.items():
        for entry in entries:
            if entry.current is not None:
                sensors.append(
                    {
                        "name": entry.label or group,
                        "hardware": group,
                        "hardware_type": "Unknown",
                        "identifier": f"psutil:{group}:{entry.label}",
                        "sensor_type": "Temperature",
                        "value": float(entry.current),
                        "minimum": entry.current,
                        "maximum": entry.current,
                        "source": "psutil",
                    }
                )
    lhm_sensors, lhm_note = _read_lhm_sensors()
    sensors.extend(lhm_sensors)
    if not lhm_sensors:
        sensors.extend(_read_legacy_wmi_sensors())
    return sensors, lhm_note


def _read_temperatures(
    hardware_sensors: list[dict[str, Any]],
    sensor_note: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    sensors = []
    for sensor in hardware_sensors:
        if str(sensor.get("sensor_type", "")).casefold() != "temperature":
            continue
        value = sensor.get("value")
        if isinstance(value, (int, float)):
            sensors.append(
                {
                    "name": sensor.get("name") or sensor.get("hardware"),
                    "group": sensor.get("hardware") or sensor.get("hardware_type"),
                    "hardware_type": sensor.get("hardware_type"),
                    "celsius": float(value),
                    "minimum": sensor.get("minimum"),
                    "maximum": sensor.get("maximum"),
                    "source": sensor.get("source"),
                    "identifier": sensor.get("identifier"),
                }
            )
    return (sensors, None) if sensors else ([], sensor_note or "No hay sensores térmicos accesibles.")


def _normalize_model_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _read_gpu(hardware_sensors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    if sys.platform == "win32":
        try:
            result = _powershell_json(
                "$ErrorActionPreference='Stop'; "
                "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,VideoProcessor,AdapterRAM,DriverVersion | "
                "ConvertTo-Json -Compress",
                timeout=8,
            )
            if isinstance(result, dict):
                result = [result]
            for item in result:
                if isinstance(item, dict):
                    gpus.append(
                        {
                            "name": item.get("Name") or "GPU desconocida",
                            "manufacturer": item.get("AdapterCompatibility"),
                            "video_processor": item.get("VideoProcessor"),
                            "adapter_memory_bytes": item.get("AdapterRAM"),
                            "driver": item.get("DriverVersion"),
                            "utilization_percent": None,
                            "temperature_c": None,
                            "source": "Win32_VideoController",
                        }
                    )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            LOGGER.info("La consulta de GPU no está disponible: %s", exc)
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=name,temperature.gpu,utilization.gpu,memory.total,memory.used,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    fields = [part.strip() for part in line.split(",")]
                    if len(fields) >= 6:
                        def number(value: str) -> float | None:
                            try:
                                return float(value)
                            except ValueError:
                                return None

                        live_data = {
                            "temperature_c": number(fields[1]),
                            "utilization_percent": number(fields[2]),
                            "vram_total_mb": number(fields[3]),
                            "vram_used_mb": number(fields[4]),
                            "power_watts": number(fields[5]),
                            "source": "nvidia-smi",
                        }
                        existing = next(
                            (
                                gpu for gpu in gpus
                                if _normalize_model_name(gpu.get("name") or "") == _normalize_model_name(fields[0])
                            ),
                            None,
                        )
                        if existing is None:
                            gpus.append({"name": fields[0], **live_data})
                        else:
                            existing.update(live_data)
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.debug("nvidia-smi no disponible: %s", exc)
    lhm_gpus: dict[str, dict[str, Any]] = {}
    for sensor in hardware_sensors:
        hardware_type = str(sensor.get("hardware_type") or "").casefold()
        if not hardware_type.startswith("gpu"):
            continue
        name = str(sensor.get("hardware") or "GPU")
        key = _normalize_model_name(name)
        entry = lhm_gpus.setdefault(
            key,
            {
                "name": name,
                "manufacturer": hardware_type.removeprefix("gpu").upper() or None,
                "utilization_percent": None,
                "temperature_c": None,
                "vram_total_mb": None,
                "vram_used_mb": None,
                "power_watts": None,
                "source": sensor.get("source"),
            },
        )
        sensor_name = str(sensor.get("name") or "").casefold()
        sensor_type = str(sensor.get("sensor_type") or "").casefold()
        value = sensor.get("value")
        if not isinstance(value, (int, float)):
            continue
        if sensor_type == "load" and entry["utilization_percent"] is None:
            entry["utilization_percent"] = float(value)
        elif sensor_type == "temperature":
            current = entry["temperature_c"]
            entry["temperature_c"] = max(current, float(value)) if current is not None else float(value)
        elif sensor_type == "power" and entry["power_watts"] is None:
            entry["power_watts"] = float(value)
        elif sensor_type in {"data", "smalldata"}:
            if "used" in sensor_name:
                entry["vram_used_mb"] = float(value) * (1024 if sensor_type == "data" else 1)
            elif "total" in sensor_name:
                entry["vram_total_mb"] = float(value) * (1024 if sensor_type == "data" else 1)
    for lhm_gpu in lhm_gpus.values():
        existing = next(
            (
                gpu for gpu in gpus
                if _normalize_model_name(gpu.get("name") or "")
                and (
                    _normalize_model_name(gpu["name"]) in _normalize_model_name(lhm_gpu["name"])
                    or _normalize_model_name(lhm_gpu["name"]) in _normalize_model_name(gpu["name"])
                )
            ),
            None,
        )
        if existing is None:
            gpus.append(lhm_gpu)
        else:
            for key, value in lhm_gpu.items():
                if value is not None and existing.get(key) is None:
                    existing[key] = value
    return gpus


def _read_battery_details() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    script = r"""
$ErrorActionPreference = 'Stop'
$design = $null
$full = $null
$cycles = $null
$temperature = $null
$charge = $null
$batteryStatus = $null
try {
  $win32 = @(Get-CimInstance Win32_Battery -ErrorAction Stop)
  if ($win32.Count) {
    if ($win32[0].DesignCapacity) { $design = [double]$win32[0].DesignCapacity }
    if ($win32[0].FullChargeCapacity) { $full = [double]$win32[0].FullChargeCapacity }
    if ($null -ne $win32[0].EstimatedChargeRemaining) { $charge = [double]$win32[0].EstimatedChargeRemaining }
    $batteryStatus = [int]$win32[0].BatteryStatus
  }
} catch {}
try {
  $static = @(Get-CimInstance -Namespace root\wmi -ClassName BatteryStaticData -ErrorAction Stop)
  $values = @($static | Where-Object {$_.DesignedCapacity -gt 0} | ForEach-Object {[double]$_.DesignedCapacity})
  if ($values.Count) { $design = ($values | Measure-Object -Sum).Sum }
} catch {}
try {
  $charged = @(Get-CimInstance -Namespace root\wmi -ClassName BatteryFullChargedCapacity -ErrorAction Stop)
  $values = @($charged | Where-Object {$_.FullChargedCapacity -gt 0} | ForEach-Object {[double]$_.FullChargedCapacity})
  if ($values.Count) { $full = ($values | Measure-Object -Sum).Sum }
} catch {}
try {
  $cycleRows = @(Get-CimInstance -Namespace root\wmi -ClassName BatteryCycleCount -ErrorAction Stop)
  $values = @($cycleRows | Where-Object {$null -ne $_.CycleCount} | ForEach-Object {[double]$_.CycleCount})
  if ($values.Count) { $cycles = ($values | Measure-Object -Sum).Sum }
} catch {}
try {
  $tempRows = @(Get-CimInstance -Namespace root\wmi -ClassName BatteryTemperature -ErrorAction Stop)
  $values = @($tempRows | Where-Object {$_.Temperature -gt 0} | ForEach-Object {[double]$_.Temperature})
  if ($values.Count) {
    $kelvinTenths = ($values | Measure-Object -Average).Average
    $candidate = $kelvinTenths / 10 - 273.15
    if ($candidate -gt -30 -and $candidate -lt 100) { $temperature = $candidate }
  }
} catch {}
[pscustomobject]@{
  design_capacity_mwh=$design
  full_charge_capacity_mwh=$full
  cycle_count=$cycles
  temperature_c=$temperature
  charge_percent=$charge
  battery_status=$batteryStatus
} | ConvertTo-Json -Compress
"""
    try:
        result = _powershell_json(script, timeout=8)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        LOGGER.debug("Datos extendidos de batería no disponibles: %s", exc)
        return {}
    if not isinstance(result, dict):
        return {}
    return {key: value for key, value in result.items() if value is not None}


def collect_system_snapshot() -> dict[str, Any]:
    """Collect live resource data. Unsupported sensors remain explicitly unavailable."""
    cpu_frequency = psutil.cpu_freq()
    memory = psutil.virtual_memory()
    disk_usage: list[dict[str, Any]] = []
    seen_devices: set[str] = set()
    for partition in psutil.disk_partitions(all=False):
        device = partition.device or partition.mountpoint
        if device in seen_devices:
            continue
        seen_devices.add(device)
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue
        disk_usage.append(
            {
                "device": device,
                "mountpoint": partition.mountpoint,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent": usage.percent,
            }
        )
    battery_info = None
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, OSError, NotImplementedError):
        battery = None
    if battery is not None:
        battery_info = {
            "percent": battery.percent,
            "plugged": battery.power_plugged,
            "seconds_left": battery.secsleft if battery.secsleft >= 0 else None,
        }
    battery_details = _read_battery_details()
    if battery_info is None and battery_details:
        battery_info = {
            "percent": battery_details.get("charge_percent"),
            "plugged": None,
            "seconds_left": None,
        }
    if battery_info is not None:
        design = battery_details.get("design_capacity_mwh")
        full = battery_details.get("full_charge_capacity_mwh")
        battery_info.update(
            {
                "design_capacity_mwh": design,
                "full_charge_capacity_mwh": full,
                "cycle_count": battery_details.get("cycle_count"),
                "cycle_count_source": "root\\wmi" if battery_details.get("cycle_count") is not None else None,
                "temperature_c": battery_details.get("temperature_c"),
                "wear_percent": (
                    round(max(0, min(100, 100 * (1 - full / design))), 1)
                    if isinstance(design, (int, float))
                    and isinstance(full, (int, float))
                    and design > 0
                    else None
                ),
                "capacity_source": (
                    "Win32_Battery / root\\wmi"
                    if design is not None or full is not None
                    else None
                ),
            }
        )
    try:
        disk_io = psutil.disk_io_counters()
        disk_activity = (
            {
                "read_bytes": disk_io.read_bytes,
                "write_bytes": disk_io.write_bytes,
                "read_count": disk_io.read_count,
                "write_count": disk_io.write_count,
            }
            if disk_io
            else None
        )
    except (OSError, PermissionError):
        disk_activity = None

    hardware_sensors, sensor_note = _read_hardware_sensors()
    temperatures, temperature_note = _read_temperatures(hardware_sensors, sensor_note)
    gpus = _read_gpu(hardware_sensors)
    for gpu in gpus:
        if gpu.get("temperature_c") is not None:
            gpu_name = _normalize_model_name(gpu.get("name") or "")
            has_hardware_temperature = any(
                str(sensor.get("hardware_type") or "").casefold().startswith("gpu")
                and (
                    not gpu_name
                    or gpu_name in _normalize_model_name(str(sensor.get("group") or ""))
                    or _normalize_model_name(str(sensor.get("group") or "")) in gpu_name
                )
                for sensor in temperatures
            )
            if has_hardware_temperature:
                continue
            temperatures.append(
                {
                    "name": gpu["name"],
                    "group": "GPU",
                    "hardware_type": "Gpu",
                    "celsius": gpu["temperature_c"],
                    "high": None,
                    "critical": None,
                    "source": gpu.get("source"),
                    "identifier": f"gpu:{gpu_name}:temperature",
                }
            )
    return {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cpu": {
            "utilization_percent": psutil.cpu_percent(interval=None),
            "frequency_mhz": cpu_frequency.current if cpu_frequency else None,
            "frequency_max_mhz": cpu_frequency.max if cpu_frequency else None,
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_threads": psutil.cpu_count(logical=True),
        },
        "memory": {
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "used_bytes": memory.used,
            "percent": memory.percent,
        },
        "disks": disk_usage,
        "disk_activity": disk_activity,
        "gpus": gpus,
        "battery": battery_info,
        "temperatures": temperatures,
        "temperature_note": temperature_note,
    }


def _signature_status(paths: list[str]) -> dict[str, dict[str, Any]]:
    unique_paths = list(dict.fromkeys(paths))
    if not unique_paths or sys.platform != "win32":
        return {}
    signatures: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(unique_paths), 16):
        batch = unique_paths[offset : offset + 16]
        encoded_paths = base64.b64encode(
            json.dumps(batch).encode("utf-8")
        ).decode("ascii")
        script = (
            "$ErrorActionPreference='Stop'; $json=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded_paths}')); "
            "$paths=ConvertFrom-Json -InputObject $json; $out=foreach($p in $paths) {"
            "$v=$null; $s=$null; try {$v=[System.Diagnostics.FileVersionInfo]::GetVersionInfo($p)} catch {} "
            "try {$s=Get-AuthenticodeSignature -LiteralPath $p -ErrorAction Stop} catch {} "
            "[pscustomobject]@{path=$p;status=if($s){[string]$s.Status}else{'Error'};"
            "signer=if($s.SignerCertificate){$s.SignerCertificate.Subject}else{$null};"
            "certificate_issuer=if($s.SignerCertificate){$s.SignerCertificate.Issuer}else{$null};"
            "certificate_thumbprint=if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{$null};"
            "certificate_expires=if($s.SignerCertificate){$s.SignerCertificate.NotAfter.ToString('o')}else{$null};"
            "manufacturer=if($v){$v.CompanyName}else{$null};description=if($v){$v.FileDescription}else{$null};"
            "product=if($v){$v.ProductName}else{$null};file_version=if($v){$v.FileVersion}else{$null};"
            "product_version=if($v){$v.ProductVersion}else{$null};original_filename=if($v){$v.OriginalFilename}else{$null}}}; "
            "$out | ConvertTo-Json -Compress"
        )
        try:
            rows = _powershell_json(script, timeout=90)
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                if isinstance(row, dict) and row.get("path"):
                    signatures[str(row["path"]).casefold()] = {
                        "status": str(row.get("status", "No verificada")),
                        "signer": row.get("signer"),
                        "certificate_issuer": row.get("certificate_issuer"),
                        "certificate_thumbprint": row.get("certificate_thumbprint"),
                        "certificate_expires": row.get("certificate_expires"),
                        "manufacturer": row.get("manufacturer"),
                        "description": row.get("description"),
                        "product": row.get("product"),
                        "file_version": row.get("file_version"),
                        "product_version": row.get("product_version"),
                        "original_filename": row.get("original_filename"),
                    }
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            LOGGER.warning("No se pudieron verificar algunas firmas digitales: %s", exc)
    return signatures


def collect_processes() -> list[dict[str, Any]]:
    """Inventory processes, including best-effort signatures and network endpoints."""
    processes: list[dict[str, Any]] = []
    executable_paths: list[str] = []
    now = time.time()
    total_memory = psutil.virtual_memory().total
    for proc in psutil.process_iter(
        ["pid", "name", "exe", "username", "memory_info", "create_time"]
    ):
        try:
            info = proc.info
            executable_path = info.get("exe")
            path = executable_path if executable_path and ntpath.isabs(executable_path) else None
            if path:
                executable_paths.append(path)
            created = info.get("create_time")
            processes.append(
                {
                    "name": info.get("name") or f"PID {info['pid']}",
                    "pid": info["pid"],
                    "path": path,
                    "user": info.get("username"),
                    "memory_bytes": info["memory_info"].rss if info.get("memory_info") else None,
                    "memory_percent_total": (
                        info["memory_info"].rss / total_memory * 100
                        if info.get("memory_info") and total_memory
                        else None
                    ),
                    "create_time": created,
                    "started_at": datetime.fromtimestamp(created).astimezone().isoformat(timespec="seconds") if created else None,
                    "runtime_seconds": max(0, now - created) if created else None,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
            LOGGER.debug("No se pudo consultar un proceso: %s", exc)
    signatures = _signature_status(executable_paths)
    connections_by_pid: dict[int, list[str]] = {}
    network_by_pid: dict[int, list[dict[str, Any]]] = {}
    try:
        for connection in psutil.net_connections(kind="inet"):
            if connection.pid is None or not connection.raddr:
                continue
            endpoint = f"{connection.raddr.ip}:{connection.raddr.port}"
            connections_by_pid.setdefault(connection.pid, []).append(endpoint)
            network_by_pid.setdefault(connection.pid, []).append(
                {
                    "local": f"{connection.laddr.ip}:{connection.laddr.port}" if connection.laddr else None,
                    "remote": endpoint,
                    "status": connection.status,
                    "type": connection.type,
                }
            )
    except (psutil.AccessDenied, OSError, AttributeError) as exc:
        LOGGER.info("No se pudieron enumerar conexiones de red: %s", exc)
    for item in processes:
        item["cpu_percent"] = 0.0
        item["signature"] = signatures.get(
            (item["path"] or "").casefold(), {"status": "No verificada", "signer": None}
        )
        item["network_endpoints"] = sorted(set(connections_by_pid.get(item["pid"], [])))
        item["network_connections"] = network_by_pid.get(item["pid"], [])
    # Measure per-process CPU over one short interval after slow metadata/signature queries.
    cpu_baseline: dict[int, tuple[float, float]] = {}
    sample_started = time.monotonic()
    for item in processes:
        try:
            cpu = psutil.Process(item["pid"]).cpu_times()
            cpu_baseline[item["pid"]] = (cpu.user + cpu.system, sample_started)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    time.sleep(0.15)
    elapsed = max(0.001, time.monotonic() - sample_started)
    cpu_by_pid: dict[int, float] = {}
    for item in processes:
        try:
            cpu = psutil.Process(item["pid"]).cpu_times()
            baseline = cpu_baseline.get(item["pid"])
            delta = max(0.0, cpu.user + cpu.system - (baseline[0] if baseline else cpu.user + cpu.system))
            cpu_by_pid[item["pid"]] = round(delta / elapsed * 100, 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            cpu_by_pid[item["pid"]] = 0.0
    for item in processes:
        item["cpu_percent"] = cpu_by_pid[item["pid"]]
    return processes


def collect_process_memory() -> list[dict[str, Any]]:
    """Capture lightweight RSS samples without repeating signature or connection scans."""
    total_memory = psutil.virtual_memory().total
    samples: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "memory_info", "create_time"]):
        try:
            info = proc.info
            memory_info = info.get("memory_info")
            if not memory_info:
                continue
            samples.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name") or f"PID {info['pid']}",
                    "create_time": info.get("create_time"),
                    "memory_bytes": memory_info.rss,
                    "memory_percent_total": (
                        memory_info.rss / total_memory * 100 if total_memory else None
                    ),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return sorted(samples, key=lambda item: item["memory_bytes"], reverse=True)


def collect_startup_audit() -> list[dict[str, Any]]:
    """Read services, startup commands and scheduled tasks; never modifies them."""
    if sys.platform != "win32":
        raise OSError("La auditoría de servicios e inicio solo está disponible en Windows.")
    script = r"""
$ErrorActionPreference = 'Continue'
$items = @()
$errors = @()
try {
  Get-CimInstance Win32_Service -ErrorAction Stop | ForEach-Object {
    $items += [pscustomobject]@{kind='Servicio';name=$_.Name;state=$_.State;location=$_.PathName;user=$_.StartName;start_mode=$_.StartMode}
  }
} catch { $errors += "Servicios: $($_.Exception.Message)" }
try {
  Get-CimInstance Win32_StartupCommand -ErrorAction Stop | ForEach-Object {
    $items += [pscustomobject]@{kind='Inicio';name=$_.Name;state='Configurado';location=$_.Command;user=$_.User;start_mode=$_.Location}
  }
} catch { $errors += "Entradas de inicio: $($_.Exception.Message)" }
try {
  Get-ScheduledTask -ErrorAction Stop | ForEach-Object {
    $actions = ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join '; '
    $items += [pscustomobject]@{kind='Tarea programada';name="$($_.TaskPath)$($_.TaskName)";state=[string]$_.State;location=$actions;user=$_.Principal.UserId;start_mode=$_.TaskPath}
  }
} catch { $errors += "Tareas programadas: $($_.Exception.Message)" }
[pscustomobject]@{items=$items;errors=$errors} | ConvertTo-Json -Compress -Depth 4
"""
    result = _powershell_json(script, timeout=60)
    if not isinstance(result, dict):
        raise RuntimeError("La auditoría de Windows devolvió una estructura inesperada.")
    rows = result.get("items") or []
    if isinstance(rows, dict):
        rows = [rows]
    audit = [dict(row) for row in rows if isinstance(row, dict)]
    errors = result.get("errors") or []
    if isinstance(errors, str):
        errors = [errors]
    audit.extend(
        {
            "kind": "Aviso",
            "name": "Área no consultada",
            "state": "No disponible",
            "location": "",
            "user": None,
            "collection_error": True,
            "error": str(error),
        }
        for error in errors
    )
    return audit


