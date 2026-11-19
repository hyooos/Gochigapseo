import dataclasses

import pytest

from jeju_agent.contracts import AnalysisRequest, Intent
from jeju_agent.llm import LLMClient
from jeju_agent.llm.client import LLMRecord, ToolLoopResult
from jeju_agent.orchestrator import LLMPlan, Orchestrator, numbers_consistent


def test_offline_never_calls(settings, tmp_path):
    c = LLMClient(settings, runtime_dir=tmp_path)
    text, rec = c.chat_text("s", "u", purpose="t")
    assert text is None and rec.error == "offline" and not (tmp_path / "llm_usage.jsonl").exists()


def test_cache_and_per_question_limit(api_settings, tmp_path):
    calls = []

    def fake(messages, model):
        calls.append(1)
        return '{"intent": "access"}', 100, 20

    c = LLMClient(api_settings, runtime_dir=tmp_path, transport=fake)
    c.new_question()
    a, _ = c.chat_json("s", "q1", LLMPlan, purpose="plan")
    b, rec = c.chat_json("s", "q1", LLMPlan, purpose="plan")      # 같은 입력 → 캐시
    assert a.intent == Intent.ACCESS and b.intent == Intent.ACCESS and rec.cached and len(calls) == 1
    c.chat_text("s", "q2", purpose="x")
    _, rec3 = c.chat_text("s", "q3", purpose="x")                   # 질문당 상한 2회 초과
    assert rec3.error == "per_question_limit" and len(calls) == 2
    assert c.question_usage().input_tokens == 200


def test_bad_json_falls_back_to_rules(api_settings, repo, tmp_path):
    llm = LLMClient(api_settings, runtime_dir=tmp_path, transport=lambda m, model: ("모르겠어요", 10, 5))
    plan = Orchestrator(api_settings, repo=repo, llm=llm).plan("휠체어로 서귀포 2일 코스 짜줘")
    assert plan.parser == "rule" and plan.request.intent == Intent.COURSE and plan.request.days == 2
    assert plan.request.profile.mobility == "wheelchair_manual"


def test_network_error_falls_back(api_settings, repo, tmp_path):
    def boom(m, model):
        raise ConnectionError("down")

    o = Orchestrator(api_settings, repo=repo, llm=LLMClient(api_settings, runtime_dir=tmp_path, transport=boom))
    resp = o.ask("제주시에서 휠체어로 갈 만한 곳 추천해줘")
    assert resp.answer_source == "template" and resp.results[0].status == "ok"


def test_llm_region_outside_dictionary_is_ignored(api_settings, repo, tmp_path):
    llm = LLMClient(api_settings, runtime_dir=tmp_path,
                    transport=lambda m, model: ('{"intent":"access","region":"부산시"}', 10, 5))
    plan = Orchestrator(api_settings, repo=repo, llm=llm).plan("갈 만한 곳 알려줘")
    assert plan.parser == "llm" and plan.request.region is None


def test_numbers_check_rejects_invented_numbers():
    draft = "관광지 30곳 중 3곳이 부적합입니다."
    assert numbers_consistent("30곳 가운데 3곳이 부적합입니다.", draft)
    assert not numbers_consistent("30곳 중 3곳이 부적합이며 이동 시간이 20% 줄어듭니다.", draft)


def test_llm_nulls_and_loose_values_still_parse(api_settings, repo, tmp_path):
    """모델이 null을 주거나 "2일"·대문자·쉼표 목록처럼 답해도 LLM 해석으로 받아들이고, 목록 밖 값은 버린다."""
    reply = ('{"intent":"Course","region":"서귀포시","days":"2일","month":null,"mobility":"WHEELCHAIR_MANUAL",'
             '"needs":"acc_toilet, 아무거나","transport":null,"follow_up":null}')
    llm = LLMClient(api_settings, runtime_dir=tmp_path, transport=lambda m, model: (reply, 10, 5))
    plan = Orchestrator(api_settings, repo=repo, llm=llm).plan("서귀포 이틀 코스")
    assert plan.parser == "llm" and plan.request.intent == Intent.COURSE and plan.request.days == 2
    assert plan.request.profile.mobility == "wheelchair_manual" and plan.request.profile.needs == ["acc_toilet"]


def test_schema_error_names_field_and_is_not_cached(api_settings, tmp_path):
    calls = []

    def fake(messages, model):
        calls.append(1)
        return '{"intent":"뭔지모름"}', 10, 5

    c = LLMClient(api_settings, runtime_dir=tmp_path, transport=fake)
    c.new_question()
    got, rec = c.chat_json("s", "q", LLMPlan, purpose="plan")
    assert got is None and rec.error == "schema: intent"
    c.new_question()
    c.chat_json("s", "q", LLMPlan, purpose="plan")  # 틀린 응답은 캐시하지 않으므로 다시 호출
    assert len(calls) == 2


def test_usage_counts_plan_and_explain(api_settings, repo, tmp_path):
    def fake(messages, model):
        if "질문 해석기" in messages[0]["content"]:
            return '{"intent":"access"}', 100, 10
        return "설명", 200, 20

    o = Orchestrator(api_settings, repo=repo, llm=LLMClient(api_settings, runtime_dir=tmp_path, transport=fake))
    resp = o.ask("제주시에서 휠체어로 갈 만한 곳")
    assert resp.plan.parser == "llm"
    assert resp.llm_usage.calls == 2 and resp.llm_usage.input_tokens == 300


def test_confirmed_access_tool_is_executed_and_only_aggregate_result_is_returned(api_settings, repo, tmp_path, monkeypatch):
    """모델은 tool을 고르되, Python이 실행하고 payload 같은 기계용 상세값은 돌려주지 않는다."""
    api_settings = dataclasses.replace(api_settings, llm_tool_calling=True)
    llm = LLMClient(api_settings, runtime_dir=tmp_path)

    def fake_tool(system, user, tools, executor):
        assert {t["function"]["name"] for t in tools} == {
            "run_access_analysis", "run_course_planning", "run_status_analysis"}
        public = executor("run_access_analysis", {"region": "제주시"})
        assert public["ok"] is True and "payload" not in public and "trace" not in public
        return ToolLoopResult(public["summary"] + " [근거표 A1]", LLMRecord(purpose="analysis_tool", ok=True), "run_access_analysis")

    monkeypatch.setattr(llm, "call_tool", fake_tool)
    o = Orchestrator(api_settings, repo=repo, llm=llm)
    plan = o._make_plan(AnalysisRequest(question="제주시 갈 만한 곳", intent=Intent.ACCESS, region="제주시"), "rule")
    resp = o.execute(plan)
    assert resp.results[-1].agent == "access" and resp.answer_source == "llm"
    assert any(t.action == "tool_call" and t.detail == "run_access_analysis" for t in resp.trace)


class _FakeCompletions:
    """chat.completions.create 대역: 받은 인자를 남기고, reject에 든 낱말이 인자에 있으면 거부한다."""

    def __init__(self, reject=()):
        self.calls, self.reject = [], set(reject)

    def create(self, **kw):
        self.calls.append(kw)
        for k in self.reject:
            if k in kw:
                raise ValueError(f"Unsupported parameter: '{k}'")
        msg = type("M", (), {"content": "{}", "tool_calls": None})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()], "usage": None})()


def _client_with(settings, tmp_path, fake, **over):
    s = dataclasses.replace(settings, llm_mode="api", llm_api_key="dummy", **over)
    c = LLMClient(s, runtime_dir=tmp_path)
    c._client = type("O", (), {"chat": type("Ch", (), {"completions": fake})()})()
    return c


def test_reasoning_effort_is_sent_per_provider(settings, tmp_path):
    f = _FakeCompletions()
    _client_with(settings, tmp_path, f, llm_base_url="https://openrouter.ai/api/v1",
                 llm_reasoning_effort="low")._openai_call([{"role": "user", "content": "x"}], "openai/gpt-5-mini")
    assert f.calls[-1]["extra_body"] == {"reasoning": {"effort": "low"}} and f.calls[-1]["temperature"] == 0
    f = _FakeCompletions()
    _client_with(settings, tmp_path, f, llm_base_url="https://api.openai.com/v1",
                 llm_reasoning_effort="minimal")._openai_call([{"role": "user", "content": "x"}], "gpt-5-mini")
    assert f.calls[-1]["reasoning_effort"] == "minimal"
    f = _FakeCompletions()   # 빈칸이면 보내지 않는다(모델 기본값)
    _client_with(settings, tmp_path, f)._openai_call([{"role": "user", "content": "x"}], "gpt-4.1-mini")
    assert "reasoning_effort" not in f.calls[-1] and "extra_body" not in f.calls[-1]


def test_rejected_params_are_dropped_and_retried(settings, tmp_path):
    f = _FakeCompletions(reject={"temperature", "reasoning_effort"})
    c = _client_with(settings, tmp_path, f, llm_base_url="https://api.openai.com/v1", llm_reasoning_effort="low")
    text, _, _ = c._openai_call([{"role": "user", "content": "x"}], "m")
    assert text == "{}" and "temperature" not in f.calls[-1] and "reasoning_effort" not in f.calls[-1]
    f = _FakeCompletions(reject={"tools"})   # 상관없는 오류는 그대로 올린다
    with pytest.raises(ValueError):
        _client_with(settings, tmp_path, f)._openai_tool_request([{"role": "user", "content": "x"}], [{"t": 1}])
