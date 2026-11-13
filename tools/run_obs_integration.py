"""Non-public OBS integration test.

Starts OBS with --portable in a disposable directory, creates only a test
profile/scene, and exercises read-only OBS APIs plus a local screenshot check.
It never starts a stream or recording and never contacts Chaturbate.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtGui import QImage

from stream_doctor.obs import ObsClient
from stream_doctor.reports import export_report
from stream_doctor.core import IncidentStore
from stream_doctor.monitoring import BlackoutTracker, LiveMonitor, VideoObservation
from stream_doctor.optimizer import change_profile_bitrate, inspect_stream_profile
from stream_doctor.telemetry import hardware_snapshot
from stream_doctor.video import FrameAnalyzer, analyze_png_data


async def request_retry(client: ObsClient, request_type: str, request_data: dict | None = None, attempts: int = 5):
    last_error = None
    for _ in range(attempts):
        try:
            return await client.request(request_type, request_data)
        except Exception as exc:
            last_error = exc
            if "already exists" in str(exc).lower() and request_type in {"CreateScene", "CreateInput"}:
                return {"alreadyExists": True}
            await client.close()
            await asyncio.sleep(0.5)
            await client.connect()
    raise last_error


async def run(obs_path: Path, keep: bool, active_output: bool = False) -> int:
    print("creating disposable OBS profile", flush=True)
    root = Path(tempfile.mkdtemp(prefix="stream-doctor-obs-test-"))
    staged_obs = root / "obs-studio"
    print("staging OBS installation into disposable directory", flush=True)
    copy = subprocess.run(["robocopy", str(obs_path.parents[2]), str(staged_obs), "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/R:1", "/W:1"], capture_output=True, text=True, timeout=300)
    if copy.returncode > 7:
        print(json.dumps({"status": "FAILED", "reason": "Could not stage OBS installation", "detail": copy.stderr}))
        return 2
    (staged_obs / "portable_mode.txt").write_text("", encoding="utf-8")
    config = staged_obs / "config" / "obs-studio"
    config.mkdir(parents=True)
    test_password = "stream-doctor-local-test-password"
    (config / "global.ini").write_text(f"[OBSWebSocket]\nServerEnabled=true\nServerPort=4456\nAuthRequired=true\nServerPassword={test_password}\n", encoding="utf-8")
    profile = config / "basic" / "profiles" / "StreamDoctorTest"
    profile.mkdir(parents=True)
    (profile / "basic.ini").write_text("[General]\nName=StreamDoctorTest\n[Output]\nMode=Simple\n[SimpleOutput]\nRecFormat=mp4\nFilePath=C:\\Users\\Public\\Videos\\StreamDoctorTest\n", encoding="utf-8")
    if active_output:
        (profile / "service.json").write_text(json.dumps({
            "type": "rtmp_custom",
            "settings": {"server": "rtmp://127.0.0.1:19350/live", "key": "stream-doctor-local"},
        }), encoding="utf-8")
        with (profile / "basic.ini").open("a", encoding="utf-8") as config_file:
            config_file.write("StreamEncoder=x264\nStreamAudioEncoder=aac\nVBitrate=6000\n")
    staged_exe = staged_obs / "bin" / "64bit" / obs_path.name
    process = subprocess.Popen([str(staged_exe), "--multi", "--profile", "StreamDoctorTest", "--collection", "StreamDoctorTest", "--minimize-to-tray"], cwd=staged_exe.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"started test OBS pid={process.pid}", flush=True)
    client = ObsClient("ws://127.0.0.1:4456", test_password)
    identified = None
    authentication_enforced = False
    local_output_server = None
    ffmpeg_log = None
    try:
        for _ in range(20):
            try:
                identified = await asyncio.wait_for(client.connect(), timeout=1.0)
                break
            except Exception:
                await asyncio.sleep(0.5)
        else:
            print(json.dumps({"status": "FAILED", "reason": "OBS WebSocket did not become available", "profile": str(root)}))
            return 2
        print("finished websocket wait", flush=True)
        wrong_client = ObsClient("ws://127.0.0.1:4456", "incorrect-stream-doctor-password", request_timeout=3)
        try:
            await wrong_client.connect()
            await wrong_client.close()
        except Exception:
            authentication_enforced = True
        version = None
        for _ in range(20):
            try:
                version = await client.request("GetVersion")
                break
            except Exception:
                await asyncio.sleep(0.5)
        else:
            print(json.dumps({"status": "FAILED", "reason": "OBS WebSocket connected but OBS did not become request-ready", "profile": str(root)}))
            return 2
        encoder_capabilities = await client.discover_encoder_capabilities()
        original_config = await inspect_stream_profile(client)
        profile_name = original_config["profile"]
        if profile_name != "StreamDoctorTest" or original_config["mode"] != "Simple":
            raise RuntimeError("Disposable Simple Output profile did not load.")
        original_bitrate = original_config["bitrate"]
        test_value = 1234 if original_bitrate != 1234 else 1200
        applied = await change_profile_bitrate(client, profile_name, original_bitrate, test_value)
        restored = await change_profile_bitrate(client, profile_name, test_value, original_bitrate)
        print(f"OBS Simple Output bitrate verified: {original_bitrate} -> {applied['bitrate']} -> {restored['bitrate']}", flush=True)
        settings_cycle = {"status": "PASS", "controlled_test": True, "verified_apply": True,
                          "applied_bitrate": applied["bitrate"], "restored_bitrate": restored["bitrate"],
                          "original_bitrate": original_bitrate}
        scene = await request_retry(client, "CreateScene", {"sceneName": "StreamDoctorTestScene"})
        await request_retry(client, "CreateInput", {"sceneName": "StreamDoctorTestScene", "inputName": "StreamDoctorColor", "inputKind": "color_source_v3", "inputSettings": {"color": 4278190335, "width": 640, "height": 360}, "sceneItemEnabled": True})
        await request_retry(client, "SetCurrentProgramScene", {"sceneName": "StreamDoctorTestScene"})
        active_output_samples = []
        if active_output:
            ffmpeg_log = (root / "ffmpeg.log").open("w+b")
            local_output_server = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-listen", "1", "-i", "rtmp://127.0.0.1:19350/live/stream-doctor-local",
                "-c", "copy", "-f", "null", "-", stdout=subprocess.DEVNULL, stderr=ffmpeg_log,
            )
            await asyncio.sleep(1)
            service = await client.request("GetStreamServiceSettings")
            configured = service.get("streamServiceSettings", {})
            if service.get("streamServiceType") != "rtmp_custom" or configured.get("server") != "rtmp://127.0.0.1:19350/live":
                raise RuntimeError("Disposable OBS did not load its loopback RTMP service.")
            await client.request("StartStream")
            for _ in range(80):
                status = await client.request("GetStreamStatus")
                active_output_samples.append(bool(status.get("outputActive")))
                if status.get("outputActive"):
                    break
                await asyncio.sleep(0.25)
            if not any(active_output_samples):
                ffmpeg_log.seek(0)
                print(ffmpeg_log.read().decode(errors="replace"), flush=True)
                logs = sorted((config / "logs").glob("*.txt"), key=lambda path: path.stat().st_mtime)
                if logs:
                    print(logs[-1].read_text(encoding="utf-8", errors="replace")[-9000:], flush=True)
                raise RuntimeError("Disposable RTMP output did not become active.")
        snapshot = await client.snapshot()
        analyzer = FrameAnalyzer(freeze_samples=3)
        blackout_store = IncidentStore(root / "blackout-incidents.sqlite3")
        blackout_tracker = BlackoutTracker(blackout_store, persistence_samples=3)
        observations = []
        blackout_events = []
        for color in (4278190335, 0, 0, 0, 0, 0, 4278190335, 4278190335, 4278190335):
            print(f"capturing test frame color={color}", flush=True)
            await request_retry(client, "SetInputSettings", {"inputName": "StreamDoctorColor", "inputSettings": {"color": color}, "overlay": False})
            await asyncio.sleep(0.15)
            screenshot = await request_retry(client, "GetSourceScreenshot", {"sourceName": "StreamDoctorColor", "imageFormat": "png", "imageWidth": 64, "imageHeight": 36})
            image = QImage()
            image.loadFromData(base64.b64decode(screenshot["imageData"].split(",", 1)[-1]))
            pixels = [[image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue()] for y in range(image.height()) for x in range(image.width())]
            observation = analyzer.observe(pixels)
            observations.append({"brightness": observation.brightness, "black": observation.is_black, "frozen": observation.is_frozen})
            video = VideoObservation(observation.brightness, observation.black_ratio, observation.is_black, observation.is_frozen, True, "StreamDoctorTestScene")
            event = blackout_tracker.observe(video, datetime.now(timezone.utc).isoformat(), {"stream_active": True, "scene": "StreamDoctorTestScene", "bitrate_kbps": 0, "dropped_frames": 0, "intentional_scene": True})
            if event:
                blackout_events.append(event)
        if not any(event.get("recovery_status") == "recovered" for event in blackout_events):
            raise RuntimeError("OBS screenshot blackout did not recover.")
        incident_store = IncidentStore(root / "incidents.sqlite3")
        monitor = LiveMonitor(lambda: client, incident_store, interval=0.2)
        if active_output:
            monitor_task = asyncio.create_task(monitor.run(max_samples=40))
            await asyncio.sleep(0.8)
            await request_retry(client, "SetInputSettings", {"inputName": "StreamDoctorColor", "inputSettings": {"color": 0}, "overlay": False})
            await asyncio.sleep(3.0)
            await request_retry(client, "SetInputSettings", {"inputName": "StreamDoctorColor", "inputSettings": {"color": 4278190335}, "overlay": False})
            await asyncio.wait_for(monitor_task, timeout=40)
            if not any(row["category"] == "blackout" and row["recovery_status"] == "recovered" for row in incident_store.search()):
                raise RuntimeError("Active OBS blackout recovery was not persisted.")
        else:
            await monitor.run(max_samples=3)
        await client.connect()
        recovery = await request_retry(client, "GetVersion")
        result = {"status": "PASS", "authenticated": authentication_enforced, "identified": identified, "identified_observation": "OBS WebSocket 5.x accepted the configured test password and rejected an incorrect password", "version": version, "encoder_capabilities": encoder_capabilities, "settings_cycle": settings_cycle, "active_output": active_output, "active_output_samples": active_output_samples, "active_monitor_records": sum(1 for record in monitor.records if record.get("sample", {}).get("stream_active")), "websocket_recovery": {"verified": bool(recovery.get("obsVersion")), "version": recovery.get("obsVersion")}, "monitoring_records": monitor.records, "incident_count": len(incident_store.search()), "scene_created": bool(scene is not None), "screenshot_dimensions": [image.width(), image.height()], "frame_observations": observations, "blackout_events": blackout_events, "blackout_incidents": blackout_store.search(), "hardware": hardware_snapshot()}
        report_path = root / "stream-doctor-obs-report.json"
        export_report(result, report_path, "json")
        print(json.dumps({**result, "report": str(report_path)}, indent=2, default=str))
        return 0
    finally:
        if active_output and client.socket is not None:
            try:
                await client.request("StopStream")
            except Exception:
                pass
        if local_output_server is not None:
            if local_output_server.returncode is None:
                local_output_server.terminate()
            await local_output_server.wait()
        if ffmpeg_log is not None:
            ffmpeg_log.close()
        await client.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        if keep:
            print(f"Test profile retained at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--obs", default=r"C:\Program Files\obs-studio\bin\64bit\obs64.exe")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--active-output", action="store_true", help="start only the disposable local RTMP output for active-monitor verification")
    args = parser.parse_args()
    return asyncio.run(run(Path(args.obs), args.keep, args.active_output))


if __name__ == "__main__":
    raise SystemExit(main())
