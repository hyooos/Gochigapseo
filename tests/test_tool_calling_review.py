"""PR #5 리뷰용 재현 테스트."""
import dataclasses
import types

from jeju_agent.contracts import AnalysisRequest, Intent
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator


def _fake_llm(api_settings, tmp_path, tool_args: str):
    # 오케스트레이터 tool calling만 시험한다(하위 에이전트 LLM 판단은 test_agent_llm.py에서)
    s = dataclasses.replace(api_settings, llm_tool_calling=True, llm_cache=False, llm_max_calls_per_question=3, agent_llm=False)
    llm = LLMClient(s, runtime_dir=tmp_path)
    count = {"tool": 0, "text": 0}

    def fake_tool(messages, tools):
        count["tool"] += 1
        call = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="run_access_analysis", arguments=tool_args))
        return types.SimpleNamespace(tool_calls=[call], content=""), 100, 10

    def fake_text(messages, model):
        count["text"] += 1
        return "설명", 100, 10

    llm._openai_tool_request = fake_tool
    llm._openai_call = fake_text
    return s, llm, count


def test_rejected_tool_call_does_not_make_second_call(api_settings, repo, tmp_path):
    """tool 실행이 거부되면(조건 불일치) 두 번째 LLM 호출 없이 바로 규칙 흐름으로 돌아간다."""
    s, llm, count = _fake_llm(api_settings, tmp_path, '{"region": "서귀포시"}')
    o = Orchestrator(s, repo=repo, llm=llm)
    plan = o._make_plan(AnalysisRequest(question="제주시 갈 만한 곳", intent=Intent.ACCESS, region="제주시"), "rule")
    resp = o.execute(plan)
    # tool 1회 + 설명 1회만 허용 (거부된 tool 결과로 문장을 만드는 호출은 없어야 함)
    assert count["text"] == 1
    fb = [t.detail for t in resp.trace if t.action == "tool_call"]
    assert fb and "mismatch" in fb[0], f"거부 이유가 trace에 남아야 함: {fb}"


def test_whole_jeju_region_matches_none(api_settings, repo, tmp_path):
    """지역 미지정(region=None) 질문에서 모델이 '제주 전체'를 보내도 같은 조건으로 본다."""
    s, llm, count = _fake_llm(api_settings, tmp_path, '{"region": "제주 전체"}')
    o = Orchestrator(s, repo=repo, llm=llm)
    plan = o._make_plan(AnalysisRequest(question="갈 만한 곳 어디야?", intent=Intent.ACCESS, region=None), "rule")
    resp = o.execute(plan)
    assert any(t.action == "tool_call" and t.detail == "run_access_analysis" for t in resp.trace)


def test_tool_path_insufficient_discards_llm_sentence(api_settings, repo, tmp_path):
    """근거가 부족하면 tool이 만든 문장을 쓰지 않고 일반 경로와 같은 안내로 끝낸다."""
    s, llm, count = _fake_llm(api_settings, tmp_path, '{"region": "제주시"}')
    o = Orchestrator(s, repo=repo, llm=llm)
    plan = o._make_plan(
        AnalysisRequest(question="없는 장소", intent=Intent.ACCESS, region="제주시", place_ids=["NO_SUCH"]), "rule")
    resp = o.execute(plan)
    assert any(t.action == "tool_call" and t.detail == "run_access_analysis" for t in resp.trace)
    assert count["text"] == 1
    assert resp.answer_source == "template"
    assert resp.answer.startswith("분석에 필요한 근거가 부족합니다")
    assert "설명" not in resp.answer
    assert any(t.action == "verify" and t.detail == "근거 부족" for t in resp.trace)
