from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

from .core import sanitize_report


def report_format_for_path(destination: str | Path) -> str:
    return "html" if Path(destination).suffix.lower() == ".html" else "json"


def export_report(data: dict[str, Any], destination: str | Path, format: str = "json") -> Path:
    path = Path(destination)
    safe = sanitize_report(data)
    if format == "json":
        path.write_text(json.dumps(safe, indent=2), encoding="utf-8")
    elif format == "html":
        path.write_text("<html><body><h1>Stream Doctor report</h1><pre>" + escape(json.dumps(safe, indent=2)) + "</pre></body></html>", encoding="utf-8")
    else:
        raise ValueError("format must be json or html")
    return path
