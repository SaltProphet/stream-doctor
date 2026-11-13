"""Read-only local OBS integration workflow.

This script does not start/stop a stream or recording and does not change OBS
settings. Enable OBS WebSocket manually, then run it against the intended test
profile. It writes only sanitized diagnostic JSON locally.
"""
from __future__ import annotations

import argparse
import base64
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from PySide6.QtGui import QImage

from stream_doctor.core import IncidentStore
from stream_doctor.obs import ObsClient, ObsConnectionError
from stream_doctor.reports import export_report
from stream_doctor.monitoring import diagnose_sample, sample_from_obs, derive_bitrate_kbps
from stream_doctor.telemetry import hardware_snapshot, network_adapters


async def collect(url: str, duration: int, source: str | None) -> dict:
    client = ObsClient(url, None)
    await client.connect()
    try:
        snapshots = []
        previous_bytes = None
        previous_time = None
        frame_observations = []
        analyzer = None
        if source:
            from stream_doctor.video import FrameAnalyzer
            analyzer = FrameAnalyzer()
        for _ in range(max(1, duration)):
            snapshot = await client.snapshot()
            timestamp = datetime.now(timezone.utc).isoformat()
            sample = sample_from_obs(snapshot["stats"], snapshot["stream"], None, timestamp)
            now = time.monotonic()
            current_bytes = snapshot["stream"].get("outputBytes")
            observed_bitrate = derive_bitrate_kbps(previous_bytes, current_bytes, now - previous_time) if previous_time is not None else None
            previous_bytes, previous_time = current_bytes, now
            item = {"timestamp": timestamp, "obs": snapshot, "observed_bitrate_kbps": observed_bitrate, "diagnosis": diagnose_sample(sample).__dict__}
            snapshots.append(item)
            if analyzer:
                try:
                    screenshot = await client.request("GetSourceScreenshot", {"sourceName": source, "imageFormat": "png", "imageWidth": 64, "imageHeight": 36})
                    image = QImage()
                    image.loadFromData(base64.b64decode(screenshot["imageData"].split(",", 1)[-1]))
                    pixels = [[image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue()] for y in range(image.height()) for x in range(image.width())]
                    observation = analyzer.observe(pixels)
                    frame_observations.append({"timestamp": timestamp, "brightness": observation.brightness, "black_ratio": observation.black_ratio, "is_black": observation.is_black, "is_frozen": observation.is_frozen})
                except Exception as exc:
                    frame_observations.append({"timestamp": timestamp, "error": str(exc)})
            await asyncio.sleep(1)
        return {"integration": "real OBS WebSocket", "source_requested": source, "samples": snapshots, "frame_observations": frame_observations, "hardware": hardware_snapshot(), "network_adapters": network_adapters()}
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:4455")
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--source")
    parser.add_argument("--output", type=Path, default=Path("test-artifacts/obs-integration.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = asyncio.run(collect(args.url, args.seconds, args.source))
    except ObsConnectionError as exc:
        print(json.dumps({"status": "UNAVAILABLE", "url": args.url, "reason": str(exc), "action": "Enable OBS WebSocket manually for the dedicated test profile, then rerun."}, indent=2))
        return 3
    export_report(report, args.output, "json")
    print(json.dumps({"status": "PASS", "output": str(args.output), "samples": len(report["samples"]), "stream_active": [s["obs"]["stream"].get("outputActive") for s in report["samples"]]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
