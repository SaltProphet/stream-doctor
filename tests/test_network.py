import socket
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from stream_doctor.network import measure_cloudflare_upload, measure_upload_capacity, measure_network_quality


def test_upload_probe_refuses_to_run_while_stream_is_active():
    with pytest.raises(RuntimeError, match="active stream"):
        measure_upload_capacity("127.0.0.1", 9, duration_seconds=0.1, stream_active=True)


def test_network_quality_reports_latency_jitter_and_loss_for_local_endpoint():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def accept_connections():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                conn.close()
            except (socket.timeout, OSError):
                pass

    thread = threading.Thread(target=accept_connections, daemon=True)
    thread.start()
    try:
        result = measure_network_quality("127.0.0.1", listener.getsockname()[1], samples=3)
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=1)
    assert result.successes == 3
    assert result.packet_loss_percent == 0.0
    assert result.latency_ms is not None
    assert result.jitter_ms is not None


def test_cloudflare_upload_uses_bounded_post_samples(monkeypatch):
    calls = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size=-1): return b""

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, int(request.headers["Content-length"]), timeout))
        return Response()

    monkeypatch.setattr("stream_doctor.network.urllib.request.urlopen", fake_urlopen)
    result = measure_cloudflare_upload(duration_seconds=0.01, sample_bytes=1024, samples=2)
    assert result.samples == 2
    assert result.bytes_transferred == 2048
    assert result.endpoint == "https://speed.cloudflare.com/__up"
    assert len(calls) == 2


def test_cloudflare_upload_refuses_active_stream():
    with pytest.raises(RuntimeError, match="active stream"):
        measure_cloudflare_upload(stream_active=True)


def test_network_measurement_exposes_freshness():
    result = measure_network_quality("127.0.0.1", 9, samples=1, timeout=0.01)
    assert result.fresh is True
    stale = replace(result, measured_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    assert stale.fresh is False
