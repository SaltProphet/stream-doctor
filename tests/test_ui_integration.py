import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from stream_doctor.app import FloatingMonitor, MainWindow


def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module", autouse=True)
def shutdown_qt():
    yield
    app = QApplication.instance()
    if app is not None:
        for widget in list(app.topLevelWidgets()):
            widget.setAttribute(Qt.WA_DeleteOnClose, True)
            widget.close()
            widget.deleteLater()
        app.processEvents()
        app.quit()


def test_overlay_enters_emergency_for_nested_black_video_record():
    _app()
    overlay = FloatingMonitor()
    overlay.update_sample({
        "sample": {"stream_active": True, "bitrate_kbps": 3500, "dropped_frames": 0, "total_frames": 100},
        "diagnosis": {"category": "healthy"},
        "video": {"black_ratio": 1.0, "is_black": True},
    })
    assert overlay.emergency is True
    assert "black" in overlay.warning.text().lower()
    overlay.close()


def test_overlay_clears_black_emergency_and_handles_multiple_cycles():
    _app()
    overlay = FloatingMonitor()
    normal = {"sample": {"stream_active": True, "bitrate_kbps": 3500}, "diagnosis": {"category": "healthy"}, "video": {"black_ratio": 0.0, "is_black": False}}
    black = {"sample": {"stream_active": True, "bitrate_kbps": 3500}, "diagnosis": {"category": "healthy"}, "video": {"black_ratio": 1.0, "is_black": True}}
    overlay.update_sample(black)
    assert overlay.emergency
    overlay.update_sample(normal)
    assert not overlay.emergency
    overlay.update_sample(black)
    assert overlay.emergency
    overlay.close()


def test_incidents_screen_exposes_report_export_control():
    _app()
    window = MainWindow()
    buttons = {button.text() for button in window.findChildren(QPushButton)}
    assert "Export report" in buttons
    window.overlay.close()
    window.close()


def test_optimizer_exposes_explicit_apply_and_rollback_controls():
    _app()
    window = MainWindow()
    buttons = {button.text(): button for button in window.findChildren(QPushButton)}
    assert "Apply selected recommendation" in buttons
    assert "Rollback last change" in buttons
    assert buttons["Apply selected recommendation"].isEnabled() is False
    assert buttons["Rollback last change"].isEnabled() is False
    window.overlay.close()
    window.close()


def test_report_export_control_writes_selected_json_and_html(monkeypatch, tmp_path):
    _app()
    window = MainWindow()
    window.last_record = {"sample": {"stream_active": True}}
    window.store.record("blackout", "Black video detected", {"password": "secret", "black_ratio": 1.0})
    messages = []
    monkeypatch.setattr("stream_doctor.app.QMessageBox.information", lambda *args: messages.append("ok"))
    monkeypatch.setattr("stream_doctor.app.QMessageBox.critical", lambda *args: messages.append("error"))
    json_path = tmp_path / "diagnostic.json"
    html_path = tmp_path / "diagnostic.html"
    choices = iter([(str(json_path), "JSON report (*.json)"), (str(html_path), "HTML report (*.html)")])
    monkeypatch.setattr("stream_doctor.app.QFileDialog.getSaveFileName", lambda *args: next(choices))
    window._export_report()
    window._export_report()
    assert json_path.exists() and html_path.exists()
    assert "secret" not in json_path.read_text(encoding="utf-8")
    assert "Black video detected" in html_path.read_text(encoding="utf-8")
    assert messages == ["ok", "ok"]
    window.overlay.close()
    window.close()


def test_optimizer_receives_shared_measured_inputs(monkeypatch):
    _app()
    window = MainWindow()
    calls = []
    monkeypatch.setattr(window.engine, "recommend", lambda **kwargs: calls.append(kwargs) or [])
    window._hardware_measurements = {"logical_cpus": 8, "ram_gb": 28.0, "gpus": ["NVIDIA RTX A4500"]}
    window._network_measurements = {"latency_ms": 36.0, "upload_mbps": None}
    window._obs_config = {"fpsNumerator": 30, "fpsDenominator": 1, "outputWidth": 1280, "outputHeight": 720, "encoder": "nvenc"}
    window._on_sample({"sample": {"stream_active": False, "bitrate_kbps": 4200}, "diagnosis": {"category": "healthy"}, "video": None})
    assert calls[0]["obs"]["fps"] == 30.0
    assert calls[0]["hardware"]["gpus"] == ["NVIDIA RTX A4500"]
    assert calls[0]["network"]["upload_mbps"] is None
    assert "Unavailable" in window.opt_status.text()
    window.overlay.close()
    window.close()


def test_upload_measurement_is_blocked_during_active_stream():
    _app()
    window = MainWindow()
    window.last_record = {"sample": {"stream_active": True}}
    window.upload_host_edit.setText("127.0.0.1")
    window._measure_upload_capacity()
    assert "disabled while streaming" in window.preflight_summary.text()
    window.overlay.close()
    window.close()


def test_dashboard_start_and_stop_follow_monitor_worker(monkeypatch):
    from PySide6.QtCore import QObject, Signal

    class Worker(QObject):
        state_changed = Signal(str)
        sample_received = Signal(dict)
        finished = Signal()

        def __init__(self, settings, store):
            super().__init__()
            self.hardware_measurements = {}

        def start(self):
            self.state_changed.emit("Connected: test OBS")

        def stop_monitoring(self):
            self.stopping = True

    _app()
    monkeypatch.setattr("stream_doctor.app.MonitorWorker", Worker)
    window = MainWindow()
    window.monitor_button.click()
    worker = window.worker
    assert window.monitor_button.text() == "Stop monitoring"
    assert "Connected" in window.obs_status.text()
    worker.sample_received.emit({
        "sample": {"stream_active": True, "bitrate_kbps": 4600},
        "blackout_active": True,
    })
    assert "4600" in window.m_bitrate.text()
    assert window.overlay.emergency
    window.monitor_button.click()
    assert worker.stopping
    assert not window.monitor_button.isEnabled()
    worker.finished.emit()
    assert window.monitor_button.text() == "Start monitoring"
    assert window.m_bitrate.text() == "Unavailable"
    assert window.stream_status.text() == "Stream: Not observed"
    window.overlay.close()
    window.close()


def test_optimizer_preserves_preflight_upload_when_live_samples_arrive():
    from datetime import datetime, timezone

    _app()
    window = MainWindow()
    window._network_measurements = {
        "upload_mbps": 5.0, "upload_measured_at": datetime.now(timezone.utc).isoformat(),
    }
    window._obs_config = {"profile": "Disposable", "mode": "Simple", "bitrate": 6000}
    window._on_sample({"sample": {"stream_active": False, "bitrate_kbps": None}})
    assert window.opt_table.item(0, 1).text() == "6000.0"
    assert window.opt_table.item(0, 2).text() == "3500"
    assert window.opt_apply.isEnabled()
    window._on_sample({"sample": {"stream_active": True, "bitrate_kbps": 4900}})
    assert window.opt_table.item(0, 1).text() == "6000.0"
    assert not window.opt_apply.isEnabled()
    window.overlay.close()
    window.close()
