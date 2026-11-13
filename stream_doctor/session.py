from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ConnectionSettings:
    host: str = "127.0.0.1"
    port: int = 4455
    label: str = "OBS instance"
    password: str | None = None

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({"host": self.host, "port": self.port, "label": self.label}, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ConnectionSettings":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(str(data.get("host", "127.0.0.1")), int(data.get("port", 4455)), str(data.get("label", "OBS instance")))


class MonitoringSession:
    def __init__(self, store, instance: str):
        self.store = store
        self.instance = instance
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())

    def start(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self.store.record("session", "Monitoring session started", {"instance": self.instance, "started_at": self.started_at.isoformat()})

    def finish(self) -> None:
        if self.started_at is None or self.finished_at is not None:
            return
        self.finished_at = datetime.now(timezone.utc)
        self.store.record("session", "Monitoring session finished", {"instance": self.instance, "started_at": self.started_at.isoformat(), "finished_at": self.finished_at.isoformat(), "duration_seconds": self.duration_seconds})
