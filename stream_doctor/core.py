from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DiagnosticResult:
    status: str
    checks: list[dict[str, Any]]
    next_action: str

    @classmethod
    def from_checks(cls, checks: list[tuple[str, bool | None, str]]) -> "DiagnosticResult":
        rows = [{"name": n, "passed": p, "detail": d} for n, p, d in checks]
        if any(p is False for _, p, _ in checks):
            status, action = "NOT READY", next((d for _, p, d in checks if p is False), "Resolve failed checks.")
        elif any(p is None for _, p, _ in checks):
            status = "WARNING"
            action = next(("Measure upload capacity before broadcasting." if n == "Upload capacity" else d for n, p, d in checks if p is None), "Complete unavailable checks.")
        else:
            status, action = "READY", "No corrective action required."
        return cls(status, rows, action)


class RecommendationEngine:
    def recommend(self, *, obs: dict[str, Any], hardware: dict[str, Any], network: dict[str, Any], incidents: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        upload = network.get("upload_mbps")
        if not isinstance(upload, (int, float)) or upload <= 0:
            return []
        recommendations = []
        current = float(obs.get("bitrate_kbps") or 0)
        safe = int(upload * 1000 * 0.70) // 50 * 50
        if 0 < safe < current:
            recommendations.append({"setting": "bitrate_kbps", "current": current, "recommended": safe, "reason": "Measured upload capacity leaves a 30% stability margin."})
        if obs.get("fps") == 60 and upload < 6:
            recommendations.append({"setting": "fps", "current": 60, "recommended": 30, "reason": "Measured upload capacity is below the conservative 60 FPS threshold."})
        if hardware.get("encoder") and str(hardware["encoder"]).lower() not in str(obs.get("encoder", "")).lower():
            recommendations.append({"setting": "encoder", "current": obs.get("encoder"), "recommended": hardware["encoder"], "reason": "A detected hardware encoder is available."})
        return recommendations


class IncidentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, category TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(incidents)")}
            for name, definition in (("end_timestamp", "TEXT"), ("duration_seconds", "REAL"), ("recovery_status", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE incidents ADD COLUMN {name} {definition}")

    def record(self, category: str, summary: str, evidence: dict[str, Any]) -> int:
        timestamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as db:
            cur = db.execute("INSERT INTO incidents(timestamp, category, summary, evidence) VALUES (?, ?, ?, ?)", (timestamp, category, summary, json.dumps(evidence)))
            return int(cur.lastrowid)

    def start_incident(self, category: str, summary: str, evidence: dict[str, Any], timestamp: str | None = None) -> int:
        timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as db:
            cur = db.execute("INSERT INTO incidents(timestamp, category, summary, evidence) VALUES (?, ?, ?, ?)", (timestamp, category, summary, json.dumps(evidence)))
            return int(cur.lastrowid)

    def close_incident(self, incident_id: int, end_timestamp: str, duration_seconds: float, recovery_status: str, evidence: dict[str, Any] | None = None) -> None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT evidence FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            merged = json.loads(row[0]) if row else {}
            if evidence:
                merged.update(evidence)
            db.execute("UPDATE incidents SET end_timestamp = ?, duration_seconds = ?, recovery_status = ?, evidence = ? WHERE id = ?", (end_timestamp, duration_seconds, recovery_status, json.dumps(merged), incident_id))

    def clear(self) -> int:
        with sqlite3.connect(self.path) as db:
            return db.execute("DELETE FROM incidents").rowcount

    def search(self, query: str = "") -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT id, timestamp, category, summary, evidence, end_timestamp, duration_seconds, recovery_status FROM incidents WHERE summary LIKE ? OR category LIKE ? ORDER BY id DESC", (f"%{query}%", f"%{query}%")).fetchall()
        return [{"id": r[0], "timestamp": r[1], "category": r[2], "summary": r[3], "evidence": json.loads(r[4]), "end_timestamp": r[5], "duration_seconds": r[6], "recovery_status": r[7]} for r in rows]


_SENSITIVE_KEYS = {"key", "streamkey", "stream_key", "password", "credential", "token", "room", "roomid", "room_id"}


def sanitize_report(value: Any, key: str = "") -> Any:
    if key.lower().replace("-", "_") in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: sanitize_report(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_report(v, key) for v in value]
    return value
