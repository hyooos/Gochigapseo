"""무장애 관광 현황(기관용) 집계."""
import pandas as pd

from jeju_agent.contracts import AnalysisRequest, Intent


def summarize(orch, **kw):
    req = AnalysisRequest(question=kw.pop("question", "현황"), intent=Intent.STATUS, **kw)
    return orch._call_agent("status", req, {})


def test_totals_match_places(orch, repo):
    r = summarize(orch)
    s1 = next(e for e in r.evidence if e.id == "S1")
    assert s1.columns[0] == "시" and sum(row[1] for row in s1.rows) == len(repo.places())
    toilets = int((pd.to_numeric(repo.places()["acc_toilet"], errors="coerce") == 1).sum())
    assert sum(row[2] for row in s1.rows) == toilets
    s2 = next(e for e in r.evidence if e.id == "S2")
    for row in s2.rows:   # 있음 + 없음 + 정보 없음 = 전체
        assert row[1] + row[2] + row[3] == len(repo.places())


def test_default_grouping_follows_region(orch):
    assert summarize(orch, region="제주시").payload["group_by"] == "emd"
    assert summarize(orch, region="애월읍").payload["group_by"] == "category"


def test_focus_sorts_by_lowest_ratio(orch):
    r = summarize(orch, region="제주시", params={"opts": {"focus": "toilet"}})
    rows = next(e for e in r.evidence if e.id == "S1").rows
    ratios = [row[2] / row[1] for row in rows]
    assert ratios == sorted(ratios)


def test_bus_access_table_when_center_data(orch):
    r = summarize(orch)
    assert "S4" in [e.id for e in r.evidence] and "가상" in r.data_labels


def test_priority_only_below_overall(orch):
    r = summarize(orch, region="제주시")
    groups = pd.DataFrame(r.payload["groups"])
    for g in r.payload["priority_groups"]:
        row = groups[groups["group"] == g].iloc[0]
        assert row["n"] >= 3
