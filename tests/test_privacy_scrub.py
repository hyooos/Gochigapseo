"""LLM이 쓴 판단 이유에 이용자 조건이 섞여도 분석 기록(trace)에는 남지 않는다."""
from jeju_agent.agents.base import BaseAgent
from jeju_agent.contracts import AnalysisRequest, Intent, TraceStep, TravelerProfile
from jeju_agent.privacy import scrub
from jeju_agent.verify import _sensitive_words


def test_scrub_hides_condition_words():
    t = scrub("휠체어 이용자라서 경사 7° 넘는 곳은 뺐고, 보청기 쓰는 분께 음성 안내는 의미 없음",
              TravelerProfile(mobility="wheelchair_manual", hearing="hard"))
    assert "휠체어" not in t and "보청기" not in t and "(조건)" in t and "경사 7°" in t


def test_agent_step_scrubs_llm_reason():
    class Dummy(BaseAgent):
        name = "dummy"

        def _run(self, req, ctx):
            return None
    a = Dummy()
    a._profile = TravelerProfile(mobility="stroller", needs=["acc_toilet"])
    a.step("llm:select", "유아차 끌고 다니고 장애인 화장실이 꼭 필요해서 사진 포함")
    d = a._trace[-1].detail
    assert "유아차" not in d and "장애인 화장실" not in d


def test_privacy_check_catches_short_words():
    """검증은 '수동 휠체어'뿐 아니라 LLM이 줄여 쓴 '휠체어'도 잡는다."""
    from jeju_agent.contracts import Plan
    req = AnalysisRequest(question="q", intent=Intent.ACCESS, profile=TravelerProfile(mobility="wheelchair_manual"))
    words = _sensitive_words(Plan(request=req, understood="", steps=[], parser="rule"))
    text = TraceStep(agent="x", action="llm", detail="휠체어라서").model_dump_json()
    assert any(w in text for w in words)
