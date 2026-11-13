import pytest

from stream_doctor.recovery import switch_program_scene


class Obs:
    scene = "Broken"

    async def request(self, method, data=None):
        if method == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.scene}
        assert method == "SetCurrentProgramScene"
        self.scene = data["sceneName"]

    async def capture_program_screenshot(self, scene):
        return {"imageData": scene}


@pytest.mark.asyncio
async def test_recovery_checks_target_switches_and_verifies(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("stream_doctor.recovery.analyze_png_data",
                        lambda data, _: SimpleNamespace(is_black=data == "Black"))
    obs = Obs()
    with pytest.raises(ValueError, match="also previews as black"):
        await switch_program_scene(obs, "Broken", "Black")
    with pytest.raises(ValueError, match="scene changed"):
        await switch_program_scene(obs, "Stale", "Fallback")
    assert obs.scene == "Broken"
    result = await switch_program_scene(obs, "Broken", "Fallback")
    assert obs.scene == "Fallback"
    assert result["program_black"] is False
