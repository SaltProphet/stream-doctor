"""Run uncapped/current and capped/current/lower OBS delivery trials locally."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stream_doctor.obs import ObsClient
from stream_doctor.optimizer import change_profile_bitrate, inspect_stream_profile
from tools.run_obs_integration import ensure_first_run_enabled, request_retry, redact_text, utc_now


ROOT = Path(tempfile.gettempdir()) / "stream-doctor-obs-test"
OBS_PROFILE = "StreamDoctorTest"
OBS_KEY = "stream-doctor-local"
OBS_SERVER = "rtmp://127.0.0.1:19350/live"
CAP_BPS = 500_000  # 4 Mbps: below current ~6.3 Mbps media, above 3 Mbps + audio.
LOWER_BITRATE = 3000
WINDOW_SECONDS = 15


async def wait_listening(process: asyncio.subprocess.Process, port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"receiver exited before listening: {process.returncode}")
        if any(c.pid == process.pid and c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN for c in psutil.net_connections(kind="tcp")):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"receiver did not listen on {port}")


def stage_runtime(obs_path: Path) -> Path:
    staged = ROOT / "obs-studio"
    executable = staged / "bin" / "64bit" / obs_path.name
    if not executable.exists():
        ROOT.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["robocopy", str(obs_path.parents[2]), str(staged), "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/R:1", "/W:1"], capture_output=True, text=True, timeout=300)
        if result.returncode > 7:
            raise RuntimeError(result.stderr or "could not stage OBS")
    config = staged / "config" / "obs-studio"
    import shutil
    shutil.rmtree(config, ignore_errors=True)
    config.mkdir(parents=True, exist_ok=True)
    ensure_first_run_enabled(config / "user.ini")
    (config / "global.ini").write_text("[OBSWebSocket]\nServerEnabled=true\nServerPort=4456\nAuthRequired=true\nServerPassword=stream-doctor-local-test-password\n", encoding="utf-8")
    profile = config / "basic" / "profiles" / OBS_PROFILE
    profile.mkdir(parents=True)
    (profile / "basic.ini").write_text("[General]\nName=StreamDoctorTest\n[Output]\nMode=Simple\n[SimpleOutput]\nRecFormat=mp4\nFilePath=C:\\Users\\Public\\Videos\\StreamDoctorTest\nStreamEncoder=x264\nStreamAudioEncoder=aac\nVBitrate=6000\n", encoding="utf-8")
    (profile / "service.json").write_text(json.dumps({"type": "rtmp_custom", "settings": {"server": OBS_SERVER, "key": OBS_KEY}}), encoding="utf-8")
    return staged


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is not None:
        if process.returncode is None:
            process.terminate()
        await process.wait()


def parse_progress(path: Path) -> dict[str, str]:
    result = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                result[key] = value
    return result


async def trial(client: ObsClient, bitrate: int, cap_bps: int, trial_name: str, root: Path) -> dict:
    profile = await inspect_stream_profile(client)
    if profile["bitrate"] != bitrate:
        raise RuntimeError(f"bitrate read-back mismatch before {trial_name}: {profile}")
    receiver_port = 19351
    progress_path = root / f"{trial_name}-progress.txt"
    stderr_path = root / f"{trial_name}-ffmpeg.log"
    stats_path = root / f"{trial_name}-proxy.json"
    receiver = await asyncio.create_subprocess_exec("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-listen", "1", "-i", f"rtmp://127.0.0.1:{receiver_port}/live/{OBS_KEY}", "-progress", str(progress_path), "-c", "copy", "-f", "null", "-", stdout=subprocess.DEVNULL, stderr=stderr_path.open("w", encoding="utf-8"))
    proxy = await asyncio.create_subprocess_exec(sys.executable, "tools/rtmp_cap_proxy.py", "--listen-port", "19350", "--target-port", str(receiver_port), "--bytes-per-second", str(cap_bps), "--stats", str(stats_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await wait_listening(receiver, receiver_port)
        await wait_listening(proxy, 19350)
        event_start = len(client.event_log)
        await client.request("SetStreamServiceSettings", {"streamServiceType": "rtmp_custom", "streamServiceSettings": {"server": OBS_SERVER, "key": OBS_KEY}})
        await client.request("StartStream")
        samples = []
        colors = [4278190335, 65535, 16711935, 4294901760, 16776960, 4278255360]
        started = time.monotonic()
        index = 0
        while time.monotonic() - started < WINDOW_SECONDS:
            await client.request("SetInputSettings", {"inputName": "StreamDoctorColor", "inputSettings": {"color": colors[index % len(colors)]}, "overlay": False})
            samples.append({"timestamp": utc_now(), "status": await client.request("GetStreamStatus")})
            index += 1
            await asyncio.sleep(0.5)
        await client.request("StopStream")
        for _ in range(20):
            if not (await client.request("GetStreamStatus")).get("outputActive"):
                break
            await asyncio.sleep(0.25)
        events = client.event_log[event_start:]
        statuses = [sample["status"] for sample in samples if sample["status"].get("outputActive")]
        skipped = [status.get("outputSkippedFrames", 0) for status in statuses]
        reconnects = sum(1 for status in statuses if status.get("outputReconnecting"))
        baseline_congestion = statuses[0].get("outputCongestion", 0.0) if statuses else 0.0
        congestion_delta = max((max(0.0, status.get("outputCongestion", 0.0) - baseline_congestion) for status in statuses), default=0.0)
        progress = parse_progress(progress_path)
        stderr = redact_text(stderr_path.read_text(encoding="utf-8", errors="replace"))
        return {
            "name": trial_name,
            "bitrate_kbps": bitrate,
            "cap_bps": cap_bps,
            "window_seconds": WINDOW_SECONDS,
            "samples": samples,
            "max_congestion": max((status.get("outputCongestion", 0.0) for status in statuses), default=0.0),
            "baseline_congestion": baseline_congestion,
            "max_congestion_delta": congestion_delta,
            "newly_skipped_output_frames": max(skipped, default=0) - min(skipped, default=0),
            "reconnect_samples": reconnects,
            "stream_events": events,
            "ffmpeg_progress": progress,
            "ffmpeg_media_received": int(progress.get("frame", "0") or 0) > 0,
            "ffmpeg_frames": int(progress.get("frame", "0") or 0),
            "ffmpeg_out_time_ms": int(progress.get("out_time_ms", "0") or 0),
            "ffmpeg_accept_log": next((line for line in stderr.splitlines() if "Input #0" in line), None),
            "proxy_stats": json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {},
        }
    finally:
        await stop_process(proxy)
        await stop_process(receiver)
        stderr_path.touch(exist_ok=True)


async def run(obs_path: Path) -> dict:
    staged = stage_runtime(obs_path)
    process = subprocess.Popen([str(staged / "bin" / "64bit" / obs_path.name), "--multi", "--profile", OBS_PROFILE, "--collection", "StreamDoctorTest", "--minimize-to-tray"], cwd=staged / "bin" / "64bit")
    client = ObsClient("ws://127.0.0.1:4456", "stream-doctor-local-test-password")
    root = ROOT / "trials"
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        for _ in range(30):
            try:
                await client.connect()
                break
            except Exception:
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("OBS WebSocket did not become available")
        await request_retry(client, "CreateScene", {"sceneName": "StreamDoctorTestScene"})
        await request_retry(client, "CreateInput", {"sceneName": "StreamDoctorTestScene", "inputName": "StreamDoctorColor", "inputKind": "color_source_v3", "inputSettings": {"color": 4278190335, "width": 640, "height": 360}, "sceneItemEnabled": True})
        await request_retry(client, "SetCurrentProgramScene", {"sceneName": "StreamDoctorTestScene"})
        original = await inspect_stream_profile(client)
        current = original["bitrate"]
        if current != 6000:
            raise RuntimeError(f"unexpected current bitrate: {current}")
        trials = [await trial(client, current, 0, "uncapped-current", root)]
        capped_current = await trial(client, current, CAP_BPS, "capped-current", root)
        uncapped = trials[0]
        cap_reproduced_failure = (
            capped_current["max_congestion_delta"] > uncapped["max_congestion_delta"]
            or capped_current["newly_skipped_output_frames"] > uncapped["newly_skipped_output_frames"]
            or capped_current["reconnect_samples"] > uncapped["reconnect_samples"]
            or capped_current["ffmpeg_out_time_ms"] < uncapped["ffmpeg_out_time_ms"]
        )
        if not cap_reproduced_failure:
            raise RuntimeError("Capped current-bitrate trial did not reproduce a measurable delivery failure.")
        trials.append(capped_current)
        await change_profile_bitrate(client, OBS_PROFILE, current, LOWER_BITRATE)
        readback = await inspect_stream_profile(client)
        if readback["bitrate"] != LOWER_BITRATE:
            raise RuntimeError(f"lower bitrate read-back failed: {readback}")
        trials.append(await trial(client, LOWER_BITRATE, CAP_BPS, "capped-lower", root))
        restored = await change_profile_bitrate(client, OBS_PROFILE, LOWER_BITRATE, current)
        final = await inspect_stream_profile(client)
        return {"status": "PASS", "original_bitrate_kbps": current, "lower_bitrate_kbps": LOWER_BITRATE, "cap_bps": CAP_BPS, "cap_mbps": CAP_BPS * 8 / 1_000_000, "cap_above_lower_total_bitrate": CAP_BPS * 8 > (LOWER_BITRATE + 160) * 1000, "cap_reproduced_failure": cap_reproduced_failure, "trials": trials, "restored": restored, "final_bitrate_kbps": final["bitrate"], "correction_improved_delivery": trials[2]["ffmpeg_media_received"] and (trials[2]["newly_skipped_output_frames"] < trials[1]["newly_skipped_output_frames"] or trials[2]["max_congestion_delta"] < trials[1]["max_congestion_delta"] or trials[2]["ffmpeg_out_time_ms"] > trials[1]["ffmpeg_out_time_ms"]), "report": str(root / "trials-report.json")}
    finally:
        await client.close()
        process.terminate()
        process.wait(timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--obs", type=Path, default=Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"))
    args = parser.parse_args()
    result = asyncio.run(run(args.obs))
    report = ROOT / "trials" / "trials-report.json"
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
