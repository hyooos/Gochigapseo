"""가독성 개선: 화면은 요약 먼저·주의는 따로, 보고서는 같은 말 한 번·긴 근거표는 부록으로(근거 추적은 그대로)."""
import re

from jeju_agent.report.builder import BODY_ROWS, build_markdown
from jeju_agent.report.cards import _shorten, card_markdown, common_questions
from jeju_agent.ui_helpers import pills_html, split_answer


def test_split_answer_moves_caution_and_limit_lines():
    body, notes = split_answer("결론 문장\n- 2일 코스입니다.\n- 주의: 자료가 오래됨\n- 한계: 직선거리 추정")
    assert "주의" not in body and "한계" not in body and "2일 코스" in body
    assert notes == ["주의: 자료가 오래됨", "한계: 직선거리 추정"]


def test_pills_do_not_rely_on_color_alone():
    html = pills_html([{"key": "acc_toilet", "label": "장애인 화장실", "mark": "✓", "needed": True},
                       {"key": "elevator", "label": "엘리베이터", "mark": "?"}])
    assert "✓" in html and "?" in html                      # 기호
    assert "aria-label='화장실 있음 (꼭 필요)'" in html        # 화면 낭독기
    assert "pill ok need" in html and "pill unk" in html


def test_pills_escape_labels():
    assert "<b>" not in pills_html([{"key": "x", "label": "<b>", "mark": "✗"}])


def test_shorten_keeps_quote_start_and_marks_cut():
    s = _shorten("✓ 경사로 있음 — 원문: “" + "가" * 100 + "”")
    assert s.endswith("…”") and "가" * 70 + "…" in s            # 원문 앞 70자 + 줄임표, 따옴표는 닫는다
    long = _shorten("다" * 200)
    assert long.endswith("…") and len(long) == 121


def test_common_questions_printed_once(orch, settings):
    r = orch.ask("휠체어로 서귀포 2일 코스 짜줘", {})
    md = build_markdown(r, settings.public_view())
    cards = [c for x in r.results for c in x.payload.get("cards", [])]
    common = common_questions(cards)
    if len(cards) > 1 and common:
        assert md.count(common[0]) == 1                      # 장소마다 반복하지 않는다
        assert all(common[0] not in card_markdown(c, common=common) for c in cards)


def test_long_evidence_goes_to_appendix_but_stays_traceable(orch, settings):
    r = orch.ask("제주 전체 2일 여행 코스", {})
    md = build_markdown(r, settings.public_view())
    long = [t for x in r.results for t in x.evidence if len(t.rows) > BODY_ROWS + 2]
    assert long, "테스트 전제: 행이 많은 근거표가 있어야 함"
    body, appendix = md.split("## 부록 — 근거표 전체")
    for t in long:
        assert f"[근거표 {t.id}]" in body and f"외 {len(t.rows) - BODY_ROWS}행" in body
        sec = appendix.split(f"[근거표 {t.id}]", 1)[1].split("### ", 1)[0]
        assert len(re.findall(r"^\| ", sec, flags=re.M)) == len(t.rows) + 1    # 머리 1줄 + 전체 행
    assert md.rstrip().splitlines()[-1].startswith("본 자료는") or "제주빅데이터센터" in md.split("---")[-1]


def test_short_report_has_no_appendix(orch, settings):
    r = orch.ask("1100고지습지 휠체어 화장실 있어?", {})
    md = build_markdown(r, settings.public_view())
    if all(len(t.rows) <= BODY_ROWS + 2 for x in r.results for t in x.evidence):
        assert "부록 — 근거표 전체" not in md


def test_map_numbers_merge_when_stops_overlap():
    from jeju_agent.ui_helpers import merged_numbers
    rows = [{"lat": 33.30, "lng": 126.50, "num": "1"}, {"lat": 33.40, "lng": 126.60, "num": "2"},
            {"lat": 33.45, "lng": 126.62, "num": "3"}, {"lat": 33.4505, "lng": 126.6205, "num": "4"}]
    assert merged_numbers(rows) == ["1", "2", "3,4", ""]      # 겹치는 번호는 한 곳에 모아 쓴다


def test_detail_table_folds_long_text_and_key_check_chips():
    from jeju_agent.ui_helpers import keycheck_html, kv_html
    long = "가" * 120
    h = kv_html([("추천 동선", long), ("주차 위치", "<정문>")], fold=70)
    assert "<details>" in h and "전체 보기" in h and long in h and "&lt;정문&gt;" in h
    assert "<details>" not in kv_html([("짧음", "가" * 10)])
    kc = {"title": "t", "need_call": True, "items": [{"key": "slope", "label": "경사", "state": "unknown", "text": "기록 없음"},
                                                      {"key": "stairs", "label": "계단", "state": "ok", "text": "기록된 곳 없음"}]}
    h = keycheck_html(kc)
    assert "? 경사" in h and "✓ 계단" in h and "방문 전 추가 확인 필요" in h and "aria-label='경사 정보 없음'" in h
    assert keycheck_html(None) == ""
