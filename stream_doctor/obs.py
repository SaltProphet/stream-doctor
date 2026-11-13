from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any

import websockets


class ObsConnectionError(RuntimeError):
    pass


class ObsClient:
    """Small OBS WebSocket 5.x client using the documented hello/identify flow."""
    def __init__(self, url: str = "ws://127.0.0.1:4455", password: str | None = None, request_timeout: float = 10.0):
        self.url, self.password, self.socket = url, password, None
        self.request_timeout = request_timeout
        self._counter = 0
        self._request_lock = asyncio.Lock()

    async def connect(self) -> dict[str, Any]:
        try:
            self.socket = await websockets.connect(self.url, subprotocols=["obswebsocket.json"], open_timeout=self.request_timeout, close_timeout=2)
            hello = json.loads(await asyncio.wait_for(self.socket.recv(), timeout=self.request_timeout))
            data = hello["d"]
            identify: dict[str, Any] = {"rpcVersion": data.get("rpcVersion", 1)}
            auth = data.get("authentication")
            if auth and self.password is not None:
                secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
                identify["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
            await self.socket.send(json.dumps({"op": 1, "d": identify}))
            identified = json.loads(await asyncio.wait_for(self.socket.recv(), timeout=self.request_timeout))
            if identified.get("op") != 2:
                raise ObsConnectionError("OBS rejected WebSocket identification")
            return identified["d"]
        except Exception as exc:
            raise ObsConnectionError(str(exc)) from exc

    async def request(self, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.socket is None:
            raise ObsConnectionError("OBS is not connected")
        async with self._request_lock:
            self._counter += 1
            request_id = str(self._counter)
            await self.socket.send(json.dumps({"op": 6, "d": {"requestType": request_type, "requestId": request_id, "requestData": request_data or {}}}))
            while True:
                response = json.loads(await asyncio.wait_for(self.socket.recv(), timeout=self.request_timeout))
                if response.get("op") == 7 and response["d"].get("requestId") == request_id:
                    data = response["d"]
                    if not data.get("requestStatus", {}).get("result", False):
                        raise ObsConnectionError(data.get("requestStatus", {}).get("comment", "OBS request failed"))
                    return data.get("responseData", {})

    async def snapshot(self) -> dict[str, Any]:
        stats, status, video, service, outputs, scene = await asyncio.gather(*(self.request(x) for x in ("GetStats", "GetStreamStatus", "GetVideoSettings", "GetStreamServiceSettings", "GetOutputList", "GetCurrentProgramScene")))
        output_items = outputs.get("outputs", [])
        output_status = {}
        output_settings = {}
        if output_items:
            output_name = output_items[0].get("outputName")
            if output_name:
                output_status = await self.request("GetOutputStatus", {"outputName": output_name})
                raw_settings = await self.request("GetOutputSettings", {"outputName": output_name})
                raw_values = raw_settings.get("outputSettings", {})
                output_settings = {
                    "outputName": output_name,
                    "outputSettings": {key: raw_values[key] for key in ("encoder", "outputEncoder", "bitrate", "rate_control", "rateControl") if key in raw_values},
                }
        service.pop("streamServiceSettings", None)  # credentials never enter the diagnostic snapshot
        return {"stats": stats, "stream": status, "video": video, "service": service, "output": output_status, "output_settings": output_settings, "outputs": outputs, "scene": scene}

    async def capture_program_screenshot(self, scene_name: str, width: int = 160, height: int = 90) -> dict[str, Any]:
        return await self.request("GetSourceScreenshot", {"sourceName": scene_name, "imageFormat": "png", "imageWidth": width, "imageHeight": height})

    async def discover_encoder_capabilities(self) -> dict[str, Any]:
        """Report OBS encoder evidence without confusing GPU presence with support."""
        version = await self.request("GetVersion")
        output_list = await self.request("GetOutputList")
        outputs = output_list.get("outputs", [])
        current_encoder = None
        if outputs and outputs[0].get("outputName"):
            settings = await self.request("GetOutputSettings", {"outputName": outputs[0]["outputName"]})
            output_settings = settings.get("outputSettings", {})
            current_encoder = output_settings.get("encoder") or output_settings.get("outputEncoder")
        available = version.get("availableRequests", [])
        complete = "GetEncoderList" in available
        return {
            "current_encoder": current_encoder,
            "supported_encoders": [current_encoder] if current_encoder else [],
            "complete": complete,
            "source": "obs_current_output_settings",
            "unavailable_reason": None if complete else "OBS WebSocket does not expose a complete encoder enumeration request",
        }

    async def close(self) -> None:
        if self.socket:
            await self.socket.close()
            self.socket = None
