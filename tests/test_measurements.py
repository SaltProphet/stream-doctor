from stream_doctor.measurements import build_optimizer_inputs
from stream_doctor.monitoring import _safe_obs_config


def test_obs_config_record_excludes_stream_service_credentials():
    config = _safe_obs_config({
        "video": {"fpsNumerator": 30, "fpsDenominator": 1},
        "output_settings": {"outputSettings": {"encoder": "nvenc", "key": "secret", "server": "rtmp://private"}},
    })
    assert config == {"fpsNumerator": 30, "fpsDenominator": 1, "encoder": "nvenc"}


def test_optimizer_inputs_use_observed_obs_and_hardware_values():
    inputs = build_optimizer_inputs(
        sample={"bitrate_kbps": 4200},
        obs_config={
            "fpsNumerator": 30,
            "fpsDenominator": 1,
            "outputWidth": 1280,
            "outputHeight": 720,
            "encoder": "obs_nvenc_h264_tex",
        },
        hardware={"logical_cpus": 8, "ram_gb": 28.0, "gpus": ["NVIDIA RTX A4500"]},
        network={"latency_ms": 36.0, "upload_mbps": None},
        incidents=[{"category": "blackout"}],
    )
    assert inputs["obs"]["fps"] == 30.0
    assert inputs["obs"]["resolution"] == "1280x720"
    assert inputs["obs"]["encoder"] == "obs_nvenc_h264_tex"
    assert inputs["hardware"]["gpus"] == ["NVIDIA RTX A4500"]
    assert inputs["network"]["upload_mbps"] is None
    assert inputs["incidents"][0]["category"] == "blackout"


def test_optimizer_inputs_do_not_invent_missing_measurements():
    inputs = build_optimizer_inputs(sample={}, obs_config={}, hardware={}, network={}, incidents=[])
    assert inputs["obs"]["fps"] is None
    assert inputs["obs"]["bitrate_kbps"] is None
    assert inputs["network"]["upload_mbps"] is None
