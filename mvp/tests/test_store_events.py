import pytest

from mvp.app.models import new_id


def seed_events(store, run_id, count):
    with store._conn:
        store._conn.executemany(
            "INSERT INTO events(id, run_id, type, ts, payload) VALUES(?,?,?,?,?)",
            [(new_id("evt"), run_id, "tool.result", float(i), '{"i":%d}' % i) for i in range(count)],
        )


def test_iter_events_returns_all_batches_in_sequence(store):
    seed_events(store, "run_a", 2501)
    seed_events(store, "run_b", 3)
    events = list(store.iter_events("run_a", batch=1000))
    assert len(events) == 2501
    assert [event["payload"]["i"] for event in events] == list(range(2501))
    assert len(list(store.iter_events("run_a", after=events[1999]["seq"], batch=100))) == 501


def test_event_iteration_has_a_finite_snapshot(store):
    seed_events(store, "run_a", 3)
    iterator = store.iter_events("run_a", batch=1)
    first = next(iterator)
    seed_events(store, "run_a", 2)
    remaining = list(iterator)
    assert [first["seq"], *[item["seq"] for item in remaining]] == [1, 2, 3]
    assert len(list(store.iter_events("run_a", after=3))) == 2


@pytest.mark.parametrize("batch", [0, -1])
def test_event_iteration_rejects_invalid_batch_size(store, batch):
    with pytest.raises(ValueError):
        list(store.iter_events("run_a", batch=batch))
