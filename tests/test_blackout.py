from datetime import datetime, timedelta, timezone

from stream_doctor.core import IncidentStore
from stream_doctor.monitoring import BlackoutTracker, VideoObservation


def _time(seconds: int) -> str:
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_sustained_black_video_creates_one_incident_and_closes_on_recovery(tmp_path):
    store = IncidentStore(tmp_path / "incidents.sqlite3")
    tracker = BlackoutTracker(store, persistence_samples=3)
    context = {"stream_active": True, "scene": "Camera", "bitrate_kbps": 3500, "dropped_frames": 2}
    black = VideoObservation(0, 1.0, True, False, True, "Camera")
    assert tracker.observe(black, _time(0), context) is None
    assert tracker.observe(black, _time(1), context) is None
    started = tracker.observe(black, _time(2), context)
    assert started is not None
    assert started["summary"] == "Black video detected"
    assert started["incident_id"] == 1
    assert tracker.observe(black, _time(3), context) is None
    recovered = tracker.observe(VideoObservation(80, 0.0, False, False, True, "Camera"), _time(5), context)
    assert recovered["duration_seconds"] == 5.0
    rows = store.search("Black video")
    assert len(rows) == 1
    assert rows[0]["end_timestamp"] is not None
    assert rows[0]["recovery_status"] == "recovered"
    assert rows[0]["evidence"]["brightness"] == 0
    assert rows[0]["evidence"]["black_ratio"] == 1.0
    assert rows[0]["evidence"]["recovery_brightness"] == 80


def test_intentional_black_scene_is_recorded_with_evidence(tmp_path):
    store = IncidentStore(tmp_path / "incidents.sqlite3")
    tracker = BlackoutTracker(store, persistence_samples=1)
    event = tracker.observe(VideoObservation(0, 1, True, False, True, "Black Test Scene"), _time(0), {"stream_active": True, "scene": "Black Test Scene", "intentional_scene": True})
    assert event["evidence"]["intentional_scene"] is True


def test_static_frozen_source_does_not_create_blackout(tmp_path):
    store = IncidentStore(tmp_path / "incidents.sqlite3")
    tracker = BlackoutTracker(store, persistence_samples=1)
    event = tracker.observe(VideoObservation(80, 0, False, True, True, "Static Overlay"), _time(0), {"stream_active": True, "scene": "Static Overlay", "intentional_static_source": True})
    assert event is None


def test_live_monitor_uses_program_scene_screenshots_and_recovers(tmp_path, monkeypatch):
    import asyncio
    from stream_doctor.monitoring import LiveMonitor

    class Client:
        socket = object()
        def __init__(self):
            self.frames = iter([True, True, False])
        async def snapshot(self):
            return {"stats": {}, "stream": {"outputActive": True}, "scene": {"currentProgramSceneName": "Program"}, "output": {}}
        async def capture_program_screenshot(self, scene_name):
            assert scene_name == "Program"
            return {"imageData": "unused"}
        async def close(self):
            pass

    def fake_analyze(_data, _analyzer):
        black = next(client.frames)
        return type("Frame", (), {"brightness": 0 if black else 90, "black_ratio": 1.0 if black else 0.0, "is_black": black, "is_frozen": False})()

    monkeypatch.setattr("stream_doctor.monitoring.analyze_png_data", fake_analyze)
    store = IncidentStore(tmp_path / "incidents.sqlite3")
    client = Client()
    monitor = LiveMonitor(lambda: client, store, interval=0, blackout_tracker=BlackoutTracker(store, persistence_samples=2))
    asyncio.run(monitor.run(max_samples=3))
    incidents = store.search("Black video")
    assert len(incidents) == 1
    assert incidents[0]["recovery_status"] == "recovered"
