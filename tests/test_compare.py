"""조건을 바꾸기 전 ↔ 후 비교(jeju_agent/compare.py)."""
from jeju_agent.compare import compare
from jeju_agent.contracts import TravelerProfile


def _form(orch, mem, **kw):
    form = {"profile": TravelerProfile(), "days": 2, "region": "제주 전체", "intent": "course", "start_date": "2026-11-18", **kw}
    return orch.execute(orch.plan_form("코스", form), mem)


def test_compare_counts_changed_and_course_places(orch):
    mem: dict = {}
    before = _form(orch, mem)
    after = _form(orch, mem, profile=TravelerProfile(mobility="wheelchair_manual"))
    d = compare(before, after)
    assert d["kind"] == "course" and d["same_kind"]
    assert d["cond_removed"] == ["이동 조건 없음"] and "수동 휠체어" in d["cond_added"]
    assert d["count"]["부적합"][1] > d["count"]["부적합"][0]          # 휠체어 기준이 엄격해 부적합이 는다
    assert all(y != x for _, x, y in d["changed"]) and d["changed"]
    it_b = {p["name"] for p in before.results[-1].payload["itinerary"]}
    it_a = {p["name"] for p in after.results[-1].payload["itinerary"]}
    assert set(d["added"]) == it_a - it_b and set(d["removed"]) == it_b - it_a


def test_compare_needs_place_verdicts(orch):
    mem: dict = {}
    course = _form(orch, mem)
    status = orch.ask("제주시 읍면동별 장애인 화장실 현황", {})
    assert compare(course, status) is None
    same = compare(course, course)
    assert not same["added"] and not same["removed"] and not same["changed"] and not same["cond_added"]
