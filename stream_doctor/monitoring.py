from __future__ import annotations

from dataclasses import dataclass, replace
import asyncio
from datetime import datetime, timezone
import time
from typing import Any
from .video import FrameAnalyzer, analyze_png_data


@dataclass(frozen=True)
class MonitorSample:
    timestamp: str
    stream_active: bool
    reconnecting: bool
    bitrate_kbps: float | None
    dropped_frames: int | None
    total_frames: int | None
    render_lag_ms: float | None
    encoding_lag_ms: float | None
    network_loss_percent: float | None
    output_congestion: float | None = None
    new_skipped_frames: int | None = None
    congestion_samples: int = 0


@dataclass(frozen=True)
class Diagnosis:
    category: str
    explanation: str
    next_action: str


@dataclass(frozen=True)
class VideoObservation:
    brightness: float
    black_ratio: float
    is_black: bool
    is_frozen: bool
    available: bool
    source_name: str


class BlackoutTracker:
    def __init__(self, incident_store, persistence_samples: int = 3):
        self.incident_store = incident_store
        self.persistence_samples = max(1, persistence_samples)
        self._black_count = 0
        self._first_black_timestamp: str | None = None
        self._open_incident_id: int | None = None

    @property
    def active(self) -> bool:
        return self._open_incident_id is not None

    def interrupt(self, timestamp: str, reason: str) -> None:
        if self.active:
            start = datetime.fromisoformat(self._first_black_timestamp)
            duration = max(0.0, (datetime.fromisoformat(timestamp) - start).total_seconds())
            self.incident_store.close_incident(self._open_incident_id, timestamp, duration, "unverified", {"end_reason": reason})
        self._open_incident_id = None
        self._first_black_timestamp = None
        self._black_count = 0

    def observe(self, observation: VideoObservation, timestamp: str, context: dict[str, Any]) -> dict[str, Any] | None:
        if not observation.available or not context.get("stream_active"):
            return None
        if observation.is_black:
            if self._open_incident_id is not None:
                return None
            self._black_count += 1
            self._first_black_timestamp = self._first_black_timestamp or timestamp
            if self._black_count < self.persistence_samples:
                return None
            evidence = {**context, "source_name": observation.source_name, "brightness": observation.brightness, "black_ratio": observation.black_ratio, "persistence_samples": self._black_count, "intentional_scene": bool(context.get("intentional_scene", False))}
            self._open_incident_id = self.incident_store.start_incident("blackout", "Black video detected", evidence, self._first_black_timestamp)
            return {"incident_id": self._open_incident_id, "summary": "Black video detected", "start_timestamp": self._first_black_timestamp, "evidence": evidence}
        if self._open_incident_id is not None:
            start = datetime.fromisoformat(self._first_black_timestamp)
            end = datetime.fromisoformat(timestamp)
            duration = max(0.0, (end - start).total_seconds())
            evidence = {"recovery_scene": observation.source_name, "recovery_brightness": observation.brightness, "recovery_black_ratio": observation.black_ratio}
            self.incident_store.close_incident(self._open_incident_id, timestamp, duration, "recovered", evidence)
            event = {"incident_id": self._open_incident_id, "summary": "Black video recovered", "start_timestamp": self._first_black_timestamp, "end_timestamp": timestamp, "duration_seconds": duration, "recovery_status": "recovered", "evidence": evidence}
            self._open_incident_id = None
            self._black_count = 0
            self._first_black_timestamp = None
            return event
        self._black_count = 0
        self._first_black_timestamp = None
        return None


def diagnose_sample(sample: MonitorSample) -> Diagnosis:
    if not sample.stream_active:
        return Diagnosis("unknown", "The stream is inactive; there is insufficient evidence to attribute a failure.", "Start or inspect the test output manually.")
    if sample.reconnecting:
        return Diagnosis("ingest", "OBS reports an active reconnection attempt.", "Check the ingest connection and wait for stabilization.")
    if (sample.encoding_lag_ms or 0) >= 20:
        return Diagnosis("encoder", f"Encoding lag is {sample.encoding_lag_ms:.1f} ms, indicating encoder pressure.", "Reduce encoder load or output resolution/FPS.")
    if (sample.render_lag_ms or 0) >= 20:
        return Diagnosis("rendering", f"Rendering lag is {sample.render_lag_ms:.1f} ms, indicating render pressure.", "Reduce scene/rendering load.")
    if sample.network_loss_percent is not None and sample.network_loss_percent >= 1:
        return Diagnosis("network", f"Dropped frames coincide with measured network loss of {sample.network_loss_percent:.1f}%.", "Investigate the active adapter or reduce bitrate.")
    if sample.output_congestion is not None and sample.output_congestion >= 0.5 and sample.congestion_samples >= 3:
        return Diagnosis("network", f"OBS reports {sample.output_congestion:.0%} output congestion across {sample.congestion_samples} consecutive samples.", "Check upload stability and run Preflight when idle to evaluate a lower bitrate.")
    if sample.new_skipped_frames is not None and sample.new_skipped_frames > 0:
        return Diagnosis("output", f"OBS skipped {sample.new_skipped_frames} output frames since the previous sample. The cause is undetermined.", "Collect more samples before changing settings.")
    return Diagnosis("healthy", "No threshold breach is present in this sample.", "Continue monitoring.")


def sample_from_obs(stats: dict[str, Any], stream: dict[str, Any], network_loss_percent: float | None, timestamp: str) -> MonitorSample:
    render_total = stats.get("renderTotalFrames") or 0
    render_skipped = stats.get("renderSkippedFrames") or 0
    output_total = stream.get("outputTotalFrames", stats.get("outputTotalFrames"))
    output_skipped = stream.get("outputSkippedFrames", stats.get("outputSkippedFrames"))
    return MonitorSample(timestamp, bool(stream.get("outputActive")), bool(stream.get("outputReconnecting")), None, output_skipped, output_total, _lag(stats, render_total, render_skipped), None, network_loss_percent, stream.get("outputCongestion"))


def derive_bitrate_kbps(previous_bytes: int | float | None, current_bytes: int | float | None, elapsed_seconds: float) -> float | None:
    if previous_bytes is None or current_bytes is None or elapsed_seconds <= 0 or current_bytes < previous_bytes:
        return None
    return round((float(current_bytes) - float(previous_bytes)) * 8 / elapsed_seconds / 1000, 3)


def _lag(stats: dict[str, Any], total: int, skipped: int) -> float | None:
    frame_time = stats.get("averageFrameRenderTime")
    return float(frame_time) if isinstance(frame_time, (int, float)) else None


def _safe_obs_config(snapshot: dict[str, Any]) -> dict[str, Any]:
    video = snapshot.get("video", {}) or {}
    output = snapshot.get("output_settings", {}) or {}
    settings = output.get("outputSettings", {}) if isinstance(output, dict) else {}
    safe = {}
    for key in ("fpsNumerator", "fpsDenominator", "outputWidth", "outputHeight"):
        if key in video:
            safe[key] = video[key]
    for key in ("encoder", "outputEncoder", "bitrate", "rate_control", "rateControl"):
        if key in output:
            safe[key] = output[key]
        elif isinstance(settings, dict) and key in settings:
            safe[key] = settings[key]
    if isinstance(output, dict) and output.get("outputName"):
        safe["outputName"] = output["outputName"]
    output_items = (snapshot.get("outputs", {}) or {}).get("outputs", [])
    if output_items and output_items[0].get("outputName"):
        safe["outputName"] = output_items[0]["outputName"]
    return safe


class LiveMonitor:
    """Low-rate OBS polling loop with bounded reconnects and local incident logging."""
    def __init__(self, client_factory, incident_store, interval: float = 1.0, network_loss_provider=None, on_sample=None, blackout_tracker=None, intentional_static_sources: set[str] | None = None, intentional_scenes: set[str] | None = None):
        self.client_factory = client_factory
        self.incident_store = incident_store
        self.interval = interval
        self.network_loss_provider = network_loss_provider or (lambda: None)
        self.on_sample = on_sample
        self.blackout_tracker = blackout_tracker or BlackoutTracker(incident_store)
        self.frame_analyzer = FrameAnalyzer()
        self.intentional_static_sources = intentional_static_sources or set()
        self.intentional_scenes = intentional_scenes or set()
        self.samples_collected = 0
        self.reconnections = 0
        self.records: list[dict[str, Any]] = []
        self._stop = False
        self._previous_bytes: int | float | None = None
        self._previous_time: float | None = None
        self._was_active = False
        self._was_reconnecting = False
        self._broadcast_started_at: float | None = None
        self._has_observed_state = False
        self._previous_skipped = None
        self._congestion_samples = 0
        self._last_diagnosis = None

    async def run(self, max_samples: int | None = None) -> None:
        client = self.client_factory()
        try:
            while not self._stop and (max_samples is None or self.samples_collected < max_samples):
                try:
                    if getattr(client, "socket", None) is None:
                        await client.connect()
                        if self.reconnections:
                            self.incident_store.record("obs", "OBS WebSocket recovered", {"reconnections": self.reconnections})
                    timestamp = datetime.now(timezone.utc).isoformat()
                    snapshot = await client.snapshot()
                    sample = sample_from_obs(snapshot.get("stats", {}), snapshot.get("stream", {}), self.network_loss_provider(), timestamp)
                    if sample.stream_active and not sample.reconnecting:
                        skipped = sample.dropped_frames
                        previous = self._previous_skipped
                        delta = skipped - previous if skipped is not None and previous is not None and skipped >= previous else None
                        self._previous_skipped = skipped
                        if sample.output_congestion is not None and sample.output_congestion >= 0.5:
                            self._congestion_samples += 1
                        else:
                            self._congestion_samples = 0
                        sample = replace(sample, new_skipped_frames=delta, congestion_samples=self._congestion_samples)
                    else:
                        self._previous_skipped = None
                        self._congestion_samples = 0
                    now = time.monotonic()
                    current_bytes = snapshot.get("stream", {}).get("outputBytes")
                    sample = replace(sample, bitrate_kbps=derive_bitrate_kbps(self._previous_bytes, current_bytes, now - self._previous_time) if self._previous_time is not None else None)
                    self._previous_bytes, self._previous_time = current_bytes, now
                    video_observation = None
                    if sample.stream_active:
                        scene_name = snapshot.get("scene", {}).get("currentProgramSceneName")
                        if scene_name:
                            try:
                                screenshot = await client.capture_program_screenshot(scene_name)
                                frame = analyze_png_data(screenshot["imageData"], self.frame_analyzer)
                                video_observation = VideoObservation(frame.brightness, frame.black_ratio, frame.is_black, frame.is_frozen, True, scene_name)
                            except Exception as exc:
                                self.incident_store.record("video", "Black-screen observation unavailable", {"timestamp": timestamp, "scene": scene_name, "error": str(exc)})
                    if video_observation:
                        context = {"stream_active": sample.stream_active, "scene": video_observation.source_name, "bitrate_kbps": sample.bitrate_kbps, "dropped_frames": sample.dropped_frames, "render_lag_ms": sample.render_lag_ms, "intentional_static_source": video_observation.source_name in self.intentional_static_sources, "intentional_scene": video_observation.source_name in self.intentional_scenes}
                        video_event = self.blackout_tracker.observe(video_observation, timestamp, context)
                    else:
                        video_event = None
                        if not sample.stream_active:
                            self.blackout_tracker.interrupt(timestamp, "broadcast inactive")
                        elif not self.blackout_tracker.active:
                            self.blackout_tracker.interrupt(timestamp, "video observation unavailable")
                    diagnosis = diagnose_sample(sample)
                    self.samples_collected += 1
                    record = {"timestamp": timestamp, "sample": sample.__dict__, "diagnosis": diagnosis.__dict__, "video": video_observation.__dict__ if video_observation else None, "video_event": video_event, "blackout_active": self.blackout_tracker.active, "obs_config": _safe_obs_config(snapshot)}
                    self.records.append(record)
                    if len(self.records) > 600:
                        del self.records[0]
                    if self.on_sample:
                        self.on_sample(record)
                    if self._has_observed_state and sample.stream_active and not self._was_active:
                        self._broadcast_started_at = now
                        self.incident_store.record("stream", "Broadcast started", {"timestamp": timestamp, "sample": sample.__dict__})
                    if sample.reconnecting and not self._was_reconnecting:
                        self.incident_store.record("connection", "OBS reconnection started", {"timestamp": timestamp, "sample": sample.__dict__})
                    if not sample.reconnecting and self._was_reconnecting:
                        self.incident_store.record("connection", "OBS reconnection recovered", {"timestamp": timestamp, "sample": sample.__dict__})
                    if self._has_observed_state and not sample.stream_active and self._was_active:
                        duration = now - self._broadcast_started_at if self._broadcast_started_at is not None else None
                        self.incident_store.record("stream", "Broadcast ended", {"timestamp": timestamp, "duration_seconds": duration, "sample": sample.__dict__})
                        self._broadcast_started_at = None
                    self._was_active = sample.stream_active
                    self._was_reconnecting = sample.reconnecting
                    self._has_observed_state = True
                    if diagnosis.category not in ("healthy", "unknown") and diagnosis.category != self._last_diagnosis:
                        self.incident_store.record(diagnosis.category, diagnosis.explanation, {"timestamp": timestamp, "sample": sample.__dict__, "next_action": diagnosis.next_action})
                    self._last_diagnosis = diagnosis.category
                    await asyncio.sleep(self.interval)
                except Exception as exc:
                    self.reconnections += 1
                    self._previous_skipped = None
                    self._congestion_samples = 0
                    self._last_diagnosis = None
                    self._previous_bytes = self._previous_time = None
                    self.blackout_tracker.interrupt(datetime.now(timezone.utc).isoformat(), "OBS connection interrupted")
                    if self.on_sample:
                        self.on_sample({"_obs_connected": False, "diagnosis": {
                            "category": "connection", "explanation": "OBS WebSocket connection interrupted.",
                            "next_action": "Check OBS WebSocket and wait for reconnection.",
                        }})
                    self.incident_store.record("obs", "OBS monitoring connection interrupted", {"error": str(exc), "reconnections": self.reconnections, "timestamp": datetime.now(timezone.utc).isoformat()})
                    try:
                        await client.close()
                    except Exception:
                        pass
                    await asyncio.sleep(self.interval)
        finally:
            self.blackout_tracker.interrupt(datetime.now(timezone.utc).isoformat(), "monitoring ended")
            await client.close()

    def stop(self) -> None:
        self._stop = True
