from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

from PySide6.QtCore import QThread, Qt, Signal, QTimer
from PySide6.QtGui import QAction, QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSlider, QSpinBox, QStackedWidget,
    QScrollArea, QSizeGrip, QStyle, QHeaderView, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .core import IncidentStore, RecommendationEngine
from .monitoring import LiveMonitor, compare_broadcasts, summarize_broadcast
from .measurements import build_optimizer_inputs, fresh_upload
from .network import measure_cloudflare_upload, measure_network_quality, measure_upload_capacity
from .obs import ObsClient, ObsConnectionError
from .optimizer import change_profile_bitrate, inspect_stream_profile
from .recovery import switch_program_scene
from .reports import export_report, report_format_for_path
from .session import ConnectionSettings, MonitoringSession
from .telemetry import hardware_snapshot


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
        self.hardware_measurements = {}
        self._stop_requested = False

    def run(self) -> None:
        async def work() -> None:
            client = ObsClient(self.settings.url, self.settings.password)
            try:
                self.state_changed.emit(f"Connecting to {self.settings.url} …")
                await client.connect()
                if self._stop_requested:
                    return
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
                if not self._stop_requested:
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
        if len(self.report_records) > 600:
            del self.report_records[0]
        self.sample_received.emit(record)

    def stop_monitoring(self) -> None:
        self._stop_requested = True
        if self.monitor:
            self.monitor.stop()


# ---------------------------------------------------------------------------
# UI components
# ---------------------------------------------------------------------------
class TaskWorker(QThread):
    def __init__(self, task, parent):
        super().__init__(parent)
        self.task = task

    def run(self):
        try:
            result = self.task()
        except Exception as exc:
            self.result = (None, str(exc))
        else:
            self.result = (result, "")


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
            painter.setPen(QColor("#b6bfcc"))
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
        self.warning.setStyleSheet("color: #ffae79;")
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
        self.grip = QSizeGrip(self)
        self.grip.setAccessibleName("Resize floating monitor")
        actions.addWidget(self.grip)
        root.addLayout(actions)
        self.details = (self.dropped_label, self.opacity, controls.itemAt(0).widget(), inc, self.grip)
        self.opacity.setAccessibleName("Floating monitor opacity")
        self.opacity.sliderReleased.connect(self._save_geometry)
        self._apply_mode()

    def _apply_mode(self, resize=True) -> None:
        expanded = self.mode == "expanded"
        compact = self.mode == "compact"
        self.chart.setVisible(expanded)
        for widget in self.details:
            widget.setVisible(expanded or compact)
        self.render_label.setVisible(expanded or compact)
        self.warning.setVisible(expanded or self.emergency)
        if not resize:
            return
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
        changed = self.emergency != active
        self.emergency = active
        self.setObjectName("overlayEmergency" if active else "overlayWindow")
        if changed:
            self.style().unpolish(self)
            self.style().polish(self)
        if active:
            self.warning.setText(message or "Emergency condition detected")
            self.warning.setStyleSheet("color: #ffaaa5; font-weight: 600;")
        else:
            self.warning.setStyleSheet("color: #ffae79;")
        self._apply_mode(resize=False)

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
            "OBS: Disconnected" if record.get("_obs_connected") is False else
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
        elif record.get("blackout_active", black is True or (isinstance(black, (int, float)) and black >= 0.98)):
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
            self.mode = data.get("mode", "compact")
            self.opacity.setValue(data.get("opacity", 90))
            self._apply_mode(resize=False)
            self.resize(data.get("w", 320), data.get("h", 180))
        except (OSError, ValueError, TypeError, KeyError):
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
        self._hardware_measurements = {}
        self._network_measurements = {}
        self._obs_config: dict = {}
        self._preflight_settings = None
        self._optimizer_recommendations: list[dict] = []
        self._optimizer_backup: dict | None = None
        self._task = None
        self._quitting = False
        self._last_optimizer_view = None
        self._last_stream_active = False
        self._broadcast_samples: list[dict] = []
        self._last_broadcast_metrics: dict | None = None
        self._pending_correction: dict | None = None

        self.overlay = FloatingMonitor()
        self.overlay.open_dashboard.connect(self._show_from_overlay)
        self.overlay.open_incident.connect(lambda: self._show_screen("Incidents"))

        self._build_ui()
        self._build_tray()
        self._apply_theme()
        self.monitor_button.setFocus()

        smoke_file = os.environ.get("STREAM_DOCTOR_SMOKE_FILE")
        if smoke_file:
            QTimer.singleShot(1000, lambda: self._write_smoke_probe(smoke_file))

    def _apply_theme(self) -> None:
        qss = Path(__file__).with_name("theme.qss")
        if qss.exists():
            theme = qss.read_text(encoding="utf-8")
            self.setStyleSheet(theme)
            self.overlay.setStyleSheet(theme)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(150)
        side_layout = QVBoxLayout(sidebar)
        self.nav = QListWidget()
        self.nav.setAccessibleName("Main navigation")
        self.nav.setObjectName("sidebarList")
        for name in ("Dashboard", "Preflight", "Diagnostics", "Optimizer", "Incidents", "Settings"):
            self.nav.addItem(QListWidgetItem(name))
        self.nav.currentRowChanged.connect(self._switch)
        side_layout.addWidget(self.nav)
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
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
        for table in (self.preflight_table, self.opt_table, self.inc_table):
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.setCurrentRow(0)

    def _switch(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        if row == 4:
            self._refresh_incidents()

    def _show_screen(self, name: str) -> None:
        idx = list(self.pages.keys()).index(name)
        self.nav.setCurrentRow(idx)
        self.showNormal()
        self.raise_()

    def _dashboard_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QLabel("Dashboard")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        strip = QFrame()
        strip.setObjectName("statusStrip")
        strip_l = QHBoxLayout(strip)
        strip_l.setSpacing(20)
        self.obs_status = QLabel("OBS: Not checked")
        self.stream_status = QLabel("Stream: Not observed")
        self.health_status = QLabel("Monitoring: Stopped")
        for w in (self.obs_status, self.stream_status, self.health_status):
            strip_l.addWidget(w)
        strip_l.addStretch()
        layout.addWidget(strip)

        controls = QHBoxLayout()
        self.connect_button = QPushButton("Connect to OBS")
        self.connect_button.setToolTip("Check OBS WebSocket using the endpoint in Settings.")
        self.connect_button.clicked.connect(self._test_connection)
        self.monitor_button = QPushButton("Start monitoring")
        self.monitor_button.setObjectName("primary")
        self.monitor_button.setAccessibleDescription("Starts or stops Stream Doctor telemetry only.")
        self.monitor_button.clicked.connect(self._toggle_monitoring)
        controls.addWidget(self.monitor_button)
        controls.addWidget(self.connect_button)
        controls.addStretch()
        layout.addLayout(controls)

        actions = QHBoxLayout()
        preflight = QPushButton("Run Preflight")
        preflight.clicked.connect(self._open_preflight)
        optimizer = QPushButton("Open Optimizer")
        optimizer.clicked.connect(lambda: self._show_screen("Optimizer"))
        self.overlay_enabled = QCheckBox("Floating monitor")
        self.overlay_enabled.toggled.connect(self._toggle_overlay)
        for control in (preflight, optimizer, self.overlay_enabled):
            actions.addWidget(control)
        actions.addStretch()
        layout.addLayout(actions)

        self.dashboard_warning = QLabel("Start monitoring to see live stream health.")
        self.dashboard_warning.setWordWrap(True)
        self.dashboard_warning.setObjectName("dashboardWarning")
        layout.addWidget(self.dashboard_warning)
        self.congestion_evidence = QLabel("Sustained congestion evidence: waiting for an active broadcast.")
        self.congestion_evidence.setWordWrap(True)
        self.congestion_evidence.setObjectName("dashboardEvidence")
        layout.addWidget(self.congestion_evidence)
        self.dashboard_correction = QLabel("Measured upload bitrate correction: run Preflight while OBS is idle.")
        self.dashboard_correction.setWordWrap(True)
        layout.addWidget(self.dashboard_correction)
        self.next_broadcast_result = QLabel("Next-broadcast result: apply a correction, then monitor the next broadcast.")
        self.next_broadcast_result.setWordWrap(True)
        layout.addWidget(self.next_broadcast_result)
        correction_actions = QHBoxLayout()
        self.dashboard_apply = QPushButton("Apply measured bitrate correction")
        self.dashboard_apply.setEnabled(False)
        self.dashboard_apply.clicked.connect(self._apply_optimizer_recommendation)
        self.dashboard_rollback = QPushButton("Rollback bitrate correction")
        self.dashboard_rollback.setEnabled(False)
        self.dashboard_rollback.clicked.connect(self._rollback_optimizer_change)
        correction_actions.addWidget(self.dashboard_apply)
        correction_actions.addWidget(self.dashboard_rollback)
        correction_actions.addStretch()
        layout.addLayout(correction_actions)
        self.recover_video_button = QPushButton("Choose recovery scene…")
        self.recover_video_button.clicked.connect(self._open_scene_recovery)
        self.recover_video_button.hide()
        layout.addWidget(self.recover_video_button, 0, Qt.AlignLeft)

        self.bitrate_chart = MiniChart()
        self.dropped_chart = MiniChart()
        self.render_chart = MiniChart()
        self.m_bitrate = QLabel("Unavailable")
        self.m_dropped = QLabel("Unavailable")
        self.m_render = QLabel("Unavailable")
        self.m_network = QLabel("Unavailable")
        for metrics in (
            (("Observed bitrate", self.m_bitrate, self.bitrate_chart),
             ("Dropped frames", self.m_dropped, self.dropped_chart)),
            (("Render time", self.m_render, self.render_chart),
             ("Network loss", self.m_network, None)),
        ):
            row = QHBoxLayout()
            for title, value, chart in metrics:
                card = QFrame()
                card.setObjectName("metricCard")
                box = QVBoxLayout(card)
                box.addWidget(QLabel(title))
                value.setObjectName("metricValue")
                box.addWidget(value)
                if chart:
                    box.addWidget(chart)
                else:
                    box.addStretch()
                row.addWidget(card, 1)
            layout.addLayout(row)
        layout.addStretch()
        return page

    def _preflight_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("<b>Preflight</b> — measured checks only"))
        self.preflight_table = QTableWidget(0, 3)
        self.preflight_table.setAccessibleName("Preflight results")
        self.preflight_table.setHorizontalHeaderLabels(["Check", "Result", "Detail"])
        layout.addWidget(self.preflight_table)
        run = QPushButton("Run Preflight")
        run.setObjectName("primary")
        run.clicked.connect(self._run_preflight)
        layout.addWidget(run)
        self.preflight_summary = QLabel("Run Preflight to inspect OBS, hardware and Internet upload.")
        self.preflight_summary.setWordWrap(True)
        layout.addWidget(self.preflight_summary)
        target = QFormLayout()
        self.upload_host_edit = QLineEdit()
        self.upload_host_edit.setPlaceholderText("Optional TCP test server")
        self.upload_port_edit = QSpinBox()
        self.upload_port_edit.setRange(1, 65535)
        self.upload_port_edit.setValue(443)
        target.addRow("Network test host", self.upload_host_edit)
        target.addRow("Network test port", self.upload_port_edit)
        layout.addLayout(target)
        network_button = QPushButton("Test custom TCP server")
        network_button.clicked.connect(self._measure_upload_capacity)
        layout.addWidget(network_button)
        internet_button = QPushButton("Retest Internet upload")
        internet_button.clicked.connect(self._measure_cloudflare_upload)
        layout.addWidget(internet_button)
        manual = QHBoxLayout()
        self.manual_upload = QDoubleSpinBox()
        self.manual_upload.setRange(0, 10000)
        self.manual_upload.setDecimals(2)
        self.manual_upload.setSuffix(" Mbps")
        self.manual_upload.setAccessibleName("Externally measured upload speed")
        self.manual_upload.setToolTip("Enter a recent upload measurement taken on this computer while OBS was idle.")
        use_measurement = QPushButton("Use external upload result")
        use_measurement.clicked.connect(self._use_external_upload)
        manual.addWidget(self.manual_upload)
        manual.addWidget(use_measurement)
        manual.addStretch()
        layout.addLayout(manual)
        return page

    def _use_external_upload(self) -> None:
        value = self.manual_upload.value()
        if value <= 0:
            self.preflight_summary.setText("Enter a recent upload result in Mbps.")
            return
        self._network_measurements.update({
            "upload_mbps": value,
            "upload_measured_at": datetime.now(timezone.utc).isoformat(),
            "upload_source": "External test entered by operator",
        })
        self.preflight_summary.setText(
            f"Using external result: {value:.2f} Mbps. This measures the external test path, which can differ from the broadcast route."
        )
        self._refresh_optimizer()

    def _open_preflight(self) -> None:
        self._show_screen("Preflight")
        self._run_preflight()

    def _run_preflight(self) -> None:
        self._start_preflight()

    def _measure_cloudflare_upload(self) -> None:
        self._start_preflight()

    def _measure_upload_capacity(self) -> None:
        if self.last_record.get("sample", {}).get("stream_active"):
            self.preflight_summary.setText("Upload measurement is disabled while streaming.")
            return
        host = self.upload_host_edit.text().strip()
        if not host:
            self.preflight_summary.setText("Enter a TCP test server that accepts and discards test data.")
            return
        self._start_preflight((host, self.upload_port_edit.value()))

    def _start_preflight(self, custom_endpoint=None) -> None:
        settings = self._current_settings()

        async def check_obs():
            client = ObsClient(settings.url, settings.password)
            try:
                await client.connect()
                config = await inspect_stream_profile(client)
                inputs = (await client.request("GetInputList")).get("inputs", [])
                cameras = [item["inputName"] for item in inputs
                           if item.get("inputKind") in {"dshow_input", "v4l2_input", "av_capture_input"}]
                status = await client.request("GetStreamStatus")
                return config, cameras, status.get("outputActive") is False and not status.get("outputReconnecting")
            finally:
                await client.close()

        async def preflight():
            hardware = hardware_snapshot()
            quality = measure_network_quality("8.8.8.8", 443, samples=5)
            try:
                config, cameras, idle = await check_obs()
            except Exception as exc:
                return hardware, quality, {}, [], None, f"OBS unavailable: {exc}"
            if not idle:
                return hardware, quality, config, cameras, None, "Upload measurement is disabled while streaming."
            if custom_endpoint:
                result = await asyncio.to_thread(measure_upload_capacity, *custom_endpoint)
                detail = f"Custom TCP throughput: {result.upload_mbps:g} Mbps. Run the Internet test for optimization." if result.upload_mbps is not None else "Custom TCP upload unavailable."
                return hardware, quality, config, cameras, None, detail
            result = await asyncio.to_thread(measure_cloudflare_upload)
            detail = result.error or "No upload samples completed" if result.upload_mbps is None else ""
            return hardware, quality, config, cameras, result, detail

        if self._run_task(lambda: asyncio.run(preflight()),
                          lambda result, error: self._preflight_finished(result, error, settings)):
            self.preflight_summary.setText("Checking OBS, hardware and network…")
        else:
            self.preflight_summary.setText("Wait for the current check to finish.")

    def _preflight_finished(self, result, error, settings) -> None:
        if error:
            self.preflight_summary.setText(f"Preflight failed: {error}")
            return
        hardware, quality, config, cameras, upload, detail = result
        self._hardware_measurements.update(hardware)
        self._obs_config = config
        self._preflight_settings = settings
        self._network_measurements.update({
            "latency_ms": quality.latency_ms, "jitter_ms": quality.jitter_ms,
            "tcp_failure_percent": quality.packet_loss_percent,
        })
        if upload is not None and upload.upload_mbps is not None:
            self._network_measurements.update({
                "upload_mbps": upload.upload_mbps, "upload_measured_at": upload.measured_at,
                "upload_source": "Cloudflare speed test",
            })
        elif self._network_measurements.get("upload_source") == "Cloudflare speed test":
            self._network_measurements.pop("upload_mbps", None)
        self.preflight_summary.setText(
            detail or f"Internet upload: {upload.upload_mbps:.2f} Mbps across {upload.samples} samples ({upload.bytes_transferred:,} bytes)."
        )
        rows = [
            ("OBS profile", config.get("profile"), config.get("mode")),
            ("Video output", f"{config['outputWidth']} × {config['outputHeight']}" if config.get("outputWidth") else None,
             f"{config['fpsNumerator'] / config['fpsDenominator']:g} FPS" if config.get("fpsDenominator") else None),
            ("Configured bitrate", config.get("bitrate"), "kbps"),
            ("Configured encoder", config.get("encoder"), "Encoder availability list unavailable via this probe"),
            ("Capture sources", ", ".join(cameras), "Configured in OBS"),
            ("CPU", hardware.get("cpu"), f"{hardware.get('logical_cpus')} logical cores, {hardware.get('cpu_percent')}% load"),
            ("Memory", f"{hardware.get('ram_gb')} GB", f"{hardware.get('ram_percent')}% used"),
            ("GPU", ", ".join(hardware.get("gpus", [])), "GPU utilization unavailable"),
            ("TCP connect latency", quality.latency_ms, "ms to 8.8.8.8"),
            ("TCP connect jitter", quality.jitter_ms, "ms"),
            ("TCP connection failures", quality.packet_loss_percent, "% of attempts"),
            ("Internet upload", upload.upload_mbps if upload else None, "Mbps via Cloudflare"),
        ]
        self.preflight_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for column, value in enumerate(row):
                self.preflight_table.setItem(i, column, QTableWidgetItem(str(value) if value is not None and value != "" else "Unavailable"))
        self.preflight_table.resizeRowsToContents()
        self._refresh_optimizer()

    def _diagnostics_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("<b>Diagnostics</b> — evidence from the latest sample"))
        self.diag_text = QLabel("Start monitoring to collect evidence.")
        self.diag_text.setWordWrap(True)
        layout.addWidget(self.diag_text)
        recovery = QPushButton("Choose recovery scene…")
        recovery.setToolTip("Preview and switch to another OBS program scene without restarting the broadcast.")
        recovery.clicked.connect(self._choose_recovery_scene)
        layout.addWidget(recovery)
        self.recovery_status = QLabel("")
        self.recovery_status.setWordWrap(True)
        layout.addWidget(self.recovery_status)
        layout.addStretch()
        return page

    def _open_scene_recovery(self) -> None:
        self._show_screen("Diagnostics")
        self._choose_recovery_scene()

    def _choose_recovery_scene(self) -> None:
        settings = self.worker.settings if self.worker else self._current_settings()

        async def scenes():
            client = ObsClient(settings.url, settings.password)
            try:
                await client.connect()
                return await client.request("GetSceneList")
            finally:
                await client.close()

        if self._run_task(lambda: asyncio.run(scenes()), lambda result, error: self._recovery_scenes_loaded(settings, result, error)):
            self.recovery_status.setText("Reading OBS scenes…")

    def _recovery_scenes_loaded(self, settings, result, error) -> None:
        if error:
            self.recovery_status.setText(f"Could not read OBS scenes: {error}")
            return
        current = result.get("currentProgramSceneName")
        choices = [scene["sceneName"] for scene in result.get("scenes", []) if scene["sceneName"] != current]
        if not current or not choices:
            self.recovery_status.setText("OBS needs another scene to switch to.")
            return
        target, accepted = QInputDialog.getItem(
            self, "Switch live program scene", "Preview and switch to this OBS scene", choices, 0, False,
        )
        if not accepted:
            self.recovery_status.setText("Scene switch canceled.")
            return
        self._run_recovery_switch(settings, current, target)

    def _run_recovery_switch(self, settings, current, target) -> None:
        async def recover():
            client = ObsClient(settings.url, settings.password)
            try:
                await client.connect()
                return await switch_program_scene(client, current, target)
            finally:
                await client.close()

        def completed(result, error):
            if error:
                self.recovery_status.setText(f"Scene recovery could not be verified: {error}")
                return
            summary = "Recovery scene selected; OBS still reports black video" if result["program_black"] else "Recovery scene selected; OBS video verified"
            self.recovery_status.setText(summary)
            self.store.record("recovery", summary, result)
            self._refresh_incidents()

        if self._run_task(lambda: asyncio.run(recover()), completed):
            self.recovery_status.setText("Checking the target scene and verifying the switch…")
        else:
            self.recovery_status.setText("Another OBS operation is in progress. Try again.")

    def _optimizer_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("<b>Optimizer</b> — recommendations only from measured conditions"))
        self.opt_table = QTableWidget(0, 4)
        self.opt_table.setAccessibleName("OBS recommendations")
        self.opt_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.opt_table.setSelectionMode(QTableWidget.SingleSelection)
        self.opt_table.itemSelectionChanged.connect(self._update_optimizer_buttons)
        self.opt_table.setHorizontalHeaderLabels(["Setting", "Current", "Recommended", "Reason"])
        layout.addWidget(self.opt_table)
        self.opt_status = QLabel("Waiting for measured OBS and network data")
        self.opt_status.setWordWrap(True)
        self.opt_context = QLabel("Run Preflight to read the current OBS profile.")
        self.opt_context.setWordWrap(True)
        layout.addWidget(self.opt_context)
        layout.addWidget(self.opt_status)
        note = QLabel("Bitrate changes apply to Simple Output profiles before the next broadcast. Other recommendations are manual.")
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
        self.inc_table = QTableWidget(0, 6)
        self.inc_table.setAccessibleName("Incident timeline")
        self.inc_table.setHorizontalHeaderLabels(["Local time", "Category", "Summary", "Duration", "Recovery", "Evidence"])
        layout.addWidget(self.inc_table)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_incidents)
        layout.addWidget(refresh)
        export = QPushButton("Export report")
        export.clicked.connect(self._export_report)
        layout.addWidget(export)
        self.clear_history_button = QPushButton("Clear incident history")
        self.clear_history_button.setToolTip("Stop monitoring before clearing saved incidents and events.")
        self.clear_history_button.clicked.connect(self._clear_incident_history)
        layout.addWidget(self.clear_history_button)
        self.incident_status = QLabel("")
        self.incident_status.setWordWrap(True)
        layout.addWidget(self.incident_status)
        self._refresh_incidents()
        return page

    def _refresh_incidents(self) -> None:
        rows = self.store.search()
        self.inc_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            raw = row.get("timestamp", "")
            try:
                stamp = datetime.fromisoformat(raw).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                stamp = raw
            self.inc_table.setItem(i, 0, QTableWidgetItem(stamp))
            self.inc_table.setItem(i, 1, QTableWidgetItem(row.get("category", "")))
            self.inc_table.setItem(i, 2, QTableWidgetItem(row.get("summary", "")))
            ev = row.get("evidence", {})
            dur = row.get("duration_seconds")
            self.inc_table.setItem(i, 3, QTableWidgetItem(f"{dur:.3f} s" if isinstance(dur, (int, float)) else "—"))
            self.inc_table.setItem(i, 4, QTableWidgetItem(row.get("recovery_status") or "Unavailable"))
            evidence = QTableWidgetItem(str(ev)[:80])
            evidence.setToolTip(json.dumps(ev, indent=2))
            self.inc_table.setItem(i, 5, evidence)

    def _clear_incident_history(self) -> None:
        if self.worker is not None:
            return
        choice = QMessageBox.question(
            self, "Clear incident history",
            "Permanently delete all saved incidents and events from Stream Doctor? "
            "Export a report first if you want to keep a copy.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes or self.worker is not None:
            return
        try:
            count = self.store.clear()
        except sqlite3.Error as exc:
            self.incident_status.setText(f"Could not clear history: {exc}")
            return
        self.last_record = {}
        self._refresh_incidents()
        self.incident_status.setText(f"Cleared {count} saved records.")

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
        row = self.opt_table.currentRow()
        if not 0 <= row < len(self._optimizer_recommendations):
            return
        recommendation = self._optimizer_recommendations[row]
        config = self._obs_config
        if recommendation["setting"] != "bitrate_kbps" or config.get("mode") != "Simple":
            return
        if self._optimizer_backup:
            return
        if self._current_settings() != self._preflight_settings:
            self.opt_status.setText("OBS endpoint changed. Run Preflight again.")
            return
        if fresh_upload(self._network_measurements) is None:
            self._refresh_optimizer()
            return
        target = int(recommendation["recommended"])
        current = int(recommendation["current"])
        if QMessageBox.question(self, "Apply bitrate", f"Change OBS profile {config['profile']} from {current} to {target} kbps for the next broadcast?") != QMessageBox.StandardButton.Yes:
            return
        self._write_bitrate(self._current_settings(), config["profile"], current, target, rollback=False)

    def _rollback_optimizer_change(self) -> None:
        if self._optimizer_backup:
            settings, profile, original, applied = self._optimizer_backup
            self._write_bitrate(settings, profile, applied, original, rollback=True)

    def _write_bitrate(self, settings, profile, current, target, *, rollback):
        async def write():
            client = ObsClient(settings.url, settings.password)
            try:
                await client.connect()
                return await change_profile_bitrate(client, profile, current, target)
            finally:
                await client.close()

        def completed(config, error):
            if error:
                self.opt_status.setText(f"OBS setting change failed: {error}")
                return
            self._obs_config = config
            self._optimizer_backup = None if rollback else (settings, profile, current, target)
            if rollback:
                self._pending_correction = None
            else:
                self._pending_correction = {"target": target, "baseline": self._last_broadcast_metrics}
            self._refresh_optimizer()
            self.opt_status.setText(f"OBS verified {'restoration to' if rollback else 'the new setting of'} {target} kbps.")

        if self._run_task(lambda: asyncio.run(write()), completed):
            self.opt_status.setText("Waiting for OBS read-back…")

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

        mode_box = QHBoxLayout()
        for m in ("collapsed", "compact", "expanded"):
            b = QPushButton(m.capitalize())
            b.clicked.connect(lambda _, mode=m: self.overlay.set_mode(mode))
            mode_box.addWidget(b)
        layout.addLayout(mode_box)

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

    def _run_task(self, task, callback) -> bool:
        if self._task is not None:
            return False
        worker = TaskWorker(task, self)
        self._task = worker
        worker.callback = callback
        worker.finished.connect(self._task_finished)
        worker.start()
        return True

    def _task_finished(self) -> None:
        worker = self._task
        self._task = None
        worker.deleteLater()
        self.connect_button.setEnabled(True)
        if not self._quitting:
            worker.callback(*worker.result)
        self._finish_quit_if_ready()

    def _test_connection(self) -> None:
        settings = self._current_settings()

        async def probe():
            client = ObsClient(settings.url, settings.password)
            try:
                await client.connect()
                return await client.request("GetVersion")
            finally:
                await client.close()

        def completed(version, error):
            self._on_state(f"Unavailable: {error}" if error else
                           f"Connected: OBS {version.get('obsVersion')} / WS {version.get('obsWebSocketVersion')}")

        if self._run_task(lambda: asyncio.run(probe()), completed):
            self.connect_button.setEnabled(False)
            self.obs_status.setText("OBS: Connecting…")

    def _toggle_monitoring(self) -> None:
        if self.worker is None:
            self._start_monitoring()
        else:
            self._stop_monitoring()

    def _start_monitoring(self) -> None:
        if self.worker is not None:
            return
        self.settings = self._current_settings()
        self.worker = MonitorWorker(self.settings, self.store)
        self.worker.state_changed.connect(self._on_state)
        self.worker.sample_received.connect(self._on_sample)
        self.worker.finished.connect(self._monitor_finished)
        self.health_status.setText("Monitoring: Starting…")
        self.monitor_button.setText("Stop monitoring")
        self.clear_history_button.setEnabled(False)
        self.incident_status.clear()
        self.worker.start()
        if self.overlay_enabled.isChecked():
            self.overlay.show()

    def _stop_monitoring(self) -> None:
        if self.worker is not None:
            self.worker.stop_monitoring()
            self.monitor_button.setEnabled(False)
            self.monitor_button.setText("Stopping monitoring…")

    def _monitor_finished(self) -> None:
        self.worker.deleteLater()
        self.worker = None
        self.clear_history_button.setEnabled(True)
        self.last_record = {}
        self._last_stream_active = False
        self._broadcast_samples = []
        self.monitor_button.setText("Start monitoring")
        self.monitor_button.setEnabled(True)
        self.health_status.setText("Monitoring: Stopped")
        self.stream_status.setText("Stream: Not observed")
        for metric in (self.m_bitrate, self.m_dropped, self.m_render, self.m_network):
            metric.setText("Unavailable")
        self.recover_video_button.hide()
        self.dashboard_warning.setText("Start monitoring to see live stream health.")
        self.congestion_evidence.setText("Sustained congestion evidence: waiting for an active broadcast.")
        self.overlay.update_sample({})
        self.overlay.status_row.setText("Monitoring stopped")
        self.overlay.set_emergency(False)
        self._update_optimizer_buttons()
        self._refresh_incidents()
        self._finish_quit_if_ready()

    def _on_state(self, text: str) -> None:
        self.connection_status.setText(text)
        if text.startswith("Connected"):
            self.obs_status.setText("OBS: Connected")
            self.connect_button.setText("Reconnect to OBS")
        elif "error" in text.lower() or "Unavailable" in text:
            self.obs_status.setText("OBS: Unavailable")
            self.obs_status.setObjectName("statusCritical")

    def _on_sample(self, record: dict) -> None:
        self.last_record = record
        if self.worker:
            self._hardware_measurements.update(self.worker.hardware_measurements)
        sample = record.get("sample", {})
        diagnosis = record.get("diagnosis", {})
        active = bool(sample.get("stream_active"))
        self._update_broadcast_evidence(sample, active)
        self.stream_status.setText("Stream: Streaming" if active else "Stream: Not streaming")
        cat = diagnosis.get("category", "unknown")
        if record.get("_obs_connected") is False:
            self._on_state("Unavailable: OBS disconnected. Reconnecting…")
            self.stream_status.setText("Stream: Not observed")
        else:
            self.obs_status.setText("OBS: Connected")
        blackout = bool(record.get("blackout_active") or (record.get("video_event") or {}).get("summary") == "Black video detected")
        self.recover_video_button.setVisible(blackout and record.get("_obs_connected") is not False)
        if blackout:
            self.health_status.setText("Health: Black video")
            self.dashboard_warning.setText("OBS program video is black. Check the active scene and capture source.")
        else:
            self.dashboard_warning.setText(diagnosis.get("next_action") or "Waiting for live telemetry.")
            self.health_status.setText(f"Health: {cat.capitalize()}" if active else "Monitoring: Active")

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

        live_config = record.get("obs_config") or {}
        for key in ("fpsNumerator", "fpsDenominator", "outputWidth", "outputHeight"):
            if key in live_config:
                self._obs_config[key] = live_config[key]
        self._refresh_optimizer(sample)
        self.overlay.update_sample(record)
        if record.get("video_event") and self.nav.currentRow() == 4:
            self._refresh_incidents()

    def _update_broadcast_evidence(self, sample: dict, active: bool) -> None:
        if active and not self._last_stream_active:
            self._broadcast_samples = []
            if self._pending_correction:
                self.next_broadcast_result.setText(
                    f"Next-broadcast result: collecting evidence after the {self._pending_correction['target']} kbps correction…"
                )
        if active:
            self._broadcast_samples.append(sample)
            metrics = summarize_broadcast(self._broadcast_samples)
            if metrics["samples"]:
                state = "sustained congestion detected" if metrics["sustained_congestion"] else "no sustained congestion yet"
                self.congestion_evidence.setText(
                    f"Sustained congestion evidence: {state}; {metrics['congestion_samples']} samples ≥ 50%, "
                    f"peak {metrics['max_congestion']:.0%}, skipped +{metrics['skipped_frames']}, reconnects {metrics['reconnects']}."
                )
        elif self._last_stream_active:
            metrics = summarize_broadcast(self._broadcast_samples)
            self._last_broadcast_metrics = metrics
            if self._pending_correction and self._pending_correction.get("baseline"):
                comparison = compare_broadcasts(self._pending_correction["baseline"], metrics)
                outcome = "improved" if comparison["improved"] else "did not improve both measures"
                self.next_broadcast_result.setText(
                    f"Next-broadcast result: {outcome}; congestion {'improved' if comparison['congestion_improved'] else 'not improved'}, "
                    f"skipped frames {'improved' if comparison['skipped_frames_improved'] else 'not improved'} "
                    f"({metrics['skipped_frames']} in this broadcast)."
                )
                self._pending_correction = None
            elif self._pending_correction:
                self.next_broadcast_result.setText("Next-broadcast result: no earlier broadcast baseline was available for comparison.")
        self._last_stream_active = active

    def _refresh_optimizer(self, sample=None) -> None:
        sample = sample if sample is not None else self.last_record.get("sample", {})
        measurements = build_optimizer_inputs(
            sample=sample,
            obs_config=self._obs_config,
            hardware=self._hardware_measurements,
            network=self._network_measurements,
            incidents=[],
        )
        obs = measurements["obs"]
        profile = self._obs_config.get("profile") or "Unavailable"
        bitrate = f"{obs['bitrate_kbps']} kbps" if obs["bitrate_kbps"] else "Unavailable"
        fps = f"{obs['fps']:g} FPS" if obs["fps"] else "Unavailable FPS"
        upload = measurements["network"]["upload_mbps"]
        source = self._network_measurements.get("upload_source")
        upload_text = f"{upload:.2f} Mbps" if upload else "Unavailable"
        if upload and source:
            upload_text += f" ({source})"
        self.opt_context.setText(
            f"Profile: {profile}  •  Bitrate: {bitrate}  •  Internet upload: {upload_text}\n"
            f"{obs['resolution'] or 'Unavailable resolution'}  •  {fps}  •  Encoder: {obs['encoder'] or 'Unavailable'}"
        )
        if upload and obs["bitrate_kbps"]:
            self.dashboard_correction.setText(
                f"Measured upload bitrate correction: {upload:.2f} Mbps basis with a 30% stability margin; "
                "the suggested change is opt-in and applies only while OBS is idle."
            )
        else:
            self.dashboard_correction.setText(
                "Measured upload bitrate correction: unavailable until Preflight measures upload while OBS is idle."
            )
        recs = self.engine.recommend(**measurements)
        self._optimizer_recommendations = recs
        self._update_optimizer_buttons()
        view = (recs, measurements["network"].get("upload_mbps"), self._obs_config.get("mode"))
        if view == self._last_optimizer_view:
            return
        self._last_optimizer_view = view
        self.opt_table.setRowCount(len(recs))
        for i, r in enumerate(recs):
            self.opt_table.setItem(i, 0, QTableWidgetItem(str(r.get("setting"))))
            self.opt_table.setItem(i, 1, QTableWidgetItem(str(r.get("current"))))
            self.opt_table.setItem(i, 2, QTableWidgetItem(str(r.get("recommended"))))
            self.opt_table.setItem(i, 3, QTableWidgetItem(str(r.get("reason"))))
        if recs:
            self.opt_table.selectRow(0)
        self._update_optimizer_buttons()
        self.opt_table.resizeRowsToContents()
        if measurements["network"].get("upload_mbps") is None:
            self.opt_status.setText("Unavailable or stale upload measurement. Run Preflight with OBS idle to get a current Internet upload measurement.")
        elif not obs["bitrate_kbps"]:
            self.opt_status.setText("Configured bitrate unavailable. This release reads the bitrate of Simple Output profiles.")
        elif recs:
            self.opt_status.setText(f"{len(recs)} measured recommendation(s) available; no changes are applied automatically.")
        else:
            self.opt_status.setText("No changes recommended from the current measured conditions.")

    def _update_optimizer_buttons(self) -> None:
        row = self.opt_table.currentRow()
        active = self.last_record.get("sample", {}).get("stream_active")
        apply = (not active and self._optimizer_backup is None and
                 self._obs_config.get("mode") == "Simple" and bool(self._obs_config.get("profile")) and
                 0 <= row < len(self._optimizer_recommendations) and
                 self._optimizer_recommendations[row]["setting"] == "bitrate_kbps")
        self.opt_apply.setEnabled(apply)
        self.opt_rollback.setEnabled(not active and self._optimizer_backup is not None)
        if hasattr(self, "dashboard_apply"):
            self.dashboard_apply.setEnabled(apply)
            self.dashboard_rollback.setEnabled(not active and self._optimizer_backup is not None)

    def _toggle_overlay(self, on: bool) -> None:
        self.overlay.setVisible(on)

    def _show_from_overlay(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        self.tray.setToolTip("Stream Doctor")
        menu = QMenu()
        show = QAction("Open Stream Doctor", self)
        show.triggered.connect(self.showNormal)
        menu.addAction(show)
        overlay = QAction("Show / hide Live Monitor", self)
        overlay.triggered.connect(lambda: self.overlay_enabled.toggle())
        menu.addAction(overlay)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        passthrough = QAction("Floating monitor click-through", self, checkable=True)
        passthrough.toggled.connect(self._set_overlay_passthrough)
        menu.insertAction(quit_action, passthrough)
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _set_overlay_passthrough(self, enabled: bool) -> None:
        self.overlay.setWindowFlag(Qt.WindowTransparentForInput, enabled)
        self.overlay.setVisible(self.overlay_enabled.isChecked())

    def _quit(self) -> None:
        self._quitting = True
        self._stop_monitoring()
        self._finish_quit_if_ready()

    def _finish_quit_if_ready(self) -> None:
        if self._quitting and self.worker is None and self._task is None:
            self.overlay._save_geometry()
            QApplication.quit()

    def closeEvent(self, event):  # type: ignore[no-untyped-def]
        event.ignore()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._quit()
            return
        self.hide()
        self.tray.showMessage("Stream Doctor", "Still running in the system tray. Live Monitor remains available.")

    def _write_smoke_probe(self, path: str) -> None:
        # Minimal compatibility stub so existing smoke probes do not crash
        result = {"window_title": self.windowTitle(), "sidebar": True}
        Path(path).write_text(json.dumps(result), encoding="utf-8")


def main() -> int:
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI" if sys.platform == "win32" else "Sans Serif", 11))
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
