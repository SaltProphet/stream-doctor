"""Operator-triggered OBS program scene recovery."""
from .video import FrameAnalyzer, analyze_png_data


async def switch_program_scene(client, expected_scene: str, target_scene: str) -> dict:
    if not target_scene or target_scene == expected_scene:
        raise ValueError("Choose a different OBS program scene.")

    async def current():
        return (await client.request("GetCurrentProgramScene")).get("currentProgramSceneName")

    async def black(scene):
        frame = await client.capture_program_screenshot(scene)
        return analyze_png_data(frame["imageData"], FrameAnalyzer()).is_black

    if await current() != expected_scene:
        raise ValueError("The OBS program scene changed. Check the current scene and retry.")
    if await black(target_scene):
        raise ValueError("The selected scene also previews as black. Choose another scene.")
    if await current() != expected_scene:
        raise ValueError("The OBS program scene changed while checking the target.")
    await client.request("SetCurrentProgramScene", {"sceneName": target_scene})
    if await current() != target_scene:
        raise ValueError("OBS did not confirm the scene switch. Check OBS before retrying.")
    return {"previous_scene": expected_scene, "scene": target_scene, "program_black": await black(target_scene)}
