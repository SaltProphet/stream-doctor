import asyncio
import json

import pytest

from stream_doctor.obs import ObsClient


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.incoming = [
            json.dumps({"op": 0, "d": {"rpcVersion": 1}}),
            json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}),
            json.dumps({"op": 7, "d": {"requestId": "1", "requestStatus": {"result": True}, "responseData": {"cpuUsage": 2.5}}}),
        ]

    async def recv(self):
        return self.incoming.pop(0)

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_obs_websocket_5_handshake_and_request(monkeypatch):
    socket = FakeSocket()

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setattr("stream_doctor.obs.websockets.connect", connect)
    client = ObsClient()
    await client.connect()
    response = await client.request("GetStats")
    assert response["cpuUsage"] == 2.5
    assert socket.sent[0]["op"] == 1
    assert socket.sent[0]["d"]["eventSubscriptions"] == 16
    assert socket.sent[1]["d"]["requestType"] == "GetStats"


@pytest.mark.asyncio
async def test_output_events_are_retained_while_waiting_for_request(monkeypatch):
    socket = FakeSocket()
    socket.incoming = [
        json.dumps({"op": 0, "d": {"rpcVersion": 1}}),
        json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}),
        json.dumps({"op": 5, "d": {"eventType": "StreamStateChanged", "eventData": {"outputActive": True}}}),
        json.dumps({"op": 7, "d": {"requestId": "1", "requestStatus": {"result": True}, "responseData": {"ok": True}}}),
    ]

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setattr("stream_doctor.obs.websockets.connect", connect)
    client = ObsClient()
    await client.connect()
    assert await client.request("StartStream") == {"ok": True}
    assert client.events == [{"eventType": "StreamStateChanged", "eventData": {"outputActive": True}}]


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_read_websocket_concurrently(monkeypatch):
    socket = FakeSocket()
    socket.incoming = [
        json.dumps({"op": 0, "d": {"rpcVersion": 1}}),
        json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}),
        json.dumps({"op": 7, "d": {"requestId": "1", "requestStatus": {"result": True}, "responseData": {"one": True}}}),
        json.dumps({"op": 7, "d": {"requestId": "2", "requestStatus": {"result": True}, "responseData": {"two": True}}}),
    ]
    active = False

    async def guarded_recv():
        nonlocal active
        if active:
            raise RuntimeError("concurrent recv")
        active = True
        await asyncio.sleep(0.01)
        value = socket.incoming.pop(0)
        active = False
        return value

    socket.recv = guarded_recv

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setattr("stream_doctor.obs.websockets.connect", connect)
    client = ObsClient()
    await client.connect()
    results = await asyncio.gather(client.request("GetStats"), client.request("GetVersion"))
    assert {tuple(result) for result in results} == {("one",), ("two",)}
