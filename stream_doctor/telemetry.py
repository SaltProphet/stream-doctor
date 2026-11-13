from __future__ import annotations

import platform
import socket
import time
from typing import Any

import psutil


def hardware_snapshot() -> dict[str, Any]:
    return {"system": platform.platform(), "cpu": platform.processor() or "Unknown", "logical_cpus": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1024**3, 1), "gpus": _gpu_names(), "network_adapters": list(psutil.net_if_addrs())}


def _gpu_names() -> list[str]:
    if platform.system() != "Windows":
        return []
    try:
        import subprocess
        result = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"], capture_output=True, text=True, timeout=5, check=False)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def network_adapters() -> list[dict[str, Any]]:
    return [{"name": name, "addresses": [str(a.address) for a in addresses]} for name, addresses in psutil.net_if_addrs().items()]


def local_latency(host: str = "8.8.8.8", port: int = 443, timeout: float = 2) -> float | None:
    try:
        started = time.perf_counter()
        with socket.create_connection((host, port), timeout=timeout) as conn:
            conn.getpeername()
            return round((time.perf_counter() - started) * 1000, 3)
    except OSError:
        return None
