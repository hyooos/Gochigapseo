"""분석 명세: 결과가 어떤 자료·기준·계산으로 나왔는지 — 화면과 보고서가 같은 표를 쓰고, 값은 실제 설정과 맞아야 한다."""
from jeju_agent.config import access_rules
from jeju_agent.report.builder import build_markdown
from jeju_agent.report.spec import analysis_spec, fingerprints


def rows(resp, view=None):
    return dict(analysis_spec(resp, view))


def test_course_spec_matches_rules(orch, settings):
    r = orch.ask("휠체어로 서귀포 2일 코스 짜줘 3월에", {})
    d = rows(r, settings.public_view())
    rules = access_rules()
    s = rules["slope"]["wheelchair_manual"]
    assert f"{s['ok']:g}°" in d["판정 기준"] and f"{s['caution'] + rules['slope_block']['margin']:g}°" in d["판정 기준"]
    assert f"× {rules['course']['road_factor']}" in d["계산 방법"]
    assert "[실데이터]" in d["쓴 자료"] and "[가상]" in d["쓴 자료"]            # 센터 날씨는 가상
    v = r.verification
    assert d["검증"].startswith(f"{sum(c.ok for c in v.checks)}/{len(v.checks)}")
    assert "sample" in d["재현 정보"] and all(h in d["재현 정보"] for _, h in fingerprints())
    assert "LLM은 계산하지 않음" in d["LLM 사용"]


def test_status_and_access_have_their_own_method(orch):
    assert "비율" in rows(orch.ask("읍면동별 무장애 관광지 현황", {}))["계산 방법"]
    d = rows(orch.ask("제주시 유아차로 갈 만한 곳", {}))
    assert "판정 기준" in d and "계산 방법" not in d


def test_edits_show_in_spec(orch):
    mem: dict = {}
    orch.ask("휠체어로 서귀포 3일 코스 짜줘", mem)
    orch.ask("둘째 날 빡빡해", mem)
    assert "2일차" in rows(mem["last_response"])["이용자 수정"]


def test_report_has_spec_section_without_secrets(orch, settings):
    r = orch.ask("휠체어로 서귀포 2일 코스 짜줘", {})
    md = build_markdown(r, settings.public_view())
    assert "## 분석 명세" in md and "access_rules.yaml" in md
    assert "sk-" not in md and "LLM_API_KEY" not in md


def test_fingerprints_are_stable():
    assert fingerprints() == fingerprints() and all(len(h) == 8 for _, h in fingerprints())
