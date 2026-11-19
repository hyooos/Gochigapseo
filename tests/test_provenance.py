"""출처 문구·[가상] 표시·PNG 전용·보고서 구성·차트."""
import pytest

from jeju_agent.contracts import EvidenceTable
from jeju_agent.provenance import SOURCE_NOTICE
from jeju_agent.report.builder import build_markdown, checklist, export_request_draft
from jeju_agent.report.charts import course_map, evidence_chart, report_charts, save_png, verdict_chart


def test_notice_text_is_exact():
    assert SOURCE_NOTICE == ("본 자료는 제주빅데이터센터에서 제공하는 자료를 활용한 것이며, "
                             "연구결과는 제주특별자치도 및 제주테크노파크와 관련이 없음을 밝힙니다.")


@pytest.mark.parametrize("q", ["휠체어로 서귀포 갈 만한 곳", "유모차로 2일 코스 짜줘 7월 버스로", "지역별 무장애 관광지 현황"])
def test_report_has_notice_label_and_sources(orch, q):
    resp = orch.ask(q)
    md = build_markdown(resp)
    assert SOURCE_NOTICE in md and "[가상]" in md and "## 사용한 자료" in md


def test_checklist_follows_profile(orch):
    resp = orch.ask("휠체어로 버스 타고 2일 코스 짜줘")
    items = checklist(resp)
    assert any("휠체어 대여" in x for x in items) and any("저상버스" in x for x in items)
    assert "## 방문 전 확인 목록" in build_markdown(resp)


def test_export_draft_only_when_center_data_used(orch):
    assert export_request_draft(orch.ask("휠체어로 갈 만한 곳")) is None
    resp = orch.ask("유모차로 1일 코스 7월 버스로")
    # 가상 센터 자료는 '[가상] 센터 …' 출처라 반출 신청 대상이 아니다(실제 센터 CSV를 넣었을 때만 초안)
    assert export_request_draft(resp) is None


def test_only_png_or_jpg_export(tmp_path):
    import matplotlib.pyplot as plt

    fig, _ = plt.subplots()
    with pytest.raises(ValueError):
        save_png(fig, tmp_path / "x.svg")
    fig2, _ = plt.subplots()
    assert save_png(fig2, tmp_path / "x.png").exists()


def _table(columns, rows):
    return EvidenceTable(id="T", title="표", dataset="d", data_label="가상", columns=columns, rows=rows)


def test_evidence_chart_picks_ratio_or_given_column():
    t = _table(["지역", "관광지 수", "확인 비율"], [["A", 4, 0.5], ["B", 3, 0.33]])
    assert evidence_chart(t).axes[0].get_xlabel() == "확인 비율"
    assert evidence_chart(t, column="관광지 수").axes[0].get_xlabel() == "관광지 수"
    assert evidence_chart(_table(["지역", "값"], [["A", 0], ["B", 0]])) is None


def test_verdict_chart_uses_symbols_not_only_color():
    fig = verdict_chart({"적합": 3, "주의": 2, "부적합": 1}, "판정", cvd=True)
    labels = [t.get_text() for t in fig.axes[0].get_yticklabels()]
    assert any("○" in x for x in labels) and any("×" in x for x in labels)
    assert verdict_chart({}, "빈 표") is None


def test_course_map_and_report_charts(orch):
    resp = orch.ask("2일 코스 짜줘")
    assert course_map(resp.results[-1].payload["itinerary"], "코스") is not None
    names = [n for n, _ in report_charts(resp)]
    assert "course_map" in names
    assert [n for n, _ in report_charts(orch.ask("지역별 무장애 관광지 현황"))] == ["toilet_ratio", "info_gap"]
