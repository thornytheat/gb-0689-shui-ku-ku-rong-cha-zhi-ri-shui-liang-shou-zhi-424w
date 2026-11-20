from decimal import Decimal as D
from app.pipeline import run
from app import store
from app.config import SAMPLES
from app.errors import DomainError


def test_rerun_same_batch_adds_no_ledger(raw, isolate):
    first = run(raw)
    history = store.history()
    second = run(raw)
    assert first["manifest"]["run_id"] == second["manifest"]["run_id"]
    assert len(store.history()) == len(history) == 1
    assert len(first["budgets"]) == len(second["budgets"])


def test_rerun_same_batch_keeps_review_version(raw, isolate):
    first = run(raw)
    run_id = first["manifest"]["run_id"]
    store.review(run_id, first["version"], "通过")
    again = run(raw)
    assert again["manifest"]["run_id"] == run_id
    assert again["version"] == 2
    assert again["review"] == "通过"


def test_failed_regeneration_keeps_old_version(raw, isolate):
    saved = run(raw)
    run_id = saved["manifest"]["run_id"]
    store.review(run_id, saved["version"], "通过")
    before = store.fetch(run_id)
    # 同一批内容、非法参数：计算在保存前失败，旧版必须原样保留。
    try:
        run(raw, {"max_gap_hours": "-1"})
    except DomainError:
        pass
    else:
        raise AssertionError("expected DomainError")
    after = store.fetch(run_id)
    assert after["version"] == before["version"] == 2
    assert after["review"] == before["review"] == "通过"
    assert after["manifest"]["run_id"] == run_id


def test_failed_new_batch_saves_nothing(raw, isolate):
    run(raw)
    count = len(store.history())
    for params in ({"max_gap_hours": "0"}, {"tolerance_m3": "-1"}, {"spike_m": "0"},
                   {"curve": [["100", "0"]]}):
        try:
            run(raw, params)
        except DomainError:
            continue
        raise AssertionError("expected DomainError for %r" % (params,))
    assert len(store.history()) == count


def test_two_station_batch_idempotent(isolate):
    raw = (SAMPLES / "two_station.csv").read_bytes()
    a = run(raw)
    b = run(raw)
    assert a["manifest"]["run_id"] == b["manifest"]["run_id"]
    assert len(store.history()) == 1
    assert a["budgets"] == b["budgets"]
