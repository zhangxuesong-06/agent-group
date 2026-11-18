import asyncio
import json

import pytest

from mvp.app import main
from mvp.app.bus import EventBus
from mvp.app.models import Budget, RunState
from test_store_events import seed_events


@pytest.fixture()
def event_store(store, monkeypatch):
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "bus", EventBus(store))
    store.create_run(RunState(id="run_events", goal="events", status="succeeded", budget=Budget()))
    return store


def data_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and '"seq"' in line]


def get_stream(client, suffix="", **headers):
    return client.get(
        "/api/runs/run_events/events" + suffix,
        headers={"Authorization": "Bearer test-token", **headers},
    )


def test_sse_replays_more_than_2000_events(client, event_store):
    seed_events(event_store, "run_events", 2501)
    response = get_stream(client)
    assert response.status_code == 200
    events = data_events(response.text)
    assert [item["payload"]["i"] for item in events] == list(range(2501))
    assert response.text.endswith("event: end\ndata: {}\n\n")


def test_sse_does_not_end_at_old_terminal_event(client, event_store):
    seed_events(event_store, "run_events", 3)
    with event_store._conn:
        event_store._conn.execute("UPDATE events SET type='run.completed' WHERE seq=1")
    assert [item["seq"] for item in data_events(get_stream(client).text)] == [1, 2, 3]


@pytest.mark.parametrize("after, header, expected", [(0, "2", [3, 4]), (1, "3", [4]), (3, "1", [4]), (4, "4", [])])
def test_sse_resumes_at_latest_cursor(client, event_store, after, header, expected):
    seed_events(event_store, "run_events", 4)
    response = get_stream(client, f"?after={after}", **{"Last-Event-ID": header})
    assert response.status_code == 200
    assert [item["seq"] for item in data_events(response.text)] == expected


@pytest.mark.parametrize("header", ["abc", "-1", "1.5", str(2**63)])
def test_sse_rejects_invalid_cursor(client, event_store, header):
    assert get_stream(client, **{"Last-Event-ID": header}).status_code == 422


def test_sse_streams_transient_delta_without_rereading_history(event_store, monkeypatch):
    async def scenario():
        with event_store._conn:
            event_store._conn.execute("UPDATE runs SET status='running' WHERE id='run_events'")
        response = await main.stream_events("run_events", after=0)
        iterator = response.body_iterator
        try:
            assert (await anext(iterator)).startswith("event: snapshot")
            assert (await anext(iterator)).startswith("event: live")

            def fail(*args, **kwargs):
                raise AssertionError("transient delta must not load the full run")

            monkeypatch.setattr(event_store, "get_run", fail)
            await main.bus.publish("run_events", "model.delta", {"text": "x"}, transient=True)
            chunk = await asyncio.wait_for(anext(iterator), timeout=2)
            payload = json.loads(chunk[6:])
            assert payload["transient"] is True
            assert payload["payload"] == {"text": "x"}
        finally:
            await iterator.aclose()

    asyncio.run(scenario())


def test_sse_live_catches_up_after_queue_overflow(event_store):
    async def scenario():
        with event_store._conn:
            event_store._conn.execute("UPDATE runs SET status='running' WHERE id='run_events'")
        response = await main.stream_events("run_events", after=0)
        iterator = response.body_iterator
        try:
            assert (await anext(iterator)).startswith("event: snapshot")
            assert (await anext(iterator)).startswith("event: live")
            for i in range(1005):
                await main.bus.publish("run_events", "tool.result", {"i": i})
            text = ""
            for _ in range(1005):
                text += await asyncio.wait_for(anext(iterator), timeout=2)
            assert [item["payload"]["i"] for item in data_events(text)] == list(range(1005))
        finally:
            await iterator.aclose()
        assert not main.bus._subs.get("run_events")

    asyncio.run(scenario())
