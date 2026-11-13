"""Shared, explicit measurements consumed by diagnostics and optimization."""

from __future__ import annotations

from typing import Any
from datetime import datetime, timezone


def fresh_upload(network: dict[str, Any]) -> float | None:
    measured_at = network.get("upload_measured_at")
    if not measured_at:
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(measured_at)).total_seconds()
    except (TypeError, ValueError):
        return None
    return network.get("upload_mbps") if 0 <= age < 900 else None


def build_optimizer_inputs(
    *,
    sample: dict[str, Any],
    obs_config: dict[str, Any],
    hardware: dict[str, Any],
    network: dict[str, Any],
    incidents: list[dict[str, Any]],
) -> dict[str, Any]:
    numerator = obs_config.get("fpsNumerator")
    denominator = obs_config.get("fpsDenominator") or 1
    fps = None
    if isinstance(numerator, (int, float)) and isinstance(denominator, (int, float)) and denominator:
        fps = float(numerator) / float(denominator)

    width = obs_config.get("outputWidth") or obs_config.get("output_width")
    height = obs_config.get("outputHeight") or obs_config.get("output_height")
    resolution = f"{width}x{height}" if width and height else None

    return {
        "obs": {
            "bitrate_kbps": obs_config.get("bitrate"),
            "fps": fps,
            "resolution": resolution,
            "encoder": obs_config.get("encoder") or obs_config.get("outputEncoder"),
        },
        "hardware": {
            "cpu": hardware.get("cpu"),
            "logical_cpus": hardware.get("logical_cpus"),
            "ram_gb": hardware.get("ram_gb"),
            "gpus": hardware.get("gpus", []),
            "encoder_capabilities": hardware.get("encoder_capabilities"),
        },
        "network": {
            "latency_ms": network.get("latency_ms"),
            "jitter_ms": network.get("jitter_ms"),
            "packet_loss_percent": network.get("packet_loss_percent"),
            "upload_mbps": fresh_upload(network),
        },
        "incidents": incidents,
    }
