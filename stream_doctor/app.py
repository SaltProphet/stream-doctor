"""Stream Doctor — redesigned UI (merged).

Drop-in replacement for stream_doctor/app.py.
Contains the original MonitorWorker (unchanged) plus the new sidebar UI
and floating Live Monitor. No circular import.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal, QTimer
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSlider, QSpinBox, QStackedWidget,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .core import IncidentStore, RecommendationEngine
from .monitoring import LiveMonitor
from .measurements import build_optimizer_inputs
from .network import measure_cloudflare_upload, measure_network_quality, measure_upload_capacity
from .obs import ObsClient, ObsConnectionError
from .optimizer import apply_supported_output_settings, rollback_output_settings
from .reports import export_report, report_format_for_path
from .session import ConnectionSettings, MonitoringSession
from .telemetry import hardware_snapshot, local_latency


# ---------------------------------------------------------------------------
# Original MonitorWorker (preserved exactly so existing tests and smoke probes
# that import it continue to work)
# ---------------------------------------------------------------------------
class MonitorWorker(QThread):
    sample_received = Signal(dict)
    state_changed = Signal(str)

    def __init__(self, settings: ConnectionSettings, store: IncidentStore):
        super().__init__()
        self.settings = settings
        self.store = store
        self.monitor: LiveMonitor | None = None
        self.session: MonitoringSession | None = None
        self.report_records: list[dict] = []
        self.hardware_measurements = hardware_snapshot()
        self.network_measurements = {"latency_ms": local_latency(), "jitter_ms": None, "packet_loss_percent": None, "upload_mbps": None}

    def run(self) -> None:
        async def work() -> None:
            client = ObsClient(self.settings.url, self.settings.password)
            try:
                self.state_changed.emit(f"Connecting to {self.settings.url} …")
                await client.connect()
                version = await client.request("GetVersion")
                try:
                    self.hardware_measurements["encoder_capabilities"] = (await client.discover_encoder_capabilities()).get("supported_encoders", [])
                except Exception:
                    self.hardware_measurements["encoder_capabilities"] = None
                instance = f"OBS {version.get('obsVersion', 'unknown')} / WebSocket {version.get('obsWebSocketVersion', 'unknown')} @ {self.settings.host}:{self.settings.port}"
                self.session = MonitoringSession(self.store, instance)
                self.session.start()
                self.state_changed.emit(f"Connected: {instance}")
                self.monitor = LiveMonitor(lambda: client, self.store, on_sample=self._on_sample)
                await self.monitor.run()
            except Exception as exc:
                self.state_changed.emit(f"Connection/monitoring error: {exc}")
            finally:
                if self.session:
                    self.session.finish()
                await client.close()

        asyncio.run(work())

    def _on_sample(self, record: dict) -> None:
        self.report_records.append(record)
        self.sample_received.emit(record)

    def stop_monitoring(self) -> None:
        if self.monitor:
            self.monitor.stop()


# ---------------------------------------------------------------------------
# UI components
# ---------------------------------------------------------------------------
class MiniChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values: list[float] = []
        self.setMinimumHeight(60)
        self.setObjectName("chartContainer")

    def push(self, value: float | None) -> None:
        if isinstance(value, (int, float)):
            self.values.append(float(value))
            self.values = self.values[-60:]
        self.update()

    def paintEvent(self, event):  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111318"))
        if len(self.values) < 2:
            painter.setPen(QColor("#8b9085"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Waiting for samples")
            return
        painter.setPen(QPen(QColor("#c2410c"), 1.5))
        w, h = self.width(), self.height()
        mn, mx = min(self.values), max(self.values)
        span = mx - mn or 1.0
        pts = []
        for i, v in enumerate(self.values):
            x = int(i / (len(self.values) - 1) * (w - 4)) + 2
            y = int(h - 4 - ((v - mn) / span) * (h - 8))
            pts.append((x, y))
        for a, b in zip(pts, pts[1:]):
            painter.drawLine(a[0], a[1], b[0], b[1])


class FloatingMonitor(QWidget):
    """Separate always-on-top window. Shares samples; no second OBS connection."""
    open_dashboard = Signal()
    open_incident = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setObjectName("overlayWindow")
        self.mode = "compact"
        self.emergency = False
        self._drag_pos = None
        self._build()
        self.resize(320, 180)
        self._load_geometry()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        self.title = QLabel("Stream Doctor")
        self.title.setStyleSheet("font-weight: 600; color: #c8ccc0;")
        root.addWidget(self.title)

        self.status_row = QLabel("OBS: —  •  Stream: —")
        root.addWidget(self.status_row)

        self.bitrate_label = QLabel("Bitrate: —")
        self.dropped_label = QLabel("Dropped: —")
        self.render_label = QLabel("Render: —")
        for w in (self.bitrate_label, self.dropped_label, self.render_label):
            root.addWidget(w)

        self.chart = MiniChart()
        root.addWidget(self.chart)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #c2410c;")
        root.addWidget(self.warning)

        controls = QHBoxLayout()
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(30, 100)
        self.opacity.setValue(90)
        self.opacity.valueChanged.connect(lambda v: self.setWindowOpacity(v / 100.0))
        controls.addWidget(QLabel("Opacity"))
        controls.addWidget(self.opacity)
        root.addLayout(controls)

        actions = QHBoxLayout()
        dash = QPushButton("Open Dashboard")
        dash.clicked.connect(self.open_dashboard.emit)
        inc = QPushButton("View Incident")
        inc.clicked.connect(self.open_incident.emit)
        actions.addWidget(dash)
        actions.addWidget(inc)
        root.addLayout(actions)

        self._apply_mode()

    def _apply_mode(self) -> None:
        expanded = self.mode == "expanded"
        compact = self.mode == "compact"
        self.chart.setVisible(expanded)
        self.render_label.setVisible(expanded or compact)
        self.warning.setVisible(expanded or self.emergency)
        if self.mode == "collapsed":
            self.resize(280, 48)
        elif compact:
            self.resize(320, 160)
        else:
            self.resize(420, 280)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._apply_mode()
        self._save_geometry()

    def set_emergency(self, active: bool, message: str = "") -> None:
        self.emergency = active
        self.setObjectName("overlayEmergency" if active else "overlayWindow")
        self.style().unpolish(self)
        self.style().polish(self)
        if active:
            self.warning.setText(message or "Emergency condition detected")
            self.warning.setStyleSheet("color: #b91c1c; font-weight: 600;")
        else:
            self.warning.setStyleSheet("color: #c2410c;")
        self._apply_mode()

    def update_sample(self, record: dict) -> None:
        sample = record.get("sample", {})
        diagnosis = record.get("diagnosis", {})
        active = bool(sample.get("stream_active"))
        bitrate = sample.get("bitrate_kbps")
        dropped = sample.get("dropped_frames")
        total = sample.get("total_frames")
        render = sample.get("render_lag_ms")
        video = record.get("video") or {}
        black = video.get("black_ratio") if isinstance(video, dict) else None
        if black is None and isinstance(video, dict):
            black = video.get("is_black")

        self.status_row.setText(
            f"OBS: Connected  •  Stream: {'Streaming' if active else 'Not streaming'}"
        )
        self.bitrate_label.setText(
            f"Bitrate: {bitrate:.0f} kbps" if isinstance(bitrate, (int, float)) else "Bitrate: Unavailable"
        )
        if isinstance(dropped, int) and isinstance(total, int) and total > 0:
            pct = dropped / total * 100
            self.dropped_label.setText(f"Dropped: {pct:.2f}% ({dropped}/{total})")
        else:
            self.dropped_label.setText("Dropped: Unavailable")
        self.render_label.setText(
            f"Render lag: {render:.1f} ms" if isinstance(render, (int, float)) else "Render: Unavailable"
        )
        self.chart.push(bitrate)
        if diagnosis.get("category") not in (None, "healthy"):
            self.warning.setText(diagnosis.get("explanation", ""))
        else:
            self.warning.setText("")

        # Emergency: OBS disconnect, bitrate collapse, or sustained black (when wired)
        emergency = False
        msg = ""
        if record.get("_obs_connected") is False:
            emergency, msg = True, "OBS disconnected"
        elif active and isinstance(bitrate, (int, float)) and bitrate < 50:
            emergency, msg = True, "Bitrate collapsed"
        elif black is True or (isinstance(black, (int, float)) and black >= 0.98):
            emergency, msg = True, "Sustained black video detected"
        self.set_emergency(emergency, msg)

    def mousePressEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):  # type: ignore[no-untyped-def]
        if self._drag_pos and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):  # type: ignore[no-untyped-def]
        self._drag_pos = None
        self._save_geometry()

    def _settings_path(self) -> Path:
        return Path.home() / "AppData" / "Local" / "StreamDoctor" / "overlay.json"

    def _save_geometry(self) -> None:
        data = {
            "x": self.x(), "y": self.y(),
            "w": self.width(), "h": self.height(),
            "mode": self.mode,
            "opacity": self.opacity.value(),
        }
        try:
            self._settings_path().parent.mkdir(parents=True, exist_ok=True)
            self._settings_path().write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def _load_geometry(self) -> None:
        try:
            data = json.loads(self._settings_path().read_text(encoding="utf-8"))
            self.move(data.get("x", 100), data.get("y", 100))
            self.resize(data.get("w", 320), data.get("h", 180))
            self.mode = data.get("mode", "compact")
            self.opacity.setValue(data.get("opacity", 90))
            self._apply_mode()
        except (OSError, json.JSONDecodeError, KeyError):
            pass


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Stream Doctor")
        self.resize(1100, 720)
        data_dir = Path.home() / "AppData" / "Local" / "StreamDoctor"
        self.store = IncidentStore(data_dir / "incidents.sqlite3")
        self.settings_path = data_dir / "connection.json"
        try:
            self.settings = ConnectionSettings.load(self.settings_path)
        except (FileNotFoundError, ValueError, TypeError):
            self.settings = ConnectionSettings()
        self.worker: MonitorWorker | None = None
        self.last_record: dict = {}
        self.engine = RecommendationEngine()
        self._hardware_measurements = hardware_snapshot()
        self._network_measurements = {"latency_ms": local_latency(), "jitter_ms": None, "packet_loss_percent": None, "upload_mbps": None}
        self._obs_config: dict = {}
        self._optimizer_recommendations: list[dict] = []
        self._optimizer_backup: dict | None = None

        self.overlay = FloatingMonitor()
        self.overlay.open_dashboard.connect(self._show_from_overlay)
        self.overlay.open_incident.connect(lambda: self._show_screen("Incidents"))

        self._build_ui()
        self._build_tray()
        self._apply_theme()

        smoke_file = os.environ.get("STREAM_DOCTOR_SMOKE_FILE")
        if smoke_file:
            QTimer.singleShot(1000, lambda: self._write_smoke_probe(smoke_file))

    def _apply_theme(self) -> None:
        qss = Path(__file__).with_name("theme.qss")
        if qss.exists():
            self.setStyleSheet(qss.read_text(encoding="utf-8"))

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(180)
        side_layout = QVBoxLayout(sidebar)
        self.nav = QListWidget()
        self.nav.setObjectName("sidebarList")
        for name in ("Dashboard", "Preflight", "Diagnostics", "Optimizer", "Incidents", "Settings"):
            self.nav.addItem(QListWidgetItem(name))
        self.nav.currentRowChanged.connect(self._switch)
        side_layout.addWidget(self.nav)
        side_layout.addStretch()
        root.addWidget(sidebar)

        self.stack = QStackedWidget()
        self.pages = {
            "Dashboard": self._dashboard_page(),
            "Preflight": self._preflight_page(),
            "Diagnostics": self._diagnostics_page(),
            "Optimizer": self._optimizer_page(),
            "Incidents": self._incidents_page(),
            "Settings": self._settings_page(),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.setCurrentRow(0)

    def _switch(self, row: int) -> None:
        self.stack.setCurrentIndex(row)

    def _show_screen(self, name: str) -> None:
        idx = list(self.pages.keys()).index(name)
        self.nav.setCurrentRow(idx)
        self.showNormal()
        self.raise_()

    def _dashboard_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        strip = QFrame()
        strip.setObjectName("statusStrip")
        strip_l = QHBoxLayout(strip)
        self.obs_status = QLabel("OBS: Unknown")
        self.stream_status = QLabel("Stream: Unknown")
        self.health_status = QLabel("Status: Unknown")
        for w in (self.obs_status, self.stream_status, self.health_status):
            strip_l.addWidget(w)
        strip_l.addStretch()
        layout.addWidget(strip)

        charts = QHBoxLayout()
        self.bitrate_chart = MiniChart()
        self.dropped_chart = MiniChart()
        self.render_chart = MiniChart()
        for title, chart in (("Bitrate (kbps)", self.bitrate_chart),
                             ("Dropped frames %", self.dropped_chart),
                             ("Render lag (ms)", self.render_chart)):
            box = QVBoxLayout()
            box.addWidget(QLabel(title))
            box.addWidget(chart)
            charts.addLayout(box)
        layout.addLayout(charts)

        metrics = QFormLayout()
        self.m_bitrate = QLabel("Unavailable")
        self.m_dropped = QLabel("Unavailable")
        self.m_render = QLabel("Unavailable")
        self.m_network = QLabel("Unavailable")
        metrics.addRow("Observed bitrate", self.m_bitrate)
        metrics.addRow("Dropped frames", self.m_dropped)
        metrics.addRow("Render lag", self.m_render)
        metrics.addRow("Network loss", self.m_network)
        layout.addLayout(metrics)
        layout.addStretch()
        return page

    def _preflight_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("<b>Preflight</b> — measured checks only"))
        self.preflight_table = QTableWidget(0, 3)
        self.preflight_table.setHorizontalHeaderLabels(["Check", "Result", "Detail"])
        layout.addWidget(self.preflight_table)
        run = QPushButton("Run checks")
        run.setObjectName("primary")
        run.clicked.connect(self._run_preflight)
        layout.addWidget(run)
        self.preflight_summary = QLabel("Not run")
        layout.addWidget(self.preflight_summary)
        target = QFormLayout()
        self.upload_host_edit = QLineEdit()
        self.upload_host_edit.setPlaceholderText("Explicit test endpoint; never run during a live stream")
        self.upload_port_edit = QSpinBox()
        self.upload_port_edit.setRange(1, 65535)
        self.upload_port_edit.setValue(443)
        target.addRow("Network test host", self.upload_host_edit)
        target.addRow("Network test port", self.upload_port_edit)
        layout.addLayout(target)
        network_button = QPushButton("Measure upload capacity")
        network_button.clicked.connect(self._measure_upload_capacity)
        layout.addWidget(network_button)
        internet_button = QPushButton("Measure Internet upload (Cloudflare)")
        internet_button.clicked.connect(self._measure_cloudflare_upload)
        layout.addWidget(internet_button)
        return page

    def _run_preflight(self) -> None:
        rows = []
        quality = measure_network_quality("8.8.8.8", 443, samples=3)
        lat = quality.latency_ms
        self._network_measurements.update({"latency_ms": lat, "jitter_ms": quality.jitter_ms, "packet_loss_percent": quality.packet_loss_percent, "measured_at": quality.measured_at})
        rows.append(("Internet latency", "Healthy" if lat and lat < 100 else "Unstable" if lat else "Unavailable",
                      f"{lat:.0f} ms" if lat else "Unavailable"))
        hw = hardware_snapshot()
        rows.append(("Hardware snapshot", "Healthy", f"{hw.get('logical_cpus')} CPUs, {hw.get('ram_gb')} GB RAM"))
        gpus = hw.get("gpus") or []
        rows.append(("GPU discovery", "Healthy" if gpus else "Unavailable", ", ".join(gpus) if gpus else "Unavailable"))
        rows.append(("Hardware encoder capability", "Unavailable", "OBS encoder compatibility requires an encoder-specific probe"))
        rows.append(("Network jitter", "Healthy" if quality.jitter_ms is not None else "Unavailable", f"{quality.jitter_ms:.3f} ms" if quality.jitter_ms is not None else "Unavailable"))
        rows.append(("Packet loss", "Healthy" if quality.packet_loss_percent is not None and quality.packet_loss_percent == 0 else "Unstable" if quality.packet_loss_percent is not None else "Unavailable", f"{quality.packet_loss_percent:.1f}%" if quality.packet_loss_percent is not None else "Unavailable"))
        rows.append(("Sustained upload capacity", "Unavailable", "Required before bitrate recommendations"))
        rows.append(("OBS WebSocket", "Unknown", "Use Settings → Test connection"))
        self.preflight_table.setRowCount(len(rows))
        for i, (name, status, detail) in enumerate(rows):
            self.preflight_table.setItem(i, 0, QTableWidgetItem(name))
            self.preflight_table.setItem(i, 1, QTableWidgetItem(status))
            self.preflight_table.setItem(i, 2, QTableWidgetItem(detail))
        bad = any(s in ("Unstable", "Critical", "Unavailable") for _, s, _ in rows)
        self.preflight_summary.setText("Issues found — review before streaming" if bad else "Ready (limited checks)")

    def _measure_upload_capacity(self) -> None:
        if self.last_record.get("sample", {}).get("stream_active"):
            self.preflight_summary.setText("Unavailable: upload measurement is disabled while streaming")
            return
        host = self.upload_host_edit.text().strip()
        if not host:
            self.preflight_summary.setText("Unavailable: enter an explicit upload test endpoint")
            return
        port = self.upload_port_edit.value()
        quality = measure_network_quality(host, port, samples=5)
        try:
            upload = measure_upload_capacity(host, port, stream_active=False)
        except (OSError, RuntimeError, ValueError) as exc:
            self.preflight_summary.setText(f"Upload measurement failed: {exc}")
            return
        self._network_measurements.update({"latency_ms": quality.latency_ms, "jitter_ms": quality.jitter_ms, "packet_loss_percent": quality.packet_loss_percent, "upload_mbps": upload.upload_mbps, "measured_at": upload.measured_at})
        self.preflight_summary.setText(f"Upload measured: {upload.upload_mbps:.2f} Mbps" if upload.upload_mbps is not None else "Upload measurement unavailable")

    def _measure_cloudflare_upload(self) -> None:
        if self.last_record.get("sample", {}).get("stream_active"):
            self.preflight_summary.setText("Unavailable: upload measurement is disabled while streaming")
            return
        try:
            result = measure_cloudflare_upload(stream_active=False)
        except (OSError, RuntimeError, ValueError) as exc:
            self.preflight_summary.setText(f"Internet upload measurement failed: {exc}")
            return
        self._network_measurements.update({"upload_mbps": result.upload_mbps, "measured_at": result.measured_at, "upload_bytes": result.bytes_transferred, "upload_samples": result.samples, "upload_endpoint": result.endpoint})
        if result.upload_mbps is None:
            self.preflight_summary.setText(f"Internet upload unavailable: {result.error or 'no samples completed'}")
        else:
            self.preflight_summary.setText(f"Internet upload: {result.upload_mbps:.2f} Mbps ({result.bytes_transferred} bytes, {result.samples} samples)")

    def _diagnostics_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("<b>Diagnostics</b> — evidence from the latest sample"))
        self.diag_text = QLabel("Start monitoring to collect evidence.")
        self.diag_text.setWordWrap(True)
        layout.addWidget(self.diag_text)
        layout.addStretch()
        return page

    def _optimizer_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("<b>Optimizer</b> — recommendations only from measured conditions"))
        self.opt_table = QTableWidget(0, 4)
        self.opt_table.setHorizontalHeaderLabels(["Setting", "Current", "Recommended", "Reason"])
        layout.addWidget(self.opt_table)
        self.opt_status = QLabel("Waiting for measured OBS and network data")
        self.opt_status.setWordWrap(True)
        layout.addWidget(self.opt_status)
        note = QLabel("No automatic changes. Backup and rollback are required before any write.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.opt_apply = QPushButton("Apply selected recommendation")
        self.opt_apply.setEnabled(False)
        self.opt_apply.clicked.connect(self._apply_optimizer_recommendation)
        layout.addWidget(self.opt_apply)
        self.opt_rollback = QPushButton("Rollback last change")
        self.opt_rollback.setEnabled(False)
        self.opt_rollback.clicked.connect(self._rollback_optimizer_change)
        layout.addWidget(self.opt_rollback)
        layout.addStretch()
        return page

    def _incidents_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("<b>Incidents</b> — local SQLite history (session events are distinct from broadcast events)"))
        self.inc_table = QTableWidget(0, 5)
        self.inc_table.setHorizontalHeaderLabels(["Time", "Category", "Summary", "Duration", "Evidence"])
        layout.addWidget(self.inc_table)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_incidents)
        layout.addWidget(refresh)
        export = QPushButton("Export report")
        export.clicked.connect(self._export_report)
        layout.addWidget(export)
        self._refresh_incidents()
        return page

    def _refresh_incidents(self) -> None:
        rows = self.store.search()
        self.inc_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.inc_table.setItem(i, 0, QTableWidgetItem(row.get("timestamp", "")[:19]))
            self.inc_table.setItem(i, 1, QTableWidgetItem(row.get("category", "")))
            self.inc_table.setItem(i, 2, QTableWidgetItem(row.get("summary", "")))
            ev = row.get("evidence", {})
            dur = row.get("duration_seconds")
            self.inc_table.setItem(i, 3, QTableWidgetItem(f"{dur:.0f}s" if isinstance(dur, (int, float)) else "—"))
            self.inc_table.setItem(i, 4, QTableWidgetItem(str(ev)[:80]))

    def _export_report(self) -> None:
        report = {
            "latest_sample": self.last_record,
            "incidents": self.store.search(),
        }
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Export diagnostic report",
            "stream-doctor-report.json",
            "JSON report (*.json);;HTML report (*.html)",
        )
        if not destination:
            return
        path = Path(destination)
        if path.suffix.lower() not in {".json", ".html"}:
            path = path.with_suffix(".json")
        try:
            export_report(report, path, report_format_for_path(path))
        except OSError as exc:
            QMessageBox.critical(self, "Report export failed", str(exc))
            return
        QMessageBox.information(self, "Report exported", f"The sanitized report was saved to:\n{path}")

    def _apply_optimizer_recommendation(self) -> None:
        if not self._optimizer_recommendations:
            return
        if self.last_record.get("sample", {}).get("stream_active"):
            QMessageBox.warning(self, "Apply blocked", "OBS reports an active stream. Stop the stream manually before applying a setting.")
            return
        output_name = self._obs_config.get("outputName")
        if not output_name:
            QMessageBox.warning(self, "Apply unavailable", "The active OBS output name is unavailable.")
            return
        row = self.opt_table.currentRow()
        recommendation = self._optimizer_recommendations[row if row >= 0 else 0]
        setting = {"bitrate_kbps": "bitrate"}.get(recommendation.get("setting"), recommendation.get("setting"))
        if setting != "bitrate":
            QMessageBox.warning(self, "Apply unavailable", "Only verified OBS bitrate changes are currently supported by the UI.")
            return
        if QMessageBox.question(self, "Apply recommendation", "Apply this measured recommendation to OBS?") != QMessageBox.StandardButton.Yes:
            return
        settings = self._current_settings()

        async def apply():
            client = ObsClient(settings.url, settings.password)
            try:
                await client.connect()
                return await apply_supported_output_settings(client, output_name, {setting: int(recommendation["recommended"])})
            finally:
                await client.close()

        try:
            result = asyncio.run(apply())
        except Exception as exc:
            QMessageBox.critical(self, "Apply failed", str(exc))
            return
        if result.get("verified"):
            self._optimizer_backup = {"output_name": output_name, "settings": result["backup"]}
            self.opt_rollback.setEnabled(True)
            self.opt_status.setText("Recommendation applied and verified by OBS read-back.")
        else:
            self.opt_status.setText("OBS read-back did not verify the recommendation; rollback was attempted.")

    def _rollback_optimizer_change(self) -> None:
        if not self._optimizer_backup:
            return
        backup = self._optimizer_backup
        settings = self._current_settings()

        async def rollback():
            client = ObsClient(settings.url, settings.password)
            try:
                await client.connect()
                await rollback_output_settings(client, backup["output_name"], backup["settings"])
            finally:
                await client.close()

        try:
            asyncio.run(rollback())
        except Exception as exc:
            QMessageBox.critical(self, "Rollback failed", str(exc))
            return
        self._optimizer_backup = None
        self.opt_rollback.setEnabled(False)
        self.opt_status.setText("Previous OBS output setting restored.")

    def _settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.host_edit = QLineEdit(self.settings.host)
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(self.settings.port)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("Not saved to disk")
        form.addRow("OBS host", self.host_edit)
        form.addRow("Port", self.port_edit)
        form.addRow("WebSocket password", self.password_edit)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        save = QPushButton("Save endpoint")
        save.clicked.connect(self._save_settings)
        test = QPushButton("Test connection")
        test.clicked.connect(self._test_connection)
        buttons.addWidget(save)
        buttons.addWidget(test)
        layout.addLayout(buttons)

        self.connection_status = QLabel("Not tested")
        layout.addWidget(self.connection_status)

        self.overlay_enabled = QCheckBox("Show floating Live Monitor")
        self.overlay_enabled.setChecked(True)
        self.overlay_enabled.toggled.connect(self._toggle_overlay)
        layout.addWidget(self.overlay_enabled)

        mode_box = QHBoxLayout()
        for m in ("collapsed", "compact", "expanded"):
            b = QPushButton(m.capitalize())
            b.clicked.connect(lambda _, mode=m: self.overlay.set_mode(mode))
            mode_box.addWidget(b)
        layout.addLayout(mode_box)

        start = QPushButton("Start monitoring")
        start.setObjectName("primary")
        start.clicked.connect(self._start_monitoring)
        stop = QPushButton("Stop monitoring")
        stop.clicked.connect(self._stop_monitoring)
        layout.addWidget(start)
        layout.addWidget(stop)
        layout.addStretch()
        return page

    def _current_settings(self) -> ConnectionSettings:
        return ConnectionSettings(
            self.host_edit.text().strip() or "127.0.0.1",
            self.port_edit.value(),
            "OBS instance",
            self.password_edit.text() or None,
        )

    def _save_settings(self) -> None:
        self.settings = self._current_settings()
        self.settings.save(self.settings_path)
        self.connection_status.setText(f"Saved {self.settings.url} (password not written)")

    def _test_connection(self) -> None:
        settings = self._current_settings()
        async def probe():
            client = ObsClient(settings.url, settings.password)
            try:
                await client.connect()
                return await client.request("GetVersion")
            finally:
                await client.close()
        try:
            version = asyncio.run(probe())
            self.connection_status.setText(
                f"Connected: OBS {version.get('obsVersion')} / WS {version.get('obsWebSocketVersion')}"
            )
        except ObsConnectionError as exc:
            self.connection_status.setText(f"Unavailable: {exc}")

    def _start_monitoring(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.settings = self._current_settings()
        self.worker = MonitorWorker(self.settings, self.store)
        self.worker.state_changed.connect(self._on_state)
        self.worker.sample_received.connect(self._on_sample)
        self.worker.start()
        if self.overlay_enabled.isChecked():
            self.overlay.show()

    def _stop_monitoring(self) -> None:
        if self.worker:
            self.worker.stop_monitoring()
            self.worker.wait(15000)
        self.obs_status.setText("OBS: Unknown")
        self.stream_status.setText("Stream: Not streaming")

    def _on_state(self, text: str) -> None:
        self.connection_status.setText(text)
        if "Connected" in text:
            self.obs_status.setText("OBS: Connected")
            self.obs_status.setObjectName("statusHealthy")
        elif "error" in text.lower() or "Unavailable" in text:
            self.obs_status.setText("OBS: Unavailable")
            self.obs_status.setObjectName("statusCritical")

    def _on_sample(self, record: dict) -> None:
        self.last_record = record
        if self.worker:
            self._hardware_measurements = self.worker.hardware_measurements
            self._network_measurements = self.worker.network_measurements
        sample = record.get("sample", {})
        diagnosis = record.get("diagnosis", {})
        active = bool(sample.get("stream_active"))
        self.stream_status.setText("Stream: Streaming" if active else "Stream: Not streaming")
        cat = diagnosis.get("category", "unknown")
        if cat == "healthy":
            self.health_status.setText("Status: Healthy")
            self.health_status.setObjectName("statusHealthy")
        elif cat in ("encoder", "rendering", "network"):
            self.health_status.setText(f"Status: Unstable ({cat})")
            self.health_status.setObjectName("statusUnstable")
        else:
            self.health_status.setText("Status: Unknown")
            self.health_status.setObjectName("statusUnknown")

        bitrate = sample.get("bitrate_kbps")
        self.m_bitrate.setText(f"{bitrate:.1f} kbps" if isinstance(bitrate, (int, float)) else "Unavailable")
        self.bitrate_chart.push(bitrate)

        dropped = sample.get("dropped_frames")
        total = sample.get("total_frames")
        if isinstance(dropped, int) and isinstance(total, int) and total > 0:
            pct = dropped / total * 100
            self.m_dropped.setText(f"{pct:.2f}% ({dropped}/{total})")
            self.dropped_chart.push(pct)
        else:
            self.m_dropped.setText("Unavailable")

        render = sample.get("render_lag_ms")
        self.m_render.setText(f"{render:.1f} ms" if isinstance(render, (int, float)) else "Unavailable")
        self.render_chart.push(render)

        loss = sample.get("network_loss_percent")
        self.m_network.setText(f"{loss:.1f}%" if isinstance(loss, (int, float)) else "Unavailable")

        self.diag_text.setText(
            f"Category: {cat}\nExplanation: {diagnosis.get('explanation', '—')}\n"
            f"Next action: {diagnosis.get('next_action', '—')}\n"
            f"Evidence: bitrate={bitrate}, dropped={dropped}/{total}, render_lag={render}"
        )

        self._obs_config = record.get("obs_config") or self._obs_config
        measurements = build_optimizer_inputs(
            sample=sample,
            obs_config=self._obs_config,
            hardware=self._hardware_measurements,
            network=self._network_measurements,
            incidents=self.store.search("blackout"),
        )
        recs = self.engine.recommend(**measurements)
        self._optimizer_recommendations = recs
        self.opt_apply.setEnabled(bool(recs) and bool(self._obs_config.get("outputName")) and not active)
        self.opt_table.setRowCount(len(recs))
        for i, r in enumerate(recs):
            self.opt_table.setItem(i, 0, QTableWidgetItem(str(r.get("setting"))))
            self.opt_table.setItem(i, 1, QTableWidgetItem(str(r.get("current"))))
            self.opt_table.setItem(i, 2, QTableWidgetItem(str(r.get("recommended"))))
            self.opt_table.setItem(i, 3, QTableWidgetItem(str(r.get("reason"))))
        if measurements["network"].get("upload_mbps") is None:
            self.opt_status.setText("Unavailable: sustained upload capacity is required before bitrate recommendations can be generated.")
        elif recs:
            self.opt_status.setText(f"{len(recs)} measured recommendation(s) available; no changes are applied automatically.")
        else:
            self.opt_status.setText("No changes recommended from the current measured conditions.")

        self.overlay.update_sample(record)
        self._refresh_incidents()

    def _toggle_overlay(self, on: bool) -> None:
        self.overlay.setVisible(on)

    def _show_from_overlay(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("Stream Doctor")
        menu = QMenu()
        show = QAction("Open Stream Doctor", self)
        show.triggered.connect(self.showNormal)
        menu.addAction(show)
        overlay = QAction("Show / hide Live Monitor", self)
        overlay.triggered.connect(lambda: self.overlay.setVisible(not self.overlay.isVisible()))
        menu.addAction(overlay)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _quit(self) -> None:
        self._stop_monitoring()
        QApplication.quit()

    def closeEvent(self, event):  # type: ignore[no-untyped-def]
        event.ignore()
        self.hide()
        self.tray.showMessage("Stream Doctor", "Still running in the system tray. Live Monitor remains available.")

    def _write_smoke_probe(self, path: str) -> None:
        # Minimal compatibility stub so existing smoke probes do not crash
        result = {"window_title": self.windowTitle(), "sidebar": True}
        Path(path).write_text(json.dumps(result), encoding="utf-8")


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
