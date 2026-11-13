from stream_doctor.core import IncidentStore
from stream_doctor.session import ConnectionSettings, MonitoringSession


def test_connection_settings_round_trip_excludes_password(tmp_path):
    path = tmp_path / "connection.json"
    settings = ConnectionSettings("192.168.1.20", 4477, "OBS test instance", "secret")
    settings.save(path)
    loaded = ConnectionSettings.load(path)
    assert loaded.host == "192.168.1.20"
    assert loaded.port == 4477
    assert loaded.label == "OBS test instance"
    assert loaded.password is None
    assert "secret" not in path.read_text()


def test_monitoring_session_records_start_end_and_duration(tmp_path):
    store = IncidentStore(tmp_path / "incidents.sqlite3")
    session = MonitoringSession(store, "OBS 33.0.1 @ 4477")
    session.start()
    session.finish()
    rows = store.search("monitoring session")
    assert len(rows) == 2
    assert session.duration_seconds is not None
    assert rows[0]["evidence"]["instance"] == "OBS 33.0.1 @ 4477"
