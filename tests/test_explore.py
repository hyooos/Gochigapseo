"""데이터 탐색 에이전트: 질문마다 다른 자료 고르기, 뺀 이유, 점검, 센터 안내, 울타리, 검증 연결."""
from jeju_agent.agents.base import AgentContext
from jeju_agent.agents.explore import ExploreAgent, option_problem, rule_options
from jeju_agent.config import catalog
from jeju_agent.contracts import AnalysisRequest, Intent, TravelerProfile
from jeju_agent.data import Repository
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator
from jeju_agent.verify import verify


def explore_of(resp):
    return next(r for r in resp.results if r.agent == "explore")


def test_course_with_month_and_bus_picks_weather_and_stops(orch):
    resp = orch.ask("3월에 버스로 2일 코스 짜줘. 휠체어")
    ex = explore_of(resp)
    assert {"bf_places", "bf_points", "c5_01", "c5_02", "c3_03", "bf_nearby"} <= set(ex.payload["selected"])
    assert {"C3", "C4"} <= {e.id for r in resp.results for e in r.evidence}
    skipped = ex.payload["skip_reasons"]
    assert skipped["c1_07"].startswith("반출 불가") and skipped["c4_10"].startswith("반출 불가")
    assert not set(ex.payload["selected"]) & {k for k, d in catalog()["datasets"].items() if d["export"] == "불가"}


def test_access_with_month_skips_weather_with_reason(orch):
    ex = explore_of(orch.ask("3월에 휠체어로 서귀포 갈 만한 곳"))
    assert "c5_02" not in ex.payload["selected"] and "c3_03" not in ex.payload["selected"]
    assert "코스를 짤 때" in ex.payload["skip_reasons"]["c5_02"]


def test_status_uses_bus_only_when_asked(orch):
    with_bus = orch.ask("제주시 버스 정류소 가까운 관광지 현황")
    plain = orch.ask("제주시 읍면동별 장애인 화장실 현황")
    assert "c3_03" in explore_of(with_bus).payload["selected"] and "S4" in [e.id for e in with_bus.results[-1].evidence]
    assert "c3_03" not in explore_of(plain).payload["selected"] and "S4" not in [e.id for e in plain.results[-1].evidence]
    assert "bf_photos" not in explore_of(plain).payload["selected"]


def test_selection_differs_by_question(orch):
    picks = {q: tuple(explore_of(orch.ask(q)).payload["selected"]) for q in
             ("휠체어로 서귀포 갈 만한 곳", "3월에 버스로 2일 코스", "제주시 읍면동별 장애인 화장실 현황")}
    assert len(set(picks.values())) == 3


def test_center_guide_when_csv_missing(settings, tmp_path):
    o = Orchestrator(settings, repo=Repository("real", center_dir=tmp_path / "none"), llm=LLMClient(settings, runtime_dir=tmp_path))
    resp = o.ask("3월에 2일 코스 짜줘")
    guide = explore_of(resp).payload["center_guide"]
    assert [g["no"] for g in guide] == ["5-01", "5-02"]
    assert "3월" in guide[1]["period"] and any("센터 자료 넣기" in s for s in guide[1]["steps"])
    assert any(w.code == "CENTER_NEEDED" for w in explore_of(resp).warnings)


def test_inspect_reports_numbers_not_rows(orch, repo):
    ex = explore_of(orch.ask("휠체어로 서귀포 갈 만한 곳"))
    place = next(c for c in ex.payload["inspect"] if c["id"] == "bf_places")
    assert place["rows"] == len(repo.places()) and 0 <= place["gaps"]["acc_toilet"] <= 1
    names = set(repo.places()["name"])
    text = str([t.rows for t in ex.evidence]) + str([s.detail for s in ex.trace])
    assert not any(n in text for n in names)        # 자료 값(장소 이름 등)은 D1·D2·기록에 없다


def test_option_guards():
    acc = AnalysisRequest(question="q", intent=Intent.ACCESS)
    course = AnalysisRequest(question="q", intent=Intent.COURSE, days=2)
    status = AnalysisRequest(question="q", intent=Intent.STATUS)
    assert "여행 달" in option_problem(course, {"weather": True})
    assert option_problem(acc, {"bus": True}) and option_problem(status, {"photos": True})
    assert option_problem(course.model_copy(update={"month": 3}), {"weather": True}) is None
    assert rule_options(course.model_copy(update={"profile": TravelerProfile(transport="bus")}))["bus"] is True


def test_tool_check_rejects_and_runs(settings, repo):
    req = AnalysisRequest(question="서귀포 갈 만한 곳", intent=Intent.ACCESS, region="서귀포시")
    tool = ExploreAgent().tools(req, AgentContext(repo=repo, settings=settings))[0]
    assert tool.check({"bus": True})
    assert tool.check({"photos": False})                     # 장소 찾기는 사진을 끌 수 없다(장소 카드에 사진)
    assert tool.check({"nearby": False}) is None
    r = tool.run({"nearby": False})
    assert "bf_nearby" not in r.payload["selected"] and "bf_nearby" in r.payload["skipped"]


def test_verifier_catches_unselected_data(orch):
    resp = orch.ask("3월에 버스로 2일 코스 짜줘")
    tampered = [r.model_copy(deep=True) for r in resp.results]
    ex = next(r for r in tampered if r.agent == "explore")
    ex.payload["selected"] = [k for k in ex.payload["selected"] if k != "c5_02"]
    v = verify(resp.plan, tampered, resp.answer, resp.answer)
    bad = {c.name: c.detail for c in v.checks if not c.ok}
    assert "자료 선택" in bad and "일일 날씨" in bad["자료 선택"]
    assert verify(resp.plan, resp.results, resp.answer, resp.answer).passed is True or resp.verification.passed


def test_d1_labels_each_row(orch):
    """센터 표만 [가상]이어도 데이터허브 행은 [실데이터]로 따로 표시한다."""
    ex = explore_of(orch.ask("3월에 버스로 2일 코스 짜줘"))
    d1 = next(t for t in ex.evidence if t.id == "D1")
    col = d1.columns.index("구분")
    by = {row[0]: row[col] for row in d1.rows}
    assert by["일일 날씨 정보"] == "[가상]" and by["무장애여행정보·무장애여행코스 (관광지·코스 171곳)"] == "[실데이터]"
    assert d1.data_label == "가상"


def test_ui_photos_follow_selection(orch):
    """탐색이 사진을 빼면 화면도 사진을 읽지 않는다(app.photos_on)."""
    from jeju_agent.agents.base import uses
    resp = orch.ask("제주시 읍면동별 장애인 화장실 현황")
    assert uses(resp.plan.request, "bf_photos") is False
    resp2 = orch.ask("휠체어로 서귀포 갈 만한 곳")
    assert uses(resp2.plan.request, "bf_photos") is True


def test_catalog_date_col_is_listed():
    """기준일 열(date_col)은 그 자료의 열 목록(columns)에 있어야 한다(명세서 v13 기준)."""
    from jeju_agent.config import catalog
    for k, d in catalog()["datasets"].items():
        if d.get("date_col"):
            assert d["date_col"] in d["columns"], k


def test_d1_bus_stop_date(orch):
    """버스 정류소(3-03)는 불러온 표의 base_date로 기준일을 채운다('-'가 아님)."""
    ex = explore_of(orch.ask("버스로 다니는 휠체어 2일 코스 짜줘"))
    d1 = next(t for t in ex.evidence if t.id == "D1")
    row = next(r for r in d1.rows if r[0] == "버스 정류소 정보")
    assert row[d1.columns.index("기준일")] == "2025-10-01"


def test_llm_cannot_drop_photos_for_places():
    """API 모드에서 LLM이 코스·장소 질문의 사진을 끄면 울타리가 막는다(장소 카드·동선·일정표가 사진 없이 나오던 문제)."""
    from jeju_agent.agents.explore import option_problem, rule_options
    for intent in (Intent.ACCESS, Intent.COURSE):
        req = AnalysisRequest(question="q", intent=intent, days=2)
        assert option_problem(req, {**rule_options(req), "photos": False})
        assert option_problem(req, rule_options(req)) is None
