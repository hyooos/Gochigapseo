"""LLM 설명 숫자 대조: tool 경로는 LLM이 받은 근거표 숫자까지 기준으로 보고, 거부 이유를 기록에 남긴다."""
import json
import re

from test_tool_calling_review import _fake_llm

from jeju_agent.contracts import AnalysisRequest, Intent
from jeju_agent.orchestrator import Orchestrator, answer_problem, cited_evidence, numbers_consistent, unknown_numbers


def _evidence_numbers(messages) -> list[str]:
    """LLM이 tool 결과로 받은 근거표 행의 숫자들."""
    tool_msg = next(m for m in messages if m.get("role") == "tool")
    out = json.loads(tool_msg["content"])
    cells = [str(c) for e in out["evidence"] for row in e["rows"] for c in row if c is not None]
    return re.findall(r"\d+(?:\.\d+)?", " ".join(cells))


def _run(api_settings, repo, tmp_path, make_text):
    s, llm, _ = _fake_llm(api_settings, tmp_path, '{"region": "제주시"}')
    seen = {}

    def fake_text(messages, model):
        seen["nums"] = _evidence_numbers(messages)
        return make_text(seen["nums"]), 100, 10

    llm._openai_call = fake_text
    o = Orchestrator(s, repo=repo, llm=llm)
    plan = o._make_plan(AnalysisRequest(question="제주시 갈 만한 곳", intent=Intent.ACCESS, region="제주시"), "rule")
    return o, plan, o.execute(plan), seen


def test_tool_answer_may_cite_evidence_table_numbers(api_settings, repo, tmp_path):
    """근거표 숫자는 코드가 계산한 값이므로, 요약에 없더라도 인용하면 채택한다."""
    o, plan, resp, seen = _run(api_settings, repo, tmp_path, lambda nums: "근거표 값: " + ", ".join(nums) + " [근거표 A1]")
    draft = o._template_answer(plan, resp.results)
    assert unknown_numbers(", ".join(seen["nums"]), draft), "테스트 전제: 요약 초안에 없는 근거표 숫자가 있어야 함"
    assert resp.answer_source == "llm"


def test_tool_answer_with_new_number_falls_back_and_records_it(api_settings, repo, tmp_path):
    """근거 어디에도 없는 숫자를 만들면 문장틀로 돌아가고, 걸린 숫자를 기록에 남긴다."""
    _, _, resp, _ = _run(api_settings, repo, tmp_path, lambda nums: "대기시간이 987654분 줄어듭니다.")
    assert resp.answer_source == "template"
    detail = next(t.detail for t in resp.trace if t.action == "tool_answer")
    assert "987654" in detail


def test_number_notation_differences_are_equal():
    """'2026-04'의 04와 '4월'의 4, 0.50과 0.5는 같은 숫자다."""
    assert numbers_consistent("2026년 4월부터", "공백 시작 2026-04")
    assert numbers_consistent("비율 0.5", "비율 0.50")
    assert not numbers_consistent("1곳", "비율 1.0")          # 소수와 정수는 구분
    assert not numbers_consistent("비율 1.0", "관광지 1곳")


def test_ratio_may_be_written_as_percent():
    """0~1 비율은 백분율로 풀어 써도 같은 숫자로 본다. 다른 백분율이나 정수의 백분율 변환은 막는다."""
    assert numbers_consistent("장애인 화장실 확인 비율은 97%입니다.", "장애인 화장실 확인 비율은 0.97입니다.")
    assert numbers_consistent("절반(50%)", "비율 0.5")
    assert not numbers_consistent("98%", "비율 0.97")
    assert not numbers_consistent("900%", "관광지 9곳")      # 정수는 백분율로 바꾸지 않음


def test_question_numbers_are_not_evidence(api_settings, repo, tmp_path):
    """질문 속 숫자는 분석 근거가 아니다. LLM이 질문의 숫자를 따라 써도 채택하지 않는다."""
    s, llm, _ = _fake_llm(api_settings, tmp_path, '{"region": "제주시"}')
    llm._openai_call = lambda messages, model: ("경사가 4321% 줄었습니다. [근거표 A1]", 100, 10)
    o = Orchestrator(s, repo=repo, llm=llm)
    plan = o._make_plan(AnalysisRequest(question="제주시 경사가 4321% 줄었어?", intent=Intent.ACCESS, region="제주시"), "rule")
    resp = o.execute(plan)
    assert resp.answer_source == "template"


def test_cited_evidence_parsing():
    assert cited_evidence("비율은 0.97입니다 [근거표 H1]. 9곳입니다(근거표 H2, H4).") == ["H1", "H2", "H4"]
    assert cited_evidence("근거표 H1·H3 참고") == ["H1", "H3"]
    assert cited_evidence("(근거표: H2) 참고") == ["H2"]
    assert cited_evidence("근거표 H1~H3") == ["H1", "H3"]
    assert cited_evidence("JDC공영주차장 2곳") == []


def test_answer_without_or_with_wrong_evidence_tags_is_rejected():
    ids, ref = {"A1", "A2"}, "점수 0.97 [근거표 A1] 부적합 9곳 [근거표 A2]"
    assert answer_problem("부적합 9곳입니다. [근거표 A2]", ref, ids) is None
    assert answer_problem("부적합 9곳입니다.", ref, ids) == "근거표 표시 없음"
    assert answer_problem("부적합 9곳입니다. [근거표 A9]", ref, ids) == "없는 근거표: A9"
    assert answer_problem("근거표가 없는 분석", "", set()) is None   # 근거표가 없는 답(예: 실시간 거절)은 표시를 요구하지 않음


def test_tool_answer_without_tags_falls_back(api_settings, repo, tmp_path):
    _, _, resp, _ = _run(api_settings, repo, tmp_path, lambda nums: "제주시에는 갈 만한 곳이 많습니다.")
    assert resp.answer_source == "template"
    assert next(t.detail for t in resp.trace if t.action == "tool_answer") == "문장틀 사용(근거표 표시 없음)"


def test_more_notation_variants():
    """Cursor 검토에서 나온 표기: 97.0%, 1,209, 근거표: H1, [H1], h1. ('9.0곳'은 소수·정수 구분 규칙상 계속 거부)"""
    assert not numbers_consistent("9.0곳", "부적합 9곳")
    assert numbers_consistent("97.0%", "비율 0.97")
    assert numbers_consistent("1,209곳", "관광지 1209곳")
    assert not numbers_consistent("1곳", "비율 1.0")                 # 반대 방향은 계속 막음
    assert cited_evidence("근거표: H1") == ["H1"]
    assert cited_evidence("9곳입니다 [H2].") == ["H2"]
    assert cited_evidence("근거표 h1") == ["H1"]
