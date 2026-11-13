import asyncio

from stream_doctor.obs import ObsClient


def test_encoder_discovery_distinguishes_current_encoder_from_complete_support_list():
    client = ObsClient()

    async def request(request_type, request_data=None):
        if request_type == "GetVersion":
            return {"availableRequests": ["GetOutputSettings"]}
        if request_type == "GetOutputList":
            return {"outputs": [{"outputName": "streaming"}]}
        if request_type == "GetOutputSettings":
            return {"outputSettings": {"encoder": "obs_nvenc_h264_tex"}}
        raise AssertionError(request_type)

    client.request = request
    result = asyncio.run(client.discover_encoder_capabilities())
    assert result["current_encoder"] == "obs_nvenc_h264_tex"
    assert result["supported_encoders"] == ["obs_nvenc_h264_tex"]
    assert result["complete"] is False
