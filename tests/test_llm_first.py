"""LLM 모드 우선(O36): LLM이 맞게 읽은 조건을 규칙이 덮지 않는다. 규칙은 허용 값 검사와 안전장치만.

실제 모델 대신 '대역 모델'(정해 둔 답)을 넣어 코드 흐름만 본다. 모델이 실제로 이렇게 답하는지는
scripts/eval_golden.py --api · scripts/eval_dialog.py --api 로 따로 본다.
"""
import json

import pytest

from jeju_agent.contracts import Intent, TravelerProfile
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator


class Model:
    """시스템 프롬프트로 어떤 호출인지 가리고, 미리 넣어 둔 답을 차례로 돌려준다."""

    def __init__(self):
        self.plan: list[dict] = []
        self.dialog: list[dict] = []

    def __call__(self, messages, model):
        system = messages[0]["content"]
        if "대화 해석기" in system:
            return json.dumps(self.dialog.pop(0), ensure_ascii=False), 50, 10
        if "질문 해석기" in system:
            return json.dumps(self.plan.pop(0), ensure_ascii=False), 50, 10
        return "설명", 50, 5   # 설명은 숫자 대조에서 떨어져 문장틀로 돌아간다


@pytest.fixture
def llm(api_settings, repo, tmp_path):
    m = Model()
    return Orchestrator(api_settings, repo=repo, llm=LLMClient(api_settings, runtime_dir=tmp_path, transport=m)), m


def first(o, m, q, plan):
    m.plan.append(plan)
    return o.plan(q, {}, ask_profile=True)


@pytest.mark.parametrize("q, plan, want", [
    ("휠체어는 필요없고 지팡이 짚어요. 서귀포 2일 코스 짜줘",
     {"intent": "course", "region": "서귀포시", "days": 2, "mobility": "walker", "dropped": ["wheelchair_manual"]},
     {"mobility": "walker", "needs": []}),
    ("휠체어로 서귀포 2일 코스 짜줘. 장애인 화장실은 필요 없어",
     {"intent": "course", "region": "서귀포시", "days": 2, "mobility": "wheelchair_manual", "needs": [], "dropped": ["acc_toilet"]},
     {"mobility": "wheelchair_manual", "needs": []}),
    ("전동휠체어예요. 버스 안 타고 택시로 다닐게요. 서귀포 코스",
     {"intent": "course", "region": "서귀포시", "days": 1, "mobility": "wheelchair_power", "transport": "taxi", "dropped": ["bus"]},
     {"mobility": "wheelchair_power", "transport": "taxi"}),
    ("비 와도 괜찮아요 휠체어로 바다 위주로 제주시 코스",
     {"intent": "course", "region": "제주시", "mobility": "wheelchair_manual", "interests": ["sea"], "needs": []},
     {"needs": [], "interests": ["sea"]}),
])
def test_first_question_llm_reading_wins(llm, q, plan, want):
    o, m = llm
    p = first(o, m, q, plan).request.profile
    for k, v in want.items():
        assert getattr(p, k) == v, k


def test_llm_intent_wins_over_keyword(llm):
    o, m = llm
    r = first(o, m, "식당 말고 제주시에 갈 만한 관광지 알려줘. 휠체어 타요",
              {"intent": "access", "region": "제주시", "mobility": "wheelchair_manual"}).request
    assert r.intent == Intent.ACCESS


def test_safety_guard_when_model_omits_conditions(llm):
    """약한 모델이 이용자가 분명히 말한 휠체어·화장실을 빠뜨려도 규칙이 지킨다(실내 선호 같은 취향은 지키지 않음)."""
    o, m = llm
    p = first(o, m, "엄마가 휠체어 타셔서 화장실 꼭 필요해. 서귀포 코스",
              {"intent": "course", "region": "서귀포시", "mobility": "none", "needs": []}).request.profile
    assert p.mobility == "wheelchair_manual" and p.needs == ["acc_toilet"]


def test_dropped_against_clear_need_is_kept_and_flagged(llm):
    """LLM은 '뺀다'는데 이용자 말은 '필요하다'(화장실 없으면 안 돼요)면 지키고 확인 문구를 단다."""
    o, m = llm
    pl = first(o, m, "휠체어로 서귀포 코스. 장애인 화장실 없으면 안 돼요",
               {"intent": "course", "region": "서귀포시", "mobility": "wheelchair_manual", "needs": [], "dropped": ["acc_toilet"]})
    assert pl.request.profile.needs == ["acc_toilet"]
    assert "확인: 장애인 화장실" in pl.understood


def course(o, m, needs=None):
    mem: dict = {}
    m.plan.append({"intent": "course", "region": "서귀포시", "days": 2, "mobility": "wheelchair_manual", "needs": needs or []})
    q = "휠체어로 서귀포 2일 코스 짜줘" + (". 장애인 화장실 꼭 필요해" if needs else "")
    o.execute(o.plan(q, mem, ask_profile=True), mem)
    return mem


def test_dialog_drop_condition_by_llm(llm):
    o, m = llm
    mem = course(o, m, ["acc_toilet"])
    m.dialog.append({"type": "question", "question": "서귀포 2일 수동 휠체어 코스를 장애인 화장실 조건 없이 다시 짜줘",
                     "plan": {"intent": "course", "region": "서귀포시", "days": 2, "mobility": "wheelchair_manual",
                              "needs": [], "follow_up": True, "dropped": ["acc_toilet"]}})
    pl = o.plan("장애인 화장실은 이제 필요 없어", mem, ask_profile=True)
    assert pl.parser == "llm" and pl.request.profile.needs == [] and pl.request.profile.mobility == "wheelchair_manual"
    assert "뺀 조건: 장애인 화장실" in pl.understood and pl.request.days == 2


def test_dialog_none_needs_dropped_to_remove(llm):
    """대화 중 LLM이 'none'으로만 채우면(빈칸 채우기) 지금 조건을 지키고, dropped로 '아니라고 했다'를 내면 지운다."""
    o, m = llm
    mem = course(o, m)
    m.dialog.append({"type": "question", "question": "서귀포 2일 코스 새로 짜줘",
                     "plan": {"intent": "course", "region": "서귀포시", "days": 2, "mobility": "none", "follow_up": True}})
    assert o.plan("숙소 근처로 다시 짜줘", mem, ask_profile=True).request.profile.mobility == "wheelchair_manual"
    m.dialog.append({"type": "question", "question": "서귀포 2일 코스 새로 짜줘",
                     "plan": {"intent": "course", "region": "서귀포시", "days": 2, "mobility": "none", "follow_up": False,
                              "dropped": ["wheelchair_manual"]}})
    pl = o.plan("이번엔 나 혼자 가. 나는 걷는 데 문제없어", mem, ask_profile=True)
    assert pl.request.profile.mobility == "none" and pl.request.intent == Intent.COURSE   # 예전 조건을 다시 붙이지도, 되묻지도 않는다


def test_rule_drop_turn_only_without_llm(llm, orch):
    """조건 빼기를 규칙이 먼저 처리하는 건 키 없는 모드(또는 LLM 실패)뿐 — API 모드는 LLM이 먼저 읽는다."""
    o, m = llm
    mem = course(o, m, ["acc_toilet"])
    m.dialog.append({"type": "question", "question": "서귀포 2일 휠체어 코스",
                     "plan": {"intent": "course", "region": "서귀포시", "days": 2, "mobility": "wheelchair_manual",
                              "needs": [], "follow_up": True, "dropped": ["acc_toilet"]}})
    assert o.plan("장애인 화장실은 이제 필요 없어", mem, ask_profile=True).parser == "llm"
    off: dict = {}
    orch.ask("휠체어로 서귀포 2일 코스 짜줘. 장애인 화장실 꼭 필요해", off)
    assert orch.plan("장애인 화장실은 이제 필요 없어", off, ask_profile=True).parser == "rule"


def test_form_free_text_read_by_llm(llm, orch):
    """화면 3단계 '직접 적은 말': API 모드는 LLM이 읽고, 버튼으로 고른 값이 우선이다."""
    o, m = llm
    form = {"intent": "course", "region": "서귀포시", "days": 2, "profile": TravelerProfile()}
    m.plan.append({"intent": "course", "mobility": "walker", "needs": ["rest_often"], "dropped": ["wheelchair_manual"]})
    p = o.plan_form("서귀포 2일 코스", form, extra="휠체어는 안 쓰고 지팡이 짚어요. 자주 쉬어야 해요").request.profile
    assert p.mobility == "walker" and p.needs == ["rest_often"]
    m.plan.append({"intent": "course", "mobility": "walker"})
    btn = {**form, "profile": TravelerProfile(mobility="wheelchair_power")}
    assert o.plan_form("q", btn, extra="지팡이도 써요").request.profile.mobility == "wheelchair_power"   # 버튼이 우선
    assert orch.plan_form("q", form, extra="장애인 화장실은 필요 없어").request.profile.needs == []      # 키 없음: 규칙(부정 표현)


def test_llm_nearby_for_unsupported_facility_is_refused(llm):
    """gpt-5-mini 실측(11/19): '근처 병원 알려줘'를 근처 찾기로 읽음 → 식당·숙소 목록이 병원 답처럼 나감.
    찾을 수 없는 시설만 말했으면 답할 수 없음으로 둔다. 식당도 함께 말했으면 근처 찾기 그대로."""
    o, m = llm
    assert first(o, m, "근처 병원 알려줘", {"intent": "nearby"}).request.intent == Intent.UNSUPPORTED
    assert first(o, m, "서귀포 약국 어디 있어?", {"intent": "nearby", "region": "서귀포시"}).request.intent == Intent.UNSUPPORTED
    assert first(o, m, "중문 근처 병원 말고 식당 알려줘", {"intent": "nearby"}).request.intent == Intent.NEARBY
    assert first(o, m, "애월 맛집 추천해줘", {"intent": "nearby", "region": "애월읍"}).request.intent == Intent.NEARBY
