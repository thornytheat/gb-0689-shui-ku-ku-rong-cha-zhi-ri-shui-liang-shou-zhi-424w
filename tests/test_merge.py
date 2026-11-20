import pytest
from decimal import Decimal as D
from datetime import timedelta
from app.merge import classify_station
from app.normalize import instant
from app.pipeline import run
from app.config import SAMPLES


def point(station, time, level, inflow='2.0', outflow='1.5', role=None, reservoir=None, line=2, id=None):
    row = {"id": id or station + str(line), "station": station, "time": instant(time),
           "level": None if level is None else D(level), "inflow": None if inflow is None else D(inflow),
           "outflow": None if outflow is None else D(outflow), "flow_unit": "m3/s",
           "version": 1, "_line": line}
    if role:
        row["role"] = role
    if reservoir:
        row["reservoir"] = reservoir
    return row


def test_station_suffixes():
    assert classify_station("青岚库") == (None, "main")
    assert classify_station("青岚库备站") == ("青岚库", "backup")
    assert classify_station("青岚库（备）") == ("青岚库", "backup")
    assert classify_station("青岚库副站") == ("青岚库", "backup")
    assert classify_station("青岚库主站") == ("青岚库", "main")


def test_station_override_maps_to_reservoir():
    assert classify_station("站A", {"站A": "备站"}) == (None, "backup")
    assert classify_station("站A", {"站A": "青岚库/backup"}) == ("青岚库", "backup")
    assert classify_station("站A", {"站A": {"reservoir": "青岚库", "role": "backup"}}) == ("青岚库", "backup")


def test_two_station_sample_forms_one_reservoir_day():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    assert list(result["stations"]) == ["青岚库"]
    assert len({b["reservoir"] for b in result["budgets"]}) == 1


def test_wind_bias_recovered_not_booked_as_flow():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    info = result["stations"]["青岚库"]
    # 备站固定低 0.03 m，应被识别为风壅偏差而不是入库水量。
    assert D(info["wind_bias_m"]) == D("0.03")
    assert info["bias_samples"] > 0


def test_bubbles_isolated():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    spikes = [q for q in result["quarantine"] if q["code"] == "SPIKE"]
    assert len(spikes) == 2
    assert result["stations"]["青岚库"]["spikes"] == 2


def test_main_outage_spliced_with_backup():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    spliced = [s for s in result["levels"] if s["level_source"] == "备站"]
    assert len(spliced) == 3
    for row in spliced:
        basis = row["level_basis"]
        assert basis["method"] == "anchor_ramp"
        assert basis["anchor_start_line"] is not None and basis["anchor_end_line"] is not None


def test_spliced_level_matches_main_anchors():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    storage = {s["time"]: s for s in result["storage"]}
    # 主站锚点水位原样采信，接续点与锚点之间库容连续。
    for row in result["storage"]:
        if row["level_source"] == "主站":
            assert row["level"] == row["level_raw"]
    ordered = sorted(result["storage"], key=lambda s: s["time"])
    for i, row in enumerate(ordered):
        if row["level_source"] != "备站":
            continue
        # 接续段两端必须是主站锚点，接续水位落在这两个锚点之间。
        start = next(s for s in reversed(ordered[:i]) if s["level_source"] != "备站"
                     and s["time"] <= row["time"])
        end = next(s for s in ordered[i + 1:] if s["level_source"] != "备站")
        lo, hi = sorted([start["level"], end["level"]])
        assert lo <= row["level"] <= hi
        assert row["level_basis"]["anchor_start"] <= row["time"] <= row["level_basis"]["anchor_end"]


def test_daily_basis_is_traceable():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    day = result["budgets"][0]
    assert D(day["level_sources"]["备站"]) > 0
    assert day["flow_sources"] == {"主站": day["coverage_hours"]}
    assert day["reservoir"] == "青岚库"
    backup_segments = [s for s in day["segments"] if s["level_source"] == "备站"]
    assert backup_segments
    for seg in backup_segments:
        assert seg["flow_source"] == "主站"
        assert any(end["level_source"] == "备站" for end in seg["basis"])
        assert all("anchor_start_line" in end for end in seg["basis"])


def test_budget_yields_one_row_per_reservoir_day():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    keys = [(b["reservoir"], b["date"]) for b in result["budgets"]]
    assert len(keys) == len(set(keys))


def test_segments_chain_across_day_boundary():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    segments = [s for b in result["budgets"] for s in b["segments"]]
    segments.sort(key=lambda s: s["start"])
    for left, right in zip(segments, segments[1:]):
        assert left["end"] == right["start"]


def test_unmarked_level_only_station_attaches_to_single_main():
    raw = """id,station,time,level,inflow,outflow,flow_unit,version
1,A站,2026-11-16T00:00:00+08:00,101.0,2,1.5,m3/s,1
2,A站,2026-11-16T01:00:00+08:00,,2,1.5,m3/s,1
3,A站,2026-11-16T02:00:00+08:00,101.02,2,1.5,m3/s,1
4,B站,2026-11-16T00:00:00+08:00,100.97,,,m3/s,1
5,B站,2026-11-16T01:00:00+08:00,100.98,,,m3/s,1
6,B站,2026-11-16T02:00:00+08:00,100.99,,,m3/s,1
""".encode()
    result = run(raw, None, persist=False)
    assert result["stations"]["A站"]["backup_stations"] == ["B站"]
    assert D(result["stations"]["A站"]["wind_bias_m"]) == D("0.03")
    day = result["budgets"][0]
    # 接续点相邻的两个小时段都标注备站来源，便于交班回查。
    assert D(day["level_sources"].get("备站", "0")) == D("2.0")
    assert not [q for q in result["quarantine"] if q["key"] == "2"]


def test_both_stations_missing_level_is_quarantined():
    raw = """id,station,time,level,inflow,outflow,flow_unit,version
1,A站,2026-11-16T00:00:00+08:00,101.0,2,1.5,m3/s,1
2,A站,2026-11-16T01:00:00+08:00,,2,1.5,m3/s,1
3,A站,2026-11-16T02:00:00+08:00,101.02,2,1.5,m3/s,1
4,A站备站,2026-11-16T03:00:00+08:00,101.03,,,m3/s,1
""".encode()
    result = run(raw, None, persist=False)
    codes = {(q["key"], q["code"]) for q in result["quarantine"]}
    assert ("2", "INCOMPLETE") in codes
    assert result["audit"]["input"] == result["audit"]["retained"] + result["audit"]["isolated"]
    assert all(s["time"] != instant("2026-11-16T01:00:00+08:00") for s in result["levels"])


def test_two_main_stations_same_instant_conflict():
    raw = """id,station,time,level,inflow,outflow,flow_unit,version
1,A站主站,2026-11-16T00:00:00+08:00,101.0,2,1.5,m3/s,1
2,二号主站,2026-11-16T00:00:00+08:00,101.5,2,1.5,m3/s,1
3,A站主站,2026-11-16T01:00:00+08:00,101.01,2,1.5,m3/s,1
4,A站主站,2026-11-16T02:00:00+08:00,101.02,2,1.5,m3/s,1
""".encode()
    params = {"station_roles": {"A站主站": "A站/main", "二号主站": "A站/main"}}
    result = run(raw, params, persist=False)
    conflicts = [q for q in result["quarantine"] if q["code"] == "CONFLICT"]
    assert len(conflicts) == 2
    assert result["audit"]["input"] == result["audit"]["retained"] + result["audit"]["isolated"]


def test_no_flow_station_is_not_integrated():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    assert max(len(result["flows"]), 0) > 0
    for part in result["flows"]:
        assert part["reservoir"] == "青岚库"
        assert part["left"]["flow_source"] == "主站"


def test_single_station_behavior_unchanged(raw):
    result = run(raw, None, persist=False)
    assert list(result["stations"]) == ["青岚库"]
    assert result["stations"]["青岚库"]["backup_stations"] == []
    assert len(result["budgets"]) == 2


def test_handover_export_has_one_ledger_per_reservoir_day(isolate):
    import tempfile, os
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
    from app.main import create_app
    client = create_app().test_client()
    raw = (SAMPLES / "two_station.csv").read_text(encoding="utf-8")
    saved = client.post("/api/runs", json={"text": raw}).json
    response = client.get("/api/handover.csv?run=" + saved["manifest"]["run_id"])
    assert response.status_code == 200
    lines = [line for line in response.get_data(as_text=True).splitlines() if line.strip()]
    header = lines[0]
    assert "水库" in header and "段依据" in header
    rows = lines[1:]
    # 每库每日的账头只出现一次，段按时间逐段列出且来源于账头一致。
    ledger = {row.split(",")[1] + row.split(",")[2] for row in rows}
    assert ledger == {"青岚库2026-11-16", "青岚库2026-11-17"}
    assert all("锚点行" in row or "direct" in row for row in rows)


def test_storage_change_not_inflated_by_wind_bias():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    # 主站水位每天稳定上升 0.01 m/h，曲线 100→102 m 对应 0→30000 m³，
    # 即每 0.01 m 对应 150 m³。日 16 覆盖 24h，库容变化应为 24×150=3600。
    day = next(b for b in result["budgets"] if b["date"] == "2026-11-16")
    assert D(day["storage_change"]) == D("3600")
    # 若直接把备站（低 0.03 m）并进来，会在换源处凭空多出约 450 m³。
    assert D(day["storage_change"]) != D("3600") + D("0.03") * D("15000")


def test_levels_are_sorted_and_unique_per_reservoir():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    keys = [(row["reservoir"], row["time"]) for row in result["levels"]]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_backup_rows_never_carry_flow():
    result = run((SAMPLES / "two_station.csv").read_bytes(), None, persist=False)
    for row in result["records"]:
        if row["role"] == "backup" and row["level_source"] == "备站":
            assert row.get("flow_source") in (None, "缺测")
    for part in result["flows"]:
        assert part["station"] == "青岚库"
