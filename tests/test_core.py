from datetime import datetime, timezone

from stream_doctor.core import (
    DiagnosticResult,
    IncidentStore,
    RecommendationEngine,
    sanitize_report,
)
from stream_doctor.optimizer import ProfileBackup, verify_settings
from stream_doctor.reports import export_report
from stream_doctor.reports import report_format_for_path
from stream_doctor.optimizer import apply_supported_output_settings, rollback_output_settings


def test_diagnostic_result_is_not_ready_when_required_evidence_is_missing():
    result = DiagnosticResult.from_checks([
        ("OBS connected", True, "OBS WebSocket 5.x connected"),
        ("Upload capacity", None, "Measurement unavailable"),
    ])
    assert result.status == "WARNING"
    assert result.next_action == "Measure upload capacity before broadcasting."


def test_recommendation_requires_measured_upload_capacity():
    recommendations = RecommendationEngine().recommend(
        obs={"width": 1920, "height": 1080, "fps": 60, "bitrate_kbps": 6000},
        hardware={"encoder": "h264_nvenc"},
        network={"upload_mbps": None},
    )
    assert recommendations == []


def test_incident_store_persists_and_searches_local_incidents(tmp_path):
    store = IncidentStore(tmp_path / "incidents.sqlite3")
    store.record("network", "Packet loss detected", {"loss_percent": 4.2})
    results = store.search("packet")
    assert len(results) == 1
    assert results[0]["category"] == "network"
    assert results[0]["evidence"]["loss_percent"] == 4.2


def test_report_sanitization_removes_credentials_and_identifiers():
    report = sanitize_report({
        "streamServiceSettings": {"key": "SECRET", "server": "rtmp://example"},
        "room": "private-room",
        "stats": {"activeFps": 30},
    })
    assert "SECRET" not in str(report)
    assert "private-room" not in str(report)
    assert report["stats"]["activeFps"] == 30


def test_profile_backup_can_restore_original_file(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "basic.ini").write_text("bitrate=6000")
    backup = ProfileBackup(tmp_path / "backups").create(profile)
    (profile / "basic.ini").write_text("bitrate=1000")
    ProfileBackup(tmp_path / "backups").rollback(backup, profile)
    assert (profile / "basic.ini").read_text() == "bitrate=6000"


def test_settings_verification_reports_mismatches():
    result = verify_settings({"bitrate": 6000}, {"bitrate": 5000}, {"bitrate": 6000})
    assert result["verified"] is False
    assert result["mismatches"]["bitrate"]["actual"] == 5000


def test_html_report_is_sanitized(tmp_path):
    path = export_report({"streamKey": "hidden"}, tmp_path / "report.html", "html")
    assert "hidden" not in path.read_text()


def test_report_format_follows_user_selected_extension():
    assert report_format_for_path("diagnostic.html") == "html"
    assert report_format_for_path("diagnostic.json") == "json"


def test_optimizer_applies_supported_output_setting_and_verifies_readback():
    import asyncio

    class Client:
        def __init__(self):
            self.settings = {"bitrate": 3500, "encoder": "x264"}
        async def request(self, request_type, data=None):
            if request_type == "GetOutputSettings":
                return {"outputSettings": dict(self.settings)}
            if request_type == "SetOutputSettings":
                self.settings.update(data["outputSettings"])
                return {}
            raise AssertionError(request_type)

    client = Client()
    result = asyncio.run(apply_supported_output_settings(client, "streaming", {"bitrate": 2500}))
    assert result["verified"] is True
    assert result["backup"] == {"bitrate": 3500}
    assert client.settings["bitrate"] == 2500
    asyncio.run(rollback_output_settings(client, "streaming", result["backup"]))
    assert client.settings["bitrate"] == 3500
