import sqlite3

import pytest

from mvp.app.models import Budget, RunState, TaskState
from test_store_events import seed_events


def make_run(store, run_id="run_json"):
    store.create_run(RunState(id=run_id, goal="test", status="failed", budget=Budget()))
    store.create_tasks([TaskState(id="task_" + run_id, run_id=run_id, order=0, title="task", instruction="test", capability="general")])
    store.add_message(run_id, "user", "keep me")
    seed_events(store, run_id, 1)


@pytest.mark.parametrize("column", ["budget", "usage", "cluster"])
@pytest.mark.parametrize("raw", ["{broken-json", "null", "[]", '"text"'])
def test_bad_run_json_does_not_break_list_or_detail(store, caplog, column, raw):
    make_run(store)
    with store._conn:
        store._conn.execute(f"UPDATE runs SET {column}=? WHERE id='run_json'", (raw,))
    assert store.list_runs()[0][column] == {}
    assert store.get_run("run_json")[column] == {}
    assert f"runs.{column}" in caplog.text
    assert raw not in caplog.text


@pytest.mark.parametrize("raw", ["broken", "null", "{}"])
def test_bad_task_dependencies_and_checkpoint_have_safe_shapes(store, raw):
    make_run(store)
    with store._conn:
        store._conn.execute("UPDATE tasks SET depends_on=?, checkpoint='[]'", (raw,))
    task = store.get_run("run_json", include_checkpoint=True)["tasks"][0]
    assert task["depends_on"] == []
    assert task["checkpoint"] is None


@pytest.mark.parametrize("raw", ["{broken", "null", "[]"])
def test_bad_event_payload_does_not_drop_event_or_sequence(store, raw):
    seed_events(store, "run_json", 3)
    with store._conn:
        store._conn.execute("UPDATE events SET payload=? WHERE seq=2", (raw,))
    events = list(store.iter_events("run_json", batch=1))
    assert [item["seq"] for item in events] == [1, 2, 3]
    assert [item["payload"] for item in events] == [{"i": 0}, {}, {"i": 2}]


@pytest.mark.parametrize("column", ["scores", "results", "errors", "usage"])
@pytest.mark.parametrize("raw", ["{broken", "null", "[]"])
def test_bad_evaluation_json_does_not_break_history(store, column, raw):
    store.save_evaluation({"id": "eval_test", "provider_id": "p_test"})
    with store._conn:
        store._conn.execute(f"UPDATE evaluations SET {column}=?", (raw,))
    assert store.get_evaluation("eval_test")[column] == {}
    assert store.list_evaluations()[0][column] == {}


def test_bad_message_metadata_is_an_object(store):
    make_run(store)
    with store._conn:
        store._conn.execute("UPDATE messages SET meta='[]'")
    assert store.list_messages("run_json")[0]["meta"] == {}


def test_valid_json_is_preserved(store):
    make_run(store)
    with store._conn:
        store._conn.execute("UPDATE runs SET cluster=?", ('{"provider_ids":["p1"]}',))
        store._conn.execute("UPDATE tasks SET depends_on=?, checkpoint=?", ('["task0"]', '{"messages":[]}'))
    result = store.get_run("run_json", include_checkpoint=True)
    assert result["cluster"] == {"provider_ids": ["p1"]}
    assert result["tasks"][0]["depends_on"] == ["task0"]
    assert result["tasks"][0]["checkpoint"] == {"messages": []}


def test_delete_run_removes_only_its_children(store):
    make_run(store)
    make_run(store, "run_other")
    assert store.delete_run("run_json") is True
    assert store.get_run("run_json") is None
    assert store.list_messages("run_json") == []
    assert store.get_events("run_json") == []
    assert store._conn.execute("SELECT COUNT(*) FROM tasks WHERE run_id='run_json'").fetchone()[0] == 0
    other = store.get_run("run_other")
    assert len(other["tasks"]) == len(other["messages"]) == 1
    assert len(store.get_events("run_other")) == 1
    assert store.delete_run("run_json") is False


def test_delete_run_rolls_back_all_deletes_on_failure(store):
    make_run(store)
    store._conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        store.delete_run("run_json")
    assert not store._conn.in_transaction
    run = store.get_run("run_json")
    assert run is not None
    assert len(run["tasks"]) == len(run["messages"]) == 1
    assert len(store.get_events("run_json")) == 1
