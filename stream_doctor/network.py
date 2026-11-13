"""Explicit, bounded network measurements for preflight use."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import socket
import statistics
import time
import urllib.request


@dataclass(frozen=True)
class NetworkMeasurement:
    measured_at: str
    latency_ms: float | None
    jitter_ms: float | None
    packet_loss_percent: float | None
    upload_mbps: float | None
    successes: int
    attempts: int
    error: str | None = None
    bytes_transferred: int = 0
    samples: int = 0
    endpoint: str | None = None

    @property
    def fresh(self) -> bool:
        measured = datetime.fromisoformat(self.measured_at)
        return (datetime.now(timezone.utc) - measured).total_seconds() < 300


def measure_network_quality(host: str, port: int, *, samples: int = 5, timeout: float = 2.0) -> NetworkMeasurement:
    timings: list[float] = []
    attempts = max(1, samples)
    for _ in range(attempts):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                timings.append((time.perf_counter() - started) * 1000)
        except OSError:
            pass
    successes = len(timings)
    latency = round(statistics.mean(timings), 3) if timings else None
    jitter = round(statistics.pstdev(timings), 3) if len(timings) > 1 else 0.0 if timings else None
    loss = round((attempts - successes) / attempts * 100, 3)
    return NetworkMeasurement(datetime.now(timezone.utc).isoformat(), latency, jitter, loss, None, successes, attempts, None if successes else "No successful connections")


def measure_upload_capacity(host: str, port: int, *, duration_seconds: float = 3.0, payload_size: int = 64 * 1024, timeout: float = 2.0, stream_active: bool = False) -> NetworkMeasurement:
    if stream_active:
        raise RuntimeError("upload capacity measurement is disabled while an active stream is present")
    if duration_seconds <= 0 or payload_size <= 0:
        raise ValueError("duration_seconds and payload_size must be positive")
    payload = b"0" * payload_size
    sent = 0
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            deadline = started + duration_seconds
            while time.perf_counter() < deadline:
                sent += connection.send(payload)
    except OSError as exc:
        elapsed = max(time.perf_counter() - started, 0.001)
        return NetworkMeasurement(datetime.now(timezone.utc).isoformat(), None, None, None, round(sent * 8 / elapsed / 1_000_000, 3) if sent else None, 1 if sent else 0, 1, str(exc))
    elapsed = max(time.perf_counter() - started, 0.001)
    return NetworkMeasurement(datetime.now(timezone.utc).isoformat(), None, None, None, round(sent * 8 / elapsed / 1_000_000, 3), 1, 1)


def measure_cloudflare_upload(*, duration_seconds: float = 2.0, sample_bytes: int = 1_000_000, samples: int = 3, timeout: float = 5.0, stream_active: bool = False) -> NetworkMeasurement:
    """Measure Internet upload against Cloudflare's documented speed-test endpoint.

    Defaults are bounded to 3 MB total. Payloads exist only for the duration of
    each request and are never written to disk.
    """
    if stream_active:
        raise RuntimeError("upload capacity measurement is disabled while an active stream is present")
    if duration_seconds <= 0 or sample_bytes <= 0 or samples <= 0:
        raise ValueError("duration_seconds, sample_bytes, and samples must be positive")
    endpoint = "https://speed.cloudflare.com/__up"
    timings: list[float] = []
    transferred = 0
    for _ in range(samples):
        payload = os.urandom(sample_bytes)
        request = urllib.request.Request(endpoint, data=payload, method="POST", headers={"Content-Type": "application/octet-stream", "Content-Length": str(sample_bytes), "Origin": "https://speed.cloudflare.com", "Referer": "https://speed.cloudflare.com/", "User-Agent": "StreamDoctor/1.0"})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read(1)
            elapsed = max(time.perf_counter() - started, 0.001)
            timings.append(elapsed)
            transferred += sample_bytes
        except OSError as exc:
            return NetworkMeasurement(datetime.now(timezone.utc).isoformat(), None, None, None, None, len(timings), samples, str(exc), transferred, len(timings), endpoint)
        if sum(timings) >= duration_seconds:
            break
    total_time = max(sum(timings), 0.001)
    return NetworkMeasurement(datetime.now(timezone.utc).isoformat(), None, None, None, round(transferred * 8 / total_time / 1_000_000, 3) if transferred else None, len(timings), samples, None if transferred else "No upload samples completed", transferred, len(timings), endpoint)
