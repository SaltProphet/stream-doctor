from stream_doctor.monitoring import MonitorSample, diagnose_sample, derive_bitrate_kbps
import asyncio


def test_diagnosis_attributes_dropped_frames_to_network_when_encoder_is_healthy():
    sample = MonitorSample(
        timestamp="2026-11-12T12:00:00+00:00",
        stream_active=True,
        reconnecting=False,
        bitrate_kbps=4200,
        dropped_frames=42,
        total_frames=1000,
        render_lag_ms=3.0,
        encoding_lag_ms=4.0,
        network_loss_percent=3.1,
    )
    diagnosis = diagnose_sample(sample)
    assert diagnosis.category == "network"
    assert "3.1%" in diagnosis.explanation


def test_diagnosis_marks_insufficient_evidence_when_stream_is_inactive():
    sample = MonitorSample("2026-11-12T12:00:00+00:00", False, False, None, None, None, None, None, None)
    assert diagnose_sample(sample).category == "unknown"


def test_diagnosis_attributes_lag_to_encoder():
    sample = MonitorSample("2026-11-12T12:00:00+00:00", True, False, 4000, 0, 1000, 35, 42, 0.0)
    assert diagnose_sample(sample).category == "encoder"


def test_bitrate_is_derived_from_output_byte_delta_not_cumulative_bytes():
    assert derive_bitrate_kbps(1_000_000, 1_250_000, 1.0) == 2000.0


def test_live_monitor_persists_samples_and_reconnects_after_disconnect(tmp_path):
    from stream_doctor.core import IncidentStore
    from stream_doctor.monitoring import LiveMonitor

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def connect(self):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("simulated disconnect")

        async def snapshot(self):
            return {"stats": {"averageFrameRenderTime": 1}, "stream": {"outputActive": True, "outputReconnecting": False, "outputSkippedFrames": 0, "outputTotalFrames": 100}, "output": {}}

        async def close(self):
            pass

    store = IncidentStore(tmp_path / "incidents.sqlite3")
    client = FakeClient()
    monitor = LiveMonitor(lambda: client, store, interval=0.001)
    asyncio.run(monitor.run(max_samples=1))
    assert monitor.samples_collected == 1


def test_live_monitor_does_not_fake_broadcast_lifecycle_at_monitor_boundaries(tmp_path):
    from stream_doctor.core import IncidentStore
    from stream_doctor.monitoring import LiveMonitor

    class Client:
        socket = object()
        def __init__(self):
            self.items = iter([
                {"stats": {"averageFrameRenderTime": 1}, "stream": {"outputActive": True, "outputReconnecting": True, "outputBytes": 1000, "outputSkippedFrames": 2, "outputTotalFrames": 100}, "output": {}},
                {"stats": {"averageFrameRenderTime": 1}, "stream": {"outputActive": True, "outputReconnecting": False, "outputBytes": 2000, "outputSkippedFrames": 2, "outputTotalFrames": 100}, "output": {}},
            ])
        async def connect(self): pass
        async def snapshot(self): return next(self.items)
        async def close(self): pass

    store = IncidentStore(tmp_path / "incidents.sqlite3")
    monitor = LiveMonitor(lambda: Client(), store, interval=0)
    import asyncio
    asyncio.run(monitor.run(max_samples=2))
    summaries = {item["summary"] for item in store.search()}
    assert "OBS reconnection started" in summaries
    assert "OBS reconnection recovered" in summaries
    assert "Broadcast started" not in summaries
    assert "Broadcast ended" not in summaries


def test_live_monitor_records_only_observed_stream_transitions(tmp_path):
    from stream_doctor.core import IncidentStore
    from stream_doctor.monitoring import LiveMonitor

    class Client:
        socket = object()
        def __init__(self):
            self.items = iter([
                {"stats": {}, "stream": {"outputActive": False}, "output": {}},
                {"stats": {}, "stream": {"outputActive": True}, "output": {}},
                {"stats": {}, "stream": {"outputActive": False}, "output": {}},
            ])
        async def snapshot(self): return next(self.items)
        async def close(self): pass

    store = IncidentStore(tmp_path / "incidents.sqlite3")
    asyncio.run(LiveMonitor(lambda: Client(), store, interval=0).run(max_samples=3))
    summaries = [item["summary"] for item in store.search()]
    assert "Broadcast started" in summaries
    assert "Broadcast ended" in summaries
