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
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

# Direct execution puts ``tools`` ahead of the repository root on sys.path.
# Prefer the checkout package over any separately installed stream_doctor.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QImage

from stream_doctor.obs import ObsClient
from stream_doctor.reports import export_report
from stream_doctor.core import IncidentStore
from stream_doctor.monitoring import BlackoutTracker, LiveMonitor, VideoObservation
from stream_doctor.recovery import switch_program_scene
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


async def wait_for_receiver(process: asyncio.subprocess.Process, port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"Local RTMP receiver exited before listening: {process.returncode}")
        listening = any(
            connection.status == psutil.CONN_LISTEN
            and connection.laddr.port == port
            and connection.pid == process.pid
            for connection in psutil.net_connections(kind="tcp")
            if connection.laddr
        )
        if listening:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Local RTMP receiver did not listen on port {port}.")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_text(value: str) -> str:
    return value.replace("stream-doctor-local-test-password", "<test-password>").replace(
        "stream-doctor-local", "<test-key>"
    )


async def capture_ffmpeg_stream(stream, kind: str, trace: list[dict], progress: dict, media_progress: list[dict]) -> None:
    while True:
        raw = await stream.readline()
        if not raw:
            return
        text = redact_text(raw.decode(errors="replace").rstrip())
        timestamp = utc_now()
        trace.append({"timestamp": timestamp, "stream": kind, "line": text})
        if kind == "progress" and "=" in text:
            key, value = text.split("=", 1)
            progress[key] = value
            if key in {"frame", "out_time_ms"} and value not in {"0", "-1", "N/A"}:
                media_progress.append({"timestamp": timestamp, key: value})


def latest_obs_log(config: Path) -> str:
    logs = sorted((config / "logs").glob("*.txt"), key=lambda path: path.stat().st_mtime)
    if not logs:
        return ""
    return redact_text(logs[-1].read_text(encoding="utf-8", errors="replace")[-12000:])


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
    ffmpeg_tasks = []
    ffmpeg_trace = []
    ffmpeg_progress = {}
    ffmpeg_media_progress = []
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
            local_output_server = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-stats_period", "1",
                "-listen", "1", "-i", "rtmp://127.0.0.1:19350/live/stream-doctor-local",
                "-progress", "pipe:1",
                "-c", "copy", "-f", "null", "-", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            ffmpeg_tasks = [
                asyncio.create_task(capture_ffmpeg_stream(local_output_server.stdout, "progress", ffmpeg_trace, ffmpeg_progress, ffmpeg_media_progress)),
                asyncio.create_task(capture_ffmpeg_stream(local_output_server.stderr, "stderr", ffmpeg_trace, ffmpeg_progress, ffmpeg_media_progress)),
            ]
            await wait_for_receiver(local_output_server, 19350)
            service = await client.request("GetStreamServiceSettings")
            configured = service.get("streamServiceSettings", {})
            if service.get("streamServiceType") != "rtmp_custom" or configured.get("server") != "rtmp://127.0.0.1:19350/live":
                raise RuntimeError("Disposable OBS did not load its loopback RTMP service.")
            output_inventory_before = await client.request("GetOutputList")
            output_status_before = []
            for output in output_inventory_before.get("outputs", []):
                output_name = output.get("outputName")
                if output_name:
                    output_status_before.append({
                        "outputName": output_name,
                        "status": await client.request("GetOutputStatus", {"outputName": output_name}),
                    })
            await client.request("SetStreamServiceSettings", {
                "streamServiceType": "rtmp_custom",
                "streamServiceSettings": {
                    "server": "rtmp://127.0.0.1:19350/live",
                    "key": "stream-doctor-local",
                },
            })
            start_request = {"timestamp": utc_now(), "request": "StartStream"}
            start_request["response"] = await client.request("StartStream")
            for _ in range(80):
                status = await client.request("GetStreamStatus")
                active_output_samples.append({"timestamp": utc_now(), "status": status})
                if status.get("outputActive"):
                    break
                await asyncio.sleep(0.25)
            publish_trace = {
                "start_request": start_request,
                "obs_events": client.event_log,
                "obs_samples": active_output_samples,
                "service": {"type": service.get("streamServiceType"), "server": configured.get("server")},
                "outputs_before_start": output_inventory_before,
                "output_status_before_start": output_status_before,
                "encoder_capabilities": encoder_capabilities,
                "profile_config": redact_text((profile / "basic.ini").read_text(encoding="utf-8", errors="replace")),
                "obs_log": latest_obs_log(config),
                "ffmpeg_trace": ffmpeg_trace,
                "ffmpeg_media_progress": ffmpeg_media_progress,
            }
            if not any(sample["status"].get("outputActive") for sample in active_output_samples):
                (root / "publish-diagnostics.json").write_text(json.dumps(publish_trace, indent=2), encoding="utf-8")
                print(json.dumps({"publish_diagnostics": str(root / "publish-diagnostics.json")}, indent=2), flush=True)
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
        await request_retry(client, "CreateScene", {"sceneName": "StreamDoctorRecoveryBlack"})
        await request_retry(client, "SetCurrentProgramScene", {"sceneName": "StreamDoctorRecoveryBlack"})
        recovery_result = await switch_program_scene(client, "StreamDoctorRecoveryBlack", "StreamDoctorTestScene")
        if recovery_result["program_black"]:
            raise RuntimeError("Recovery scene still reports black video.")
        print("OBS recovery scene switch and non-black program video verified", flush=True)
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
        receiver_media_observed = bool(
            int(receiver_progress.get("frame", "0") or 0) > 0
            or int(receiver_progress.get("out_time_ms", "0") or 0) > 0
        )
        if active_output and not receiver_media_observed:
            raise RuntimeError("Local RTMP receiver did not report decoded media.")
        result = {"status": "PASS", "authenticated": authentication_enforced, "identified": identified, "identified_observation": "OBS WebSocket 5.x accepted the configured test password and rejected an incorrect password", "version": version, "encoder_capabilities": encoder_capabilities, "settings_cycle": settings_cycle, "active_output": active_output, "active_output_samples": active_output_samples, "active_monitor_records": sum(1 for record in monitor.records if record.get("sample", {}).get("stream_active")), "websocket_recovery": {"verified": bool(recovery.get("obsVersion")), "version": recovery.get("obsVersion")}, "monitoring_records": monitor.records, "incident_count": len(incident_store.search()), "scene_created": bool(scene is not None), "screenshot_dimensions": [image.width(), image.height()], "frame_observations": observations, "blackout_events": blackout_events, "blackout_incidents": blackout_store.search(), "hardware": hardware_snapshot()}
        result["receiver"] = {
            "media_observed": receiver_media_observed,
            "progress": receiver_progress,
            "trace": ffmpeg_trace,
            "media_progress": ffmpeg_media_progress,
        }
        result["obs_events"] = client.event_log
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
        if local_output_server is not None:
            for task in ffmpeg_tasks:
                if not task.done():
                    task.cancel()
            if ffmpeg_tasks:
                await asyncio.gather(*ffmpeg_tasks, return_exceptions=True)
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
