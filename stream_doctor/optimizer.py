from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProfileBackup:
    """Back up an OBS profile directory without editing secrets or originals."""
    def __init__(self, backup_root: str | Path):
        self.root = Path(backup_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, profile_dir: str | Path) -> Path:
        source = Path(profile_dir)
        destination = self.root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copytree(source, destination)
        return destination

    def rollback(self, backup_dir: str | Path, profile_dir: str | Path) -> None:
        target = Path(profile_dir)
        for child in target.iterdir():
            if child.is_file():
                child.unlink()
        for child in Path(backup_dir).iterdir():
            destination = target / child.name
            if child.is_file():
                shutil.copy2(child, destination)


def verify_settings(before: dict[str, Any], after: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    mismatches = {key: {"expected": value, "actual": after.get(key)} for key, value in requested.items() if after.get(key) != value}
    return {"verified": not mismatches, "mismatches": mismatches, "before": before, "after": after}


SUPPORTED_OUTPUT_SETTINGS = {"bitrate", "encoder", "rate_control", "rateControl"}


def _safe_settings(settings: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: settings[key] for key in keys if key in settings}


async def apply_supported_output_settings(client, output_name: str, requested: dict[str, Any]) -> dict[str, Any]:
    """Apply explicitly requested safe output settings and verify OBS read-back.

    The caller must provide a measured recommendation and explicit user intent.
    Secrets and unrelated output settings are never returned as backup data.
    """
    unsupported = set(requested) - SUPPORTED_OUTPUT_SETTINGS
    if unsupported:
        raise ValueError(f"Unsupported OBS output settings: {sorted(unsupported)}")
    before_response = await client.request("GetOutputSettings", {"outputName": output_name})
    before_settings = dict(before_response.get("outputSettings", {}))
    backup = _safe_settings(before_settings, set(requested))
    updated = dict(before_settings)
    updated.update(requested)
    await client.request("SetOutputSettings", {"outputName": output_name, "outputSettings": updated})
    after_response = await client.request("GetOutputSettings", {"outputName": output_name})
    after_settings = dict(after_response.get("outputSettings", {}))
    result = verify_settings(_safe_settings(before_settings, set(requested)), _safe_settings(after_settings, set(requested)), requested)
    result.update({"backup": backup, "before": _safe_settings(before_settings, set(requested)), "after": _safe_settings(after_settings, set(requested))})
    if not result["verified"]:
        await rollback_output_settings(client, output_name, backup)
    return result


async def rollback_output_settings(client, output_name: str, backup: dict[str, Any]) -> None:
    if set(backup) - SUPPORTED_OUTPUT_SETTINGS:
        raise ValueError("Rollback contains unsupported OBS output settings")
    current_response = await client.request("GetOutputSettings", {"outputName": output_name})
    updated = dict(current_response.get("outputSettings", {}))
    updated.update(backup)
    await client.request("SetOutputSettings", {"outputName": output_name, "outputSettings": updated})
