import asyncio
import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from mvp.app import main
from mvp.app.devices import DeviceHub, hash_worker_token, new_worker_token


class FakeWebSocket:
    def __init__(self):
        self.closed = None
        self.sent = []
        self.fail_close = False

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)
        if self.fail_close:
            raise RuntimeError("already closed")

    async def send_json(self, payload):
        self.sent.append(payload)


def test_detach_closes_socket_and_clears_state():
    async def scenario():
        hub = DeviceHub()
        ws = FakeWebSocket()
        await hub.attach("dev_test", ws)
        await hub.detach("dev_test", ws, close=True)
        assert ws.closed[0] == 4401
        assert not hub.online("dev_test")
        assert not hub._online_events["dev_test"].is_set()

    asyncio.run(scenario())


def test_detach_ignores_stale_socket_and_close_errors():
    async def scenario():
        hub = DeviceHub()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()
        await hub.attach("dev_test", ws1)
        await hub.attach("dev_test", ws2)
        assert ws1.closed[0] == 4000
        ws2.fail_close = True
        await hub.detach("dev_test", ws1)
        assert hub.online("dev_test")
        await hub.detach("dev_test", ws2, close=True)
        assert not hub.online("dev_test")

    asyncio.run(scenario())


@pytest.fixture()
def paired_device(store, monkeypatch):
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "device_hub", DeviceHub())
    token = new_worker_token()
    device = store.create_device("laptop", hash_worker_token(token))
    return device, token


def login(client):
    response = client.post("/api/auth/login", json={"token": "test-token"})
    assert response.status_code == 200


def test_delete_device_closes_worker_and_removes_it(client, paired_device):
    device, token = paired_device
    login(client)
    with client.websocket_connect(f"/api/worker/ws?token={token}") as ws:
        ws.send_json({"type": "hello", "workspaces": ["C:/work"]})
        assert ws.receive_json()["type"] == "welcome"
        response = client.delete(
            f"/api/devices/{device['id']}", headers={"x-requested-with": "XMLHttpRequest"}
        )
        assert response.status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert not main.device_hub.online(device["id"])


def test_worker_survives_malformed_and_non_object_frames(client, paired_device):
    device, token = paired_device
    with client.websocket_connect(f"/api/worker/ws?token={token}") as ws:
        ws.send_json({"type": "hello", "workspaces": ["C:/work"]})
        assert ws.receive_json()["type"] == "welcome"
        for bad in ("{not json", "[]", '"text"', "null", "42"):
            ws.send_text(bad)
        ws.send_bytes(b"\x00\x01binary")
        ws.send_text(json.dumps({"type": "heartbeat"}))
        assert ws.receive_json()["type"] == "pong"


@pytest.mark.parametrize("workspaces", [5, "C:/work", {"path": "C:/work"}, None])
def test_worker_hello_tolerates_non_list_workspaces(client, paired_device, workspaces):
    device, token = paired_device
    with client.websocket_connect(f"/api/worker/ws?token={token}") as ws:
        ws.send_json({"type": "hello", "workspaces": workspaces})
        assert ws.receive_json()["type"] == "welcome"
    assert main.store.get_device(device["id"])["workspaces"] == []


def test_worker_hello_must_be_an_object(client, paired_device):
    device, token = paired_device
    with client.websocket_connect(f"/api/worker/ws?token={token}") as ws:
        ws.send_text("[]")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_device_row_is_deleted_before_socket_close(client, paired_device, monkeypatch):
    device, _ = paired_device
    login(client)
    seen = {}

    async def detach(device_id, *args, **kwargs):
        seen["row"] = main.store.get_device(device_id)

    monkeypatch.setattr(main.device_hub, "detach", detach)
    response = client.delete(
        f"/api/devices/{device['id']}", headers={"x-requested-with": "XMLHttpRequest"}
    )
    assert response.status_code == 200
    assert seen["row"] is None


def test_worker_heartbeat_marks_device_online(client, paired_device):
    device, token = paired_device
    login(client)
    before = time.time()
    with client.websocket_connect(f"/api/worker/ws?token={token}") as ws:
        ws.send_json({"type": "hello", "workspaces": ["C:/work"]})
        ws.receive_json()
        ws.send_json({"type": "heartbeat"})
        assert ws.receive_json()["type"] == "pong"
        assert client.get("/api/devices").json()["devices"][0]["online"] is True
    assert main.store.get_device(device["id"])["last_seen"] >= before
