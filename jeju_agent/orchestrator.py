"""오케스트레이터: 질문 이해 → 계획 확인 → 하위 에이전트 호출 → 검증 → 설명
API 모드면 LLM이 먼저 읽고 규칙이 값 범위·안전 조건만 확인함 (실패하면 규칙 해석)
LLM엔 질문·조건 요약·지역 이름만 보냄
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, get_args

from pydantic import BaseModel, ValidationError, model_validator

from .agents import REGISTRY, AgentContext
from .agents.access import FAC_KO as FAC_LABEL
from .config import PROMPT_DIR, Settings
from . import progress
from .contracts import (
    KIND_KO,
    AgentResult,
    AnalysisRequest,
    Intent,
    Interest,
    Need,
    OrchestratorResponse,
    Plan,
    TraceStep,
    TravelerProfile,
)
from .data import DataNotReady, Repository
from .data.regions import region_aliases
from .edits import FAC_WORDS, REDO, apply_edit, ask, has_day_ref, keep_days, match_places, parse_edit, place_like
from .llm import LLMClient
from .text import date_label, josa
from .verify import answer_failed, answer_problem, cited_evidence, numbers_consistent, unknown_numbers, verify  # noqa: F401 (예전 위치 호환)
from .verify import evidence_ids as _evidence_ids

INTENT_KO = {Intent.ACCESS: "조건에 맞는 장소 찾기", Intent.COURSE: "여행 코스 짜기", Intent.STATUS: "무장애 관광 현황",
             Intent.REPORT: "보고서 정리", Intent.UNSUPPORTED: "지원하지 않는 질문",
             Intent.NEARBY: "근처 식당·카페·숙소 찾기"}
# 의도 → 처리할 에이전트. 안 적은 건 이름 같은 에이전트(access·course·status)가 처리
# nearby는 따로 안 만들고 access를 넓혀서 씀
AGENT_FOR = {Intent.NEARBY: "access"}

STEPS = {
    Intent.ACCESS: ["무장애 관광지 자료 불러오기", "이용자 조건(이동 방법·감각·필요 시설) 정리",
                    "장소별 경사·계단·접근불가 기록과 시설 확인", "적합·주의·부적합 판정과 점수 계산", "근거 검증 후 설명"],
    Intent.COURSE: ["접근성 진단 에이전트 호출(조건에 맞는 장소)", "여행 달의 날씨 위험 계산(센터 날씨 자료가 있으면)",
                    "속도(여유·보통)·하루 장소 수 정하기", "가까운 순서로 날짜별 배치(이동 시간 상한)",
                    "장애인 화장실·실내 대안·입장료·버스 정류소 점검", "근거 검증 후 설명"],
    Intent.STATUS: ["무장애 관광지 자료 불러오기", "지역별 시설 확인 현황 집계", "정보 공백·자료 기준 시점 점검",
                    "보완 우선 지역 고르기", "근거 검증 후 설명"],
    Intent.REPORT: ["직전 분석 결과 불러오기", "보고서 문단·근거표·한계·출처 정리"],
    Intent.UNSUPPORTED: ["근거 데이터 확인", "답할 수 없는 이유와 대안 안내"],
    Intent.NEARBY: ["무장애 코스 주변정보(식당·카페·숙소) 불러오기", "기준 장소 정하기(○○ 근처 또는 질문 지역)",
                    "장애인 화장실 조건으로 적합·부적합 판정", "장애인 화장실 있는 곳 먼저, 가까운 순으로 고르기",
                    "근거 검증 후 설명(전화로 영업 여부 확인 안내)"],
}

KW = {
    "unsupported": ["실시간", "지금 붐비", "지금 사람", "예약해", "예매해", "결제", "길 안내", "길안내", "내비",
                    "내일 날씨", "날씨 예보", "병원", "약국", "영업", "문 열", "휴무"],
    # 근처 식당·카페·숙소. 맛·평점 자료는 없어서 휠체어 진입 가능으로 조사된 곳만 답함
    "nearby": ["식당", "맛집", "밥집", "밥 먹", "밥먹", "점심", "저녁", "음식점", "카페", "편의점", "숙소", "숙박", "호텔", "펜션",
               "리조트", "게스트하우스", "잘 곳", "잘곳", "묵을"],
    "report": ["보고서", "리포트", "문서로", "정리해"],
    "status": ["현황", "통계", "지역별", "읍면동별", "부족한 지역", "사업계획서", "정책", "몇 곳", "몇 개", "분포", "공백", "비율",
               "보완", "열린관광지"],
    "course": ["코스", "일정", "루트", "동선", "여행 계획", "계획 짜", "짜줘", "짜 줘", "돌아보"],
    "access": ["추천", "갈 만한", "갈만한", "가볼", "가 볼", "어디", "장소", "관광지", "곳", "가능한", "적합", "괜찮은", "다닐"],
    "follow_up": ["그중", "그 중", "거기", "그곳", "이 장소", "이 곳들", "이곳들", "다시", "늘려", "줄여", "바꿔", "말고"],
}
MOBILITY_KW = [("wheelchair_power", ["전동휠체어", "전동 휠체어", "전동스쿠터", "전동 스쿠터"]),
               ("wheelchair_manual", ["휠체어"]),
               ("walker", ["목발", "지팡이", "보행기", "보행 보조", "보행보조", "걷기 힘", "걷는 게 힘", "거동"]),
               ("stroller", ["유모차", "유아차", "아기랑", "아기와", "영유아"]),
               ("senior", ["어르신", "노인", "고령", "할머니", "할아버지", "노부모", "70대", "80대", "90대", "오래 못 걸",
                           "오래 걷지 못"])]
VISION_KW = [("blind", ["시각장애", "전맹", "안내견", "앞을 못"]), ("low", ["저시력", "시력이 나", "눈이 잘 안", "잘 안 보"])]
HEARING_KW = [("deaf", ["청각장애", "농인", "수어"]), ("hard", ["난청", "보청기", "잘 안 들", "귀가 어두"])]
COGNITION_KW = [("intellectual", ["발달장애", "발달 장애", "지적장애", "지적 장애", "자폐", "다운증후군", "발달이 느"])]
NEED_KW = {"acc_toilet": ["화장실"], "acc_parking": ["주차"], "rest_often": ["쉬엄쉬엄", "자주 쉬", "천천히", "여유롭", "쉬면서"],
           "indoor_pref": ["실내", "비 오", "비오", "우천", "비가 와", "비 와"]}
TRANSPORT_KW = [("bus", ["버스", "대중교통"]), ("taxi", ["택시", "콜택시"]), ("car", ["렌터카", "렌트카", "자차", "차로", "운전"])]
INTEREST_KW = {"sea": ["바다", "바닷가", "해변", "해안", "포구"], "forest": ["숲", "오름", "공원", "수목원"],
               "museum": ["박물관", "미술관", "전시", "과학관"], "village": ["마을", "올레"]}
RIDE_CONTEXT = ("이동", "차에", "차를", "차로", "차 타", "차타", "앉아", "타고 가", "타기", "한 번에", "한번에", "멀미", "오래 타")
AIRPORT = "제주국제공항"
_ORIGIN = re.compile(r"([가-힣]{2,6}?)(?:에서\s*출발|\s*출발|에\s*숙소|\s*숙소에서|에\s*살|에\s*사는|\s*집에서|에\s*있는\s*집)")
KO_DAYS = {"하루": 1, "당일": 1, "이틀": 2, "사흘": 3, "나흘": 4}

NO_CONDITION = ("불편한 점 없", "불편한 데 없", "불편 없", "조건 없이", "그대로 진행")


EXTRA_PICKS = ("lunch", "dinner", "lodging")   # 화면에서 코스에 넣을지 고르는 식당·숙소
CARRIED = ("extras_pick",)                    # 이어 묻기·수정 대화에서도 유지되는 화면 선택


def _carried(last: AnalysisRequest) -> dict:
    """앞 요청의 화면 선택(식당·숙소) 이어받음. 폼에서 끈 점심이 대화로 고칠 때 다시 생기면 안 됨"""
    return {k: last.params[k] for k in CARRIED if k in last.params}


def profile_question(q: str) -> dict:
    """조건 되묻기 버튼. 누르면 조건 붙은 질문으로 바로 실행됨 (낱말은 MOBILITY_KW 등이랑 맞춰야 함)"""
    opts = [("휠체어", f"휠체어로 {q}"), ("오래 걷기 힘듦", f"오래 못 걸어서 {q}"), ("유아차", f"유아차를 끌고 {q}"),
            ("눈이 불편", f"눈이 잘 안 보여서 {q}"), ("귀가 불편", f"귀가 잘 안 들려서 {q}"), ("발달·지적장애", f"발달장애가 있어서 {q}"), ("불편한 점 없음 — 그대로", f"{q} (불편한 점 없음)")]
    return {"kind": "ask", "message": "더 맞게 찾으려고 한 가지만 여쭤볼게요. 이동이나 몸에 불편한 점이 있나요? "
                                      "아래에서 고르시거나 '없어', '휠체어 타'처럼 말로 답하셔도 돼요.",
            "choices": [{"label": lab, "q": qq} for lab, qq in opts], "for": "profile"}


EDIT_KO = {"exclude": lambda e: f"{e['name']} 빼기", "lighter": lambda e: f"{e['day']}일차 장소 1곳 줄이기",
           "lighter_all": lambda e: "모든 날을 여유 있게", "join": lambda e: f"{e['who']} 함께(+{e['n']}명)",
           "include": lambda e: f"{e['name']} 다시 넣기", "day_count": lambda e: f"{e['day']}일차를 {e['n']}곳으로",
           "day_swap": lambda e: f"{e['day']}일차를 다른 곳으로"}
# 조건 되묻기에 '없어', '괜찮아'로 답한 경우
NO_ANSWER_RE = re.compile(r"없어|없음|없습니다|없고|없다|없는데|괜찮|상관\s*없|딱히|아니(?:요|오|야)?\b|됐어|그대로 둬|고마워|감사")
TURNS_KEPT = 4   # 대화 해석에 보내는 최근 질문 수

_REGION_DESC = "지역을 지정하지 않으면 생략"
_WHOLE_ISLAND = {"제주 전체", "제주", "전체", "제주도", ""}


def _region_arg(value):
    """'제주 전체'로 읽히는 값은 None이랑 같게 봄"""
    if isinstance(value, str) and value.strip() in _WHOLE_ISLAND:
        return None
    return value


ANALYSIS_TOOLS = [
    {"type": "function", "function": {"name": "run_access_analysis", "description": "이용자 조건에 맞는 무장애 관광지를 판정한다.",
     "parameters": {"type": "object", "properties": {"region": {"type": "string", "description": _REGION_DESC}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_course_planning", "description": "조건에 맞는 장소로 날짜별 여행 코스를 짠다.",
     "parameters": {"type": "object", "properties": {"region": {"type": "string", "description": _REGION_DESC},
                                                     "days": {"type": "integer", "minimum": 1, "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_status_analysis", "description": "지역별 무장애 관광 인프라 현황을 집계한다.",
     "parameters": {"type": "object", "properties": {"region": {"type": "string", "description": _REGION_DESC}}, "additionalProperties": False}}},
]
TOOL_INTENTS = {"run_access_analysis": Intent.ACCESS, "run_course_planning": Intent.COURSE, "run_status_analysis": Intent.STATUS}


class LLMPlan(BaseModel):
    """LLM 해석 결과. 모델마다 null·"2일"·"Course"처럼 형식이 달라서 받기 전에 정리함
    intent만 필수, 나머지는 모르면 None → 규칙 값 씀
    """
    intent: Intent
    region: str | None = None
    days: int | None = None
    month: int | None = None
    mobility: str | None = None
    vision: str | None = None
    hearing: str | None = None
    cognition: str | None = None
    needs: list[str] | None = None
    transport: str | None = None
    companions: int | None = None
    max_ride_min: int | None = None
    interests: list[str] | None = None
    origin: str | None = None
    follow_up: bool | None = None
    dropped: list[str] | None = None   # 이용자가 '아니라고·필요 없다고' 한 조건 (예: acc_toilet, bus)

    @model_validator(mode="before")
    @classmethod
    def _tidy(cls, data):
        if not isinstance(data, dict):
            return data
        out = {}
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip()
                # 이동·감각 칸의 "none"은 '불편 없음'이라는 값임(모름=null이랑 다름). 다른 칸은 none이면 모름
                nulls = ("", "null", "n/a", "모름") if k in ("mobility", "vision", "hearing", "cognition") else \
                    ("", "null", "none", "n/a", "모름", "없음")
                if v.lower() in nulls:
                    v = None
            if v is not None and k in ("intent", "mobility", "vision", "hearing", "cognition", "transport") and isinstance(v, str):
                v = v.lower()
            if v is not None and k in ("days", "month", "companions", "max_ride_min") and not isinstance(v, int):
                m = re.search(r"\d+", str(v).replace(",", ""))
                v = int(m.group()) if m else None
            if v is not None and k == "follow_up" and not isinstance(v, bool):
                v = str(v).lower() in ("true", "1", "yes", "y")
            if k in ("needs", "interests", "dropped") and isinstance(v, str):
                v = [x.strip() for x in v.split(",") if x.strip()]
            out[k] = v
        return out


class LLMTurn(BaseModel):
    """대화 해석 결과 (prompts/dialog.md): edit / question / ask"""
    type: str
    command: str | None = None
    question: str | None = None
    plan: LLMPlan | None = None
    ask: str | None = None
    choices: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _tidy(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for k in ("type", "command", "question", "ask"):
            v = out.get(k)
            out[k] = None if not isinstance(v, str) or v.strip().lower() in ("", "null", "none") else v.strip()
        plan = out.get("plan")
        if not (isinstance(plan, dict) and plan.get("intent")):
            plan = {k: v for k, v in data.items() if k in LLMPlan.model_fields} if data.get("intent") else None
        out["plan"] = plan   # 질문 항목만 바로 낸 경우도 받음
        ch = out.get("choices")
        out["choices"] = [str(c).strip() for c in ch if str(c).strip()] if isinstance(ch, list) else []
        t = (out.get("type") or "").lower()
        if t not in ("edit", "question", "ask"):
            t = "edit" if out["command"] else "ask" if out["ask"] else "question"
        out["type"] = t
        return out


def _has(q: str, key: str) -> bool:
    return any(k in q for k in KW[key])


NEARBY_KIND_KW = {"restaurant": ["식당", "맛집", "밥집", "밥 먹", "밥먹", "점심", "저녁", "음식점"],
                  "cafe": ["카페", "편의점"],
                  "lodging": ["숙소", "숙박", "호텔", "펜션", "리조트", "게스트하우스", "잘 곳", "잘곳", "묵을"]}


def parse_nearby_kinds(q: str) -> list[str]:
    """근처 질문에서 찾는 종류(restaurant·cafe·lodging). 말 안 했으면 [] = 전부"""
    return [k for k, words in NEARBY_KIND_KW.items() if any(w in q for w in words)]


# ── 부정 표현 ──
# 낱말만 찾으면 '화장실은 필요 없어'·'휠체어 안 타요'를 거꾸로 읽음
# 낱말 뒤 같은 마디(다른 조건 낱말 전까지, 최대 NEG_WINDOW글자)에 아래 말 있으면 부정으로 봄
# '화장실 없는 곳은 빼줘'는 필요하다는 뜻이라 부정 아님 (빼줘·없으면은 목록에 없음)
NEG_WINDOW = 15
NEG_RE = re.compile(r"필요\s*(?:없|하지\s*않|치\s*않|는\s*없)|안\s*필요|없어도|없이도|상관\s*(?:없|않)|안\s*해도|(?:와|해)도\s*괜찮|"
                    r"안\s*(?:타|써|쓰)|타지\s*않|쓰지\s*않|이용하지\s*않|말고|빼고|제외하고|아니(?:고|에요|예요|야|요|라|다|어도|라도)|아닌데|아님")
CLAUSE_END = re.compile(r"[.!?\n,]|고\s|지만|는데|그리고")


def _all_kw() -> list[str]:
    words = [w for _, ws in MOBILITY_KW + VISION_KW + HEARING_KW + COGNITION_KW + TRANSPORT_KW for w in ws]
    words += [w for ws in list(NEED_KW.values()) + list(INTEREST_KW.values()) for w in ws]
    return sorted(set(words), key=len, reverse=True)


def _negated_at(q: str, end: int, others: list[str]) -> bool:
    """q[end:]가 낱말 바로 뒤. 같은 마디 안에 부정 표현 있는지 봄"""
    tail = q[end:end + NEG_WINDOW]
    cut = CLAUSE_END.search(tail)
    if cut:
        tail = tail[:cut.start()] + (tail[cut.start()] if tail[cut.start()] == "고" else "")
    nxt = min((i for w in others if (i := tail.find(w)) > 0), default=None)
    if nxt is not None:
        tail = tail[:nxt]
    return bool(NEG_RE.search(tail))


def _hits(q: str, words) -> tuple[bool, bool]:
    """(긍정으로 말함, 부정으로 말함). 같은 낱말 여러 번 나오면 각각 봄 ('전동휠체어 말고 수동 휠체어')"""
    others = _all_kw()
    pos = neg = False
    for w in words:
        for m in re.finditer(re.escape(w), q):
            inner = [o for o in others if o not in w and w not in o]   # 자기 자신·포함 관계 낱말에선 안 끊음
            if _negated_at(q, m.end(), inner):
                neg = True
            else:
                pos = True
    return pos, neg


def _said(q: str, words) -> bool:
    return _hits(q, words)[0]


def parse_profile(q: str) -> tuple[dict, list[str]]:
    """질문 → 이용자 조건 값(찾은 것만) + 찾은 항목 이름. 부정한 낱말('필요 없어'·'안 타요'·'말고')은 뺌"""
    got: dict[str, Any] = {}
    mob = next((m for m, words in MOBILITY_KW if _said(q, words)), None)
    if mob:
        got["mobility"] = mob
    vis = next((v for v, words in VISION_KW if _said(q, words)), None)
    if vis:
        got["vision"] = vis
    hear = next((h for h, words in HEARING_KW if _said(q, words)), None)
    if hear:
        got["hearing"] = hear
    cog = next((c for c, words in COGNITION_KW if _said(q, words)), None)
    if cog:
        got["cognition"] = cog
    needs = [n for n, words in NEED_KW.items() if _said(q, words)]
    if needs:
        got["needs"] = needs
    tr = next((t for t, words in TRANSPORT_KW if _said(q, words)), None)
    if tr:
        got["transport"] = tr
    ints = [i for i, words in INTEREST_KW.items() if _said(q, words)]
    if ints:
        got["interests"] = ints
    ride = parse_ride(q)
    if ride:
        got["max_ride_min"] = ride
    m = re.search(r"(\d{1,2})\s*명", q)
    if m and 1 <= int(m.group(1)) <= 20:
        got["companions"] = int(m.group(1))
    return got, list(got)


def parse_dropped(q: str) -> dict[str, list[str]]:
    """'아니라고만' 한 조건 (긍정으로는 안 말함). mobility·vision·hearing·cognition·needs·transport·interests 중 찾은 것만
    대화에서 조건 뺄 때, LLM 해석에서 지울 때 씀
    """
    out: dict[str, list[str]] = {}
    for key, table in (("mobility", MOBILITY_KW), ("vision", VISION_KW), ("hearing", HEARING_KW),
                       ("cognition", COGNITION_KW), ("transport", TRANSPORT_KW),
                       ("needs", list(NEED_KW.items())), ("interests", list(INTEREST_KW.items()))):
        for val, words in table:
            pos, neg = _hits(q, words)
            if neg and not pos:
                out.setdefault(key, []).append(val)
    # '휠체어 안 타요'는 수동·전동 둘 다 아니라는 뜻
    if "wheelchair_manual" in out.get("mobility", []) and "wheelchair_power" not in out["mobility"] \
            and not _said(q, dict(MOBILITY_KW)["wheelchair_power"]):
        out["mobility"].append("wheelchair_power")
    return out


def drop_conditions(prof: TravelerProfile, dropped: dict[str, list[str]]) -> TravelerProfile:
    """parse_dropped 결과를 조건에서 뺌 (단일 값은 기본값으로, 목록은 그 값만)"""
    d = prof.model_dump()
    base = TravelerProfile().model_dump()
    for k, vals in dropped.items():
        if k in ("needs", "interests"):
            d[k] = [x for x in d[k] if x not in vals]
        elif d.get(k) in vals:
            d[k] = base[k]
    return TravelerProfile(**d)


SAFETY_NEEDS = ("acc_toilet", "acc_parking", "rest_often")   # LLM이 빠뜨려도 규칙이 지키는 필요 시설 (몸 상태 관련)

DROP_KO = {"wheelchair_manual": "휠체어", "wheelchair_power": "전동 휠체어", "walker": "보행 보조", "stroller": "유아차",
           "senior": "고령", "blind": "시각장애", "low": "저시력", "deaf": "청각장애", "hard": "난청", "intellectual": "발달장애",
           "acc_toilet": "장애인 화장실", "acc_parking": "장애인 주차", "rest_often": "자주 쉬기", "indoor_pref": "실내 위주",
           "bus": "버스", "taxi": "택시", "car": "자가용", "sea": "바다", "forest": "숲", "museum": "박물관", "village": "마을"}


def parse_ride(q: str) -> int | None:
    """'차에 1시간 이상 못 앉아 계셔' → 60. 이동·차·앉기 얘기가 있을 때만"""
    if not any(w in q for w in RIDE_CONTEXT):
        return None
    m = re.search(r"(\d{1,3})\s*분", q)
    if m and 10 <= int(m.group(1)) <= 180:
        return int(m.group(1))
    m = re.search(r"(\d)\s*시간", q)
    if m and 1 <= int(m.group(1)) <= 3:
        return int(m.group(1)) * 60
    return None


def parse_origin(q: str, aliases: dict[str, str]) -> tuple[str | None, str]:
    """출발지(집·숙소) → (표준 이름, 출발지 표현 지운 질문). 공항이면 '제주국제공항'"""
    if "공항" in q and any(w in q for w in ("출발", "도착", "내려서", "에서")):
        return AIRPORT, q.replace("공항", " ")
    for m in _ORIGIN.finditer(q):
        word = m.group(1)
        for alias, std in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
            if word.endswith(alias):
                return std, q[: m.start()] + " " + q[m.end():]
    return None, q


def parse_days(q: str) -> int | None:
    m = re.search(r"(\d)\s*박\s*(\d)\s*일", q)
    if m:
        return int(m.group(2))
    m = re.search(r"(\d)\s*박", q)
    if m:
        return int(m.group(1)) + 1
    for m in re.finditer(r"(\d)\s*일\s*(?:코스|일정|여행|동안|간|짜리|로)", q):
        if not re.search(r"월\s*\d?$", q[: m.start() + 1][:-1]):   # '10월 3일 여행'의 3일은 날짜임
            return int(m.group(1))
    return next((n for w, n in KO_DAYS.items() if w in q), None)


def parse_meal_wants(q: str) -> list[str]:
    """코스 질문의 식당·숙소 요청: lunch · dinner · lodging. 카페는 코스에 안 넣음"""
    out = []
    if any(w in q for w in NEARBY_KIND_KW["restaurant"]) and "저녁" not in q or "점심" in q:
        out.append("lunch")
    if "저녁" in q or "세 끼" in q or "세끼" in q or "삼시" in q:
        out.append("dinner")
    if any(w in q for w in NEARBY_KIND_KW["lodging"]):
        out.append("lodging")
    return out


def parse_month(q: str) -> int | None:
    m = re.search(r"(?<!\d)(1[0-2]|[1-9])\s*월", q)
    return int(m.group(1)) if m else None


class Orchestrator:
    def __init__(self, settings: Settings, repo: Repository | None = None, llm: LLMClient | None = None):
        self.s = settings
        self.repo = repo or Repository(settings.data_mode)
        self.llm = llm or LLMClient(settings)
        self._trace: list[TraceStep] = []
        self._nested: list[AgentResult] = []   # 다른 에이전트가 부른 결과(코스 → 접근성 진단). 리포트에 같이 보여 줌
        self._explored: list[AgentResult] = []  # 데이터 탐색 결과 (맨 앞에 붙음)
        self._planned = False  # plan()이 방금 질문 집계를 시작했는지 (plan 호출도 비용에 넣으려고)

    # ── 지역 사전 ──
    def regions(self) -> dict[str, str]:
        """질문 속 표현 → 표준 지역명 ('서귀포' → '서귀포시')"""
        extra: list[str] = []
        try:
            pl = self.repo.get("places").df
            extra = sorted(set(pl["emd"].dropna().astype(str))) if "emd" in pl else []
        except DataNotReady:
            pass
        return region_aliases(extra)

    # ── 1단계: 계획 ──
    def plan(self, question: str, memory: dict | None = None, form: dict | None = None, ask_profile: bool = False) -> Plan:
        """질문 이해 순서. 바꾸면 docs/architecture.md 표랑 tests/test_dialog.py도 같이 고쳐야 함

        1. LLM 대화 해석(_llm_turn) → 2. 되물은 질문의 답 → 3. 규칙 해석 → 4. 버튼 되묻기
        5. LLM 한 문장 해석(첫 질문) → 6. 폼 값 → 7. 이어받기 → 8. 조건 되묻기 → 9. 기록
        """
        memory = {} if memory is None else memory   # 빈 dict여도 같은 객체 써야 기록이 남음
        q = question.strip()
        self.llm.new_question()  # 질문 1개 = plan + 에이전트 판단 + explain 호출 합쳐서 셈
        self._planned = True
        pending = memory.pop("pending", None) if not form else None
        q_in = q
        req, parser, dialog = None, "rule", None
        if not form:   # 자료에 없는 장소 물어보기는 LLM·규칙 해석 전에 막음 (지역 찾기로 코스 덮으면 안 됨)
            req = self._unknown_place(q, memory)
        tried = req is None and self.llm.enabled and not form and (memory.get("last_request") is not None or bool(pending))
        if tried:   # 대화 중: LLM이 먼저 읽고 규칙이 검증 (안 되면 None → 규칙)
            req = self._llm_turn(q, memory, pending)
            if req is not None:
                parser, dialog, q = "llm", "llm", req.question
        if req is None and not form and not pending:
            # 키 없거나 LLM 실패: '화장실은 이제 필요 없어' → 직전 조건에서 그것만 빼고 다시
            req = self._drop_turn(q, memory)
        if req is None:
            q = self._answer_pending(q, pending, memory)   # 되물은 질문에 말로 답했으면 원래 질문이랑 합침
            if (pending and not pending.get("profile") or memory.get("last_request") is not None and not pending) \
                    and q == q_in and self._just_no(q):
                # '어떻게 바꿀까요?' 뒤에 '없어·괜찮아' → 안 바꿈 (답 못하는 질문으로 보내면 안 됨)
                memory["turns"] = (memory.get("turns", []) + [q_in])[-TURNS_KEPT:]
                return self._make_plan(AnalysisRequest(question=q, intent=Intent.UNSUPPORTED, params={
                    "edit": ask("알겠어요. 코스는 그대로 둘게요. 바꾸고 싶은 게 생기면 말씀해 주세요.")}), "rule")
            req = self._edit_parse(q, memory) or self._rule_parse(q, memory)
            if not form and self._unclear(q, req, memory):   # 규칙이 못 읽은 이어 묻기 → 버튼으로 되물음
                req, dialog = self._dialog(q, req, memory)
        if self.llm.enabled and not tried and not req.params.get("edit") and dialog is None \
                and not req.params.get("dropped"):   # 맥락 없는 질문: 한 문장 해석
            sys_prompt = (PROMPT_DIR / "orchestrator_plan.md").read_text(encoding="utf-8").replace(
                "{regions}", ", ".join(sorted(set(self.regions().values()))))
            got, _ = self.llm.chat_json(sys_prompt, q, LLMPlan, purpose="plan")
            if got is not None:
                req = self._merge(req, got, memory, said=q_in)
                parser = "llm"
        if not form and parser != "llm" and (gone := parse_dropped(q_in)):
            # 규칙 해석일 때: '아니라고' 한 조건은 이어받았어도 뺌. LLM 쪽은 dropped로 이미 반영됨
            req = req.model_copy(update={"profile": drop_conditions(req.profile, gone)})
        if form:
            req = self._apply_form(req, form)
            parser = "form"
        elif ask_profile:
            if parser != "llm":   # LLM은 지금 조건 보고 이미 판단함 (다시 붙이면 '나 혼자 가'를 덮어버림)
                req = self._inherit_profile(q, req, memory)   # 앞에서 말한 조건은 다시 안 묻고 이어받음
            req = self._inherit_course(req, memory)       # '코스에 식당도 넣어줘' → 지금 코스 그대로
            req = self._anchor_nearby(req, memory)        # '주변 식당' → 지금 코스 장소 근처
            if self._needs_profile(q, req, memory):
                memory["profile_asked"] = True   # 대화에서 한 번만 물음
                req = AnalysisRequest(question=q, intent=Intent.UNSUPPORTED, params={"edit": profile_question(q)})
        if req.question != q_in:   # 분석은 앞 대화로 읽은 문장으로, 화면엔 실제로 한 말(said)도 같이 보여 줌
            req = req.model_copy(update={"params": {**req.params, "said": q_in}})
        edit = req.params.get("edit") or {}
        if not form and edit.get("kind") == "ask" and (edit.get("choices") or edit.get("for")):   # 되물었으면 다음 말을 그 답으로 봄
            memory["pending"] = {"q": q, "profile": edit.get("for") == "profile", "day": edit.get("day")}
        if not form:
            memory["turns"] = (memory.get("turns", []) + [q_in])[-TURNS_KEPT:]
        return self._make_plan(req, parser)

    # ── 대화 이어가기 ──
    def _drop_turn(self, q: str, memory: dict) -> AnalysisRequest | None:
        """이번 말이 지금 조건 일부를 '아니라고' 하면 ('화장실은 이제 필요 없어') 그것만 빼고 다시 물음
        일수를 새로 말했으면 None
        """
        last: AnalysisRequest | None = memory.get("last_request")
        if last is None or last.intent not in (Intent.ACCESS, Intent.COURSE, Intent.NEARBY) or parse_days(q) is not None:
            return None
        lp = last.profile.model_dump()
        gone = {k: [v for v in vals if (v in lp[k] if isinstance(lp[k], list) else lp[k] == v)]
                for k, vals in parse_dropped(q).items()}
        gone = {k: v for k, v in gone.items() if v}
        if not gone:
            return None
        found, _ = parse_profile(q)
        for k, v in found.items():
            lp[k] = list(dict.fromkeys(lp[k] + v)) if k in ("needs", "interests") else v
        prof = drop_conditions(TravelerProfile(**lp), gone)
        labels = list(dict.fromkeys(DROP_KO.get(v, v) for vals in gone.values() for v in vals
                                    if not (v == "wheelchair_power" and "wheelchair_manual" in vals)))
        req = last.model_copy(update={"question": q, "profile": prof,
                                      "params": {**_carried(last), "follow_up": True, "dropped": labels}})
        return self._inherit_course(req, memory)

    def _answer_pending(self, q: str, pending: dict | None, memory: dict) -> str:
        """직전에 되물었고 이번 말이 그 답이면 원래 질문에 붙여서 돌려줌. 아니면 q 그대로"""
        if not pending:
            return q
        own = has_day_ref(q) or parse_days(q) is not None or any(
            _has(q, k) for k in ("course", "status", "report", "nearby", "unsupported"))
        if pending.get("profile"):
            _, found = parse_profile(q)
            if found and not own:
                return f"{pending['q']} ({q})"
            if not found and not own and len(q) <= 40 and NO_ANSWER_RE.search(q):
                return f"{pending['q']} (불편한 점 없음)"
            return q
        day = pending.get("day")
        if day and not has_day_ref(q) and not own:   # 'N일차를 어떻게 바꿀까요?' → '세 곳으로' = 'N일차 세 곳으로'
            cand = f"{day}일차 {q}"
            try:
                places = self.repo.get("places").df[["place_id", "name"]]
            except DataNotReady:
                return q
            e = parse_edit(cand, memory, places)
            if e is not None and e["kind"] != "ask":
                return cand
        return q

    @staticmethod
    def _just_no(q: str) -> bool:
        """'없어', '괜찮아', '아니요'처럼 짧게 아니라고만 한 말"""
        return len(q) <= 40 and bool(NO_ANSWER_RE.search(q)) and not has_day_ref(q) and parse_days(q) is None \
            and not any(_has(q, k) for k in ("course", "status", "report", "nearby", "unsupported"))

    @staticmethod
    def _no_conditions(p: TravelerProfile) -> bool:
        return p.mobility == "none" and p.vision == "none" and p.hearing == "none" and p.cognition == "none" \
            and not [n for n in p.needs if n != "indoor_pref"]

    def _inherit_profile(self, q: str, req: AnalysisRequest, memory: dict) -> AnalysisRequest:
        """조건 없이 물었는데 앞 대화에 조건 있으면 이어받음 ('조건 없이'라고 하면 안 함)"""
        last: AnalysisRequest | None = memory.get("last_request")
        if (last is None or req.intent not in (Intent.ACCESS, Intent.COURSE, Intent.NEARBY) or req.params.get("edit")
                or req.params.get("dropped")   # 조건 빼 달라는 말이면 안 이어받음 (이미 뺐음)
                or not self._no_conditions(req.profile) or self._no_conditions(last.profile) or any(w in q for w in NO_CONDITION)):
            return req
        if (gone := parse_dropped(q)):   # 이어받되 '아니라고' 한 건 뺌
            last = last.model_copy(update={"profile": drop_conditions(last.profile, gone)})
        base, cur, default = last.profile.model_dump(), req.profile.model_dump(), TravelerProfile().model_dump()
        for k, v in cur.items():
            if k in ("needs", "interests"):
                base[k] = list(dict.fromkeys(base[k] + v))
            elif v != default[k]:
                base[k] = v
        return req.model_copy(update={"profile": TravelerProfile(**base), "params": {**req.params, "profile_from_chat": True}})

    @staticmethod
    def _inherit_course(req: AnalysisRequest, memory: dict) -> AnalysisRequest:
        """코스 있는데 일수·지역 말 안 한 코스 질문은 지금 코스에 이어서 짬 (일수·지역·달·출발지·고친 것 유지)
        일수나 지역 말하면 새 코스
        """
        last: AnalysisRequest | None = memory.get("last_request")
        if (last is None or memory.get("last_intent") != "course" or req.intent != Intent.COURSE or req.params.get("edit")
                or req.place_ids or req.params.get("llm_new")):
            return req
        if req.days not in (None, last.days) or req.region not in (None, last.region):   # 일수·지역 바뀌면 새 코스
            return req
        params = {**_carried(last), **req.params, "follow_up": True, "course_from_chat": True}
        kept = memory.get("edits")
        if kept:
            params["edits"] = kept
        it = memory.get("last_itinerary") or []
        same = (req.month in (None, last.month) and req.origin in (None, last.origin)
                and (req.profile == last.profile or req.params.get("profile_from_chat")))
        if same and it:   # 바뀐 조건 없으면 장소는 두고(전부 고정) 식당·숙소만 다시 붙임
            params["keep_days"] = {d: [x["place_id"] for x in it if x["day"] == d] for d in sorted({x["day"] for x in it})}
        return req.model_copy(update={"days": last.days, "region": last.region, "month": req.month or last.month,
                                      "start_date": last.start_date if req.month in (None, last.month) else None,
                                      "origin": req.origin or last.origin, "params": params})

    @staticmethod
    def _anchor_nearby(req: AnalysisRequest, memory: dict) -> AnalysisRequest:
        """기준 장소 없이 '주변 식당·숙소' 물으면 직전 결과 장소 근처로 찾음"""
        ids = memory.get("last_place_ids") or []
        if req.intent != Intent.NEARBY or req.place_ids or req.region is not None or not ids \
                or memory.get("last_intent") not in ("course", "access"):
            return req
        what = "지금 코스 장소" if memory.get("last_intent") == "course" else "직전 결과 장소"
        return req.model_copy(update={"place_ids": list(ids), "params": {**req.params, "anchor_name": what}})

    def _same_as_last(self, req: AnalysisRequest, memory: dict) -> bool:
        """이어 묻기인데 바뀐 게 하나도 없음 (같은 코스 또 짜게 됨)"""
        last: AnalysisRequest | None = memory.get("last_request")
        return (last is not None and not req.params.get("edit") and bool(req.params.get("follow_up"))
                and req.intent.value == memory.get("last_intent")
                and not req.place_ids and (req.region, req.days, req.month, req.origin, req.profile)
                == (last.region, last.days, last.month, last.origin, last.profile))

    def _unclear(self, q: str, req: AnalysisRequest, memory: dict) -> bool:
        """규칙이 이 말을 제대로 못 읽었는지: unparsed · 날은 말했는데 수정으로 못 읽음 · 바뀐 게 없음
        규칙이 되묻기로 정한 것(kind=ask)은 그대로 둠
        """
        if memory.get("last_request") is None or req.params.get("edit"):
            return False
        return bool(req.params.get("unparsed")) or (has_day_ref(q) and memory.get("last_intent") == "course") \
            or self._same_as_last(req, memory)

    def _dialog_context(self, memory: dict, pending: dict | None) -> dict:
        last: AnalysisRequest | None = memory.get("last_request")
        ctx: dict[str, Any] = {"recent_questions": memory.get("turns", [])[-TURNS_KEPT:]}
        if last is not None:
            ctx.update({"conditions": last.profile.describe(), "region": last.region or "제주 전체",
                        "days": last.days, "month": last.month, "last_task": memory.get("last_intent")})
        it = memory.get("last_itinerary") or []
        if memory.get("last_intent") == "course" and it:
            ctx["course"] = [{"day": d, "places": [p["name"] for p in it if p["day"] == d]}
                             for d in sorted({p["day"] for p in it})]
            ctx["removed"] = list(((memory.get("edits") or {}).get("exclude_names") or {}).values())
            last_resp = memory.get("last_response")
            extras = (last_resp.results[-1].payload.get("extras") if last_resp is not None and last_resp.results else None) or []
            ctx["meals_in_course"] = sum(1 for x in extras if x.get("slot") == "점심")
            ctx["lodging_in_course"] = sum(1 for x in extras if x.get("slot") == "숙소")
        if pending:
            ctx["you_asked"] = pending.get("q")
        return ctx

    def _readable(self, cmd: str, memory: dict) -> AnalysisRequest | None:
        """대화 해석이 낸 문장을 규칙으로 다시 읽음. 못 읽거나 같은 결과 반복이면 None"""
        r = self._edit_parse(cmd, memory) or self._rule_parse(cmd, memory)
        if r.params.get("unparsed") or self._same_as_last(r, memory):
            return None
        return r

    def _default_choices(self, memory: dict) -> list[str]:
        it = memory.get("last_itinerary") or []
        days = sorted({p["day"] for p in it})
        out = [f"{d}일차 다른 곳으로 바꿔줘" for d in days][:3]
        if it:
            out.append(f"{it[-1]['name']} 빼줘")
        out.append("모든 날 줄여줘")
        if days and max(days) < 5:
            out.append(f"{max(days) + 1}일로 늘려줘")
        return out

    def _llm_turn(self, q: str, memory: dict, pending: dict | None) -> AnalysisRequest | None:
        """대화 중인 말을 LLM이 먼저 해석 (edit / question / ask). 낸 값은 규칙이 다시 확인하고, 실패하면 None"""
        progress.now("대화 맥락을 읽는 중…")
        sys_prompt = (PROMPT_DIR / "dialog.md").read_text(encoding="utf-8").replace(
            "{regions}", ", ".join(sorted(set(self.regions().values()))))
        user = json.dumps({"context": self._dialog_context(memory, pending), "message": q}, ensure_ascii=False)
        got, _ = self.llm.chat_json(sys_prompt, user, LLMTurn, purpose="dialog")
        if got is None:
            return None

        def mark(r: AnalysisRequest) -> AnalysisRequest:
            return r.model_copy(update={"params": {**r.params, "dialog": {"by": "llm", "type": got.type}}})

        if got.type == "edit":
            r = self._edit_parse((got.command or "")[:100], memory) if got.command else None
            return mark(r) if r is not None else None
        if got.type == "question":
            text = (got.question or q)[:200]
            r = self._edit_parse(text, memory)   # 질문으로 냈어도 수정 규칙이 읽으면 고치기로 처리
            if r is None:
                r = self._rule_parse(text, memory)
                if got.plan is not None:
                    # 조건 = LLM 값 + (이어서 하는 말이면) 지금 조건. 안전장치는 LLM 문장 말고 이용자가 한 말로 봄
                    last = memory.get("last_request")
                    base = last.profile if last is not None and got.plan.follow_up is not False else None
                    r = self._merge(r, got.plan, memory, said=q, base=base)
                    if got.plan.follow_up is False:   # '새로 짜줘'면 지금 코스 안 이어받음
                        r = r.model_copy(update={"params": {**r.params, "llm_new": True}})
                elif r.params.get("unparsed"):
                    return None
            return mark(r)
        if got.ask:
            course = memory.get("last_intent") == "course"
            ok = [c[:80] for c in got.choices[:4] if self._readable(c[:80], memory) is not None]
            msg = re.sub(r"\s+", " ", got.ask)[:150]
            return mark(AnalysisRequest(question=q, intent=Intent.UNSUPPORTED,
                                        params={"edit": ask(msg, ok or (self._default_choices(memory) if course and got.choices else []))}))
        return None

    def _unknown_place(self, q: str, memory: dict) -> AnalysisRequest | None:
        """'성산일출봉 화장실 있어?'처럼 장소 이름 + 시설 + 묻는 말인데 그 장소가 자료에 없으면 그렇다고 답함
        지금 결과는 안 바꿈. 이름에 지역(성산)이 들어 있으면 그 읍면동의 자료 장소를 고를 답으로 줌
        """
        hit = place_like(q)
        if hit is None or _has(q, "nearby") or _has(q, "course"):
            return None
        name, fac = hit
        aliases = self.regions()
        if name in aliases:
            return None
        try:
            pl = self.repo.get("places").df
        except DataNotReady:
            return None
        if match_places(q, pl[["place_id", "name"]]):
            return None
        word = FAC_WORDS[fac][0]
        area = next((aliases[a] for a in sorted(aliases, key=len, reverse=True) if len(a) >= 2 and a in name), None)
        near = pl[pl["emd"] == area]["name"].head(3).tolist() if area and "emd" in pl else []
        msg = f"‘{name}’{josa(name, '은')[len(name):]} 무장애 조사 자료에 없어서 {word} 정보를 확인할 수 없어요."
        if memory.get("last_request") is not None:
            msg += " 지금 결과는 그대로 둘게요."
        if near:
            msg += f" {area}에서 자료가 있는 곳은 {', '.join(near)} 등이에요."
        return AnalysisRequest(question=q, intent=Intent.UNSUPPORTED,
                               params={"edit": ask(msg, [f"{n} {word} 있어?" for n in near]), "unknown_place": name})

    def _dialog(self, q: str, req: AnalysisRequest, memory: dict) -> tuple[AnalysisRequest, str | None]:
        """키 없거나 LLM 해석 실패했을 때, 규칙이 못 읽은 이어 묻기 처리"""
        course = memory.get("last_intent") == "course"
        # 키 없을 때: 코스 고치려는 말로 보일 때만 버튼으로 되물음 (날 언급·'바꿔/다시/별로'·같은 코스 반복)
        # 상관없어 보이는 말은 원래대로 답 못하는 질문으로 둠
        if course and (has_day_ref(q) or any(w in q for w in REDO + ("다시",)) or self._same_as_last(req, memory)):
            return AnalysisRequest(question=q, intent=Intent.UNSUPPORTED, params={"edit": ask(
                "코스의 어떤 부분을 바꿀까요? 아래에서 고르시거나 '2일차 세 곳으로', '○○ 빼줘'처럼 말씀해 주세요.",
                self._default_choices(memory))}), "rule"
        return req, None

    @staticmethod
    def _needs_profile(q: str, req: AnalysisRequest, memory: dict) -> bool:
        """장소·코스 질문인데 이동·감각 조건이랑 필요 시설이 하나도 없고, 아직 안 물었으면 True"""
        if memory.get("profile_asked") or req.intent not in (Intent.ACCESS, Intent.COURSE):
            return False
        if req.params.get("follow_up") or req.params.get("edit") or req.place_ids or req.params.get("profile_said"):
            return False
        p = req.profile
        health = [n for n in p.needs if n != "indoor_pref"]
        return p.mobility == "none" and p.vision == "none" and p.hearing == "none" and p.cognition == "none" and not health \
            and not any(w in q for w in NO_CONDITION)

    def plan_form(self, question: str, form: dict, extra: str | None = None) -> Plan:
        """화면 폼 값으로 바로 계획 만듦 (LLM 안 거침). '직접 적은 말'(extra)만 해석해서 더함"""
        prof = form.get("profile") or {}
        if extra and extra.strip():
            self.llm.new_question()   # 이 질문 호출 수·비용을 여기서부터 셈 (execute에서 또 세면 안 됨)
            self._planned = True
            prof = self.form_extra(extra.strip(), prof if isinstance(prof, TravelerProfile) else TravelerProfile(**prof))
        origin = form.get("origin")
        start = form.get("start_date")
        month = int(start[5:7]) if start else form.get("month")
        params: dict[str, Any] = {"opts": form["opts"]} if form.get("opts") else {}
        if form.get("extras") is not None and form.get("intent") == Intent.COURSE.value:
            params["extras_pick"] = [x for x in EXTRA_PICKS if x in form["extras"]]
        req = AnalysisRequest(question=question, intent=Intent(form.get("intent", "access")), region=_region_arg(form.get("region")),
                              days=form.get("days"), month=month, start_date=start, origin=None if origin in _WHOLE_ISLAND else origin,
                              profile=prof if isinstance(prof, TravelerProfile) else TravelerProfile(**prof), params=params)
        return self._make_plan(req, "form")

    def form_extra(self, extra: str, prof: TravelerProfile) -> TravelerProfile:
        """화면 '직접 적은 말' → 버튼 조건에 더함. 버튼 값이 우선"""
        default = TravelerProfile().model_dump()
        rule, _ = parse_profile(extra)
        found = TravelerProfile(**{k: v for k, v in rule.items() if k in default})
        if self.llm.enabled:
            sys_prompt = (PROMPT_DIR / "orchestrator_plan.md").read_text(encoding="utf-8").replace(
                "{regions}", ", ".join(sorted(set(self.regions().values()))))
            got, _ = self.llm.chat_json(sys_prompt, extra, LLMPlan, purpose="plan")
            if got is not None:
                tmp = AnalysisRequest(question=extra, intent=Intent.ACCESS, profile=found)
                found = self._merge(tmp, got, {}, said=extra).profile
        p, f = prof.model_dump(), found.model_dump()
        for k in ("mobility", "vision", "hearing", "cognition", "transport"):
            if p[k] == default[k] and f[k] != default[k]:
                p[k] = f[k]
        for k in ("needs", "interests"):
            p[k] = list(dict.fromkeys(p[k] + f[k]))
        if p["max_ride_min"] is None and f["max_ride_min"]:
            p["max_ride_min"] = f["max_ride_min"]
        if p["companions"] == 1 and f["companions"] > 1:
            p["companions"] = f["companions"]
        return TravelerProfile(**p)

    def _edit_parse(self, q: str, memory: dict) -> AnalysisRequest | None:
        """수정 대화(코스 고치기·장소 하나 물어보기)면 요청 만듦. 아니면 None
        직전 조건 그대로 두고 바꿀 것만 바꿈. 뺀 장소·줄인 날은 다음 수정에도 유지
        """
        try:
            places = self.repo.get("places").df[["place_id", "name"]]
        except DataNotReady:
            return None
        edit = parse_edit(q, memory, places)
        if edit is None or (edit["kind"].startswith("lighter") and parse_days(q) is not None):  # '2일로 줄여줘'는 일수 변경임
            return None
        last: AnalysisRequest | None = memory.get("last_request")
        if edit["kind"] == "place_q" and _has(q, "nearby"):   # '○○ 근처 화장실 있는 식당'은 장소 하나 물어보기 아님
            return None
        if edit["kind"] == "ask":
            return AnalysisRequest(question=q, intent=Intent.UNSUPPORTED, params={"edit": edit})
        if edit["kind"] == "place_q":
            prof = last.profile if last is not None else TravelerProfile(**parse_profile(q)[0])
            return AnalysisRequest(question=q, intent=Intent.ACCESS, place_ids=[edit["place_id"]], profile=prof,
                                   params={"edit": edit, "place_q": edit["facility"]})
        assert last is not None
        base = last.profile.model_dump()
        # 이용자 조건은 '같이 가' 요청에서만 읽음 ('사려니숲길 빼줘'의 '숲'을 관심사로 읽으면 안 됨)
        prof = parse_profile(q)[0] if edit["kind"] == "join" else {}
        if edit["kind"] == "join":
            base["companions"] = min(20, base["companions"] + edit["n"])
            if base["mobility"] != "none":   # 동행인 말('할머니도 같이 가')이 본인 이동 조건을 덮으면 안 됨
                prof.pop("mobility", None)
            prof.pop("companions", None)
        for k in ("needs", "interests"):
            if k in prof:
                prof[k] = list(dict.fromkeys(base[k] + prof[k]))
        prev = memory.get("last_itinerary") or []
        edits = apply_edit(edit, memory.get("edits"), prev)
        return AnalysisRequest(question=q, intent=Intent.COURSE, region=last.region, days=last.days, month=last.month,
                               start_date=last.start_date, origin=last.origin, profile=TravelerProfile(**{**base, **prof}),
                               params={**_carried(last), "edit": edit, "edits": edits, "follow_up": True, "prev_itinerary": prev,
                                       "keep_days": keep_days(edit, prev, last.days or 1)})

    def _rule_parse(self, q: str, memory: dict) -> AnalysisRequest:
        aliases = self.regions()
        origin, q_rest = parse_origin(q, aliases)      # '노형동에서 출발해서 서귀포 코스' → 출발 노형동, 지역 서귀포시
        region = None
        for alias, std in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
            if alias in q_rest:
                region = std
                break
        # 'A 말고 B'(버스 말고 택시)는 B만 읽음
        prof, found = parse_profile(q.split("말고", 1)[1] if "말고" in q else q)
        days, month = parse_days(q), parse_month(q)
        follow = _has(q, "follow_up")
        last: AnalysisRequest | None = memory.get("last_request")

        if _has(q_rest, "unsupported"):     # '애월에 숙소 잡았어'의 숙소는 출발지 표현이라 거절 낱말 아님
            intent = Intent.UNSUPPORTED
        elif _has(q, "report"):
            intent = Intent.REPORT
        elif _has(q, "course") or days is not None:   # '2일 코스, 숙소도'는 코스에서 식당·숙소 같이 넣음
            intent = Intent.COURSE
        elif _has(q_rest, "nearby"):   # 출발지 표현은 q_rest에서 빠져 있음
            intent = Intent.NEARBY
        elif _has(q, "status"):
            intent = Intent.STATUS
        elif _has(q, "access"):
            intent = Intent.ACCESS
        elif follow and memory.get("last_intent"):   # "다시 버스로"처럼 조건만 바꾼 이어 묻기는 직전 분석 그대로
            intent = Intent(memory["last_intent"])
        elif found:
            intent = Intent.ACCESS
        else:
            intent = Intent.UNSUPPORTED
        unparsed = intent == Intent.UNSUPPORTED and not _has(q_rest, "unsupported")   # 거절 낱말이 아니라 못 읽은 것

        profile = TravelerProfile(**prof)
        place_ids: list[str] = []
        start_date = None
        if follow and last is not None:  # 이어 묻기: 직전 조건 이어받고 새로 말한 것만 바꿈
            region = region or last.region
            days = days or last.days
            start_date = last.start_date if month in (None, last.month) else None   # 다른 달 말하면 출발일은 버림
            month = month or last.month
            origin = origin or last.origin
            base = last.profile.model_dump()
            for k in ("needs", "interests"):
                if k in prof:
                    prof[k] = list(dict.fromkeys(base[k] + prof[k]))
            profile = TravelerProfile(**{**base, **prof})
            if any(w in q for w in ("그중", "그 중", "이 장소", "이 곳들", "이곳들")):
                place_ids = memory.get("last_place_ids", [])
        params: dict[str, Any] = {"follow_up": follow, "parsed": found, "unparsed": unparsed}
        if follow and last is not None:
            params.update(_carried(last))
        if intent == Intent.COURSE and (want := parse_meal_wants(q)):   # '코스에 점심·저녁 식당, 숙소도 넣어줘'
            params["want_extras"] = want
        if intent == Intent.NEARBY:
            params["nearby_kinds"] = parse_nearby_kinds(q)
            if not place_ids:   # '○○ 근처'면 ○○가 기준 장소
                try:
                    hit = match_places(q, self.repo.get("places").df[["place_id", "name"]])
                except DataNotReady:
                    hit = []
                if hit:   # '사려니숲길'처럼 여러 곳 맞으면 다 기준 장소로 봄
                    place_ids = [h["place_id"] for h in hit]
                    params["anchor_name"] = hit[0]["name"] if len(hit) == 1 else hit[0]["name"] + f" 등 {len(hit)}곳"
            # 근처 가게 찾기엔 관심사 안 씀 ('이호테우해변 근처'의 '해변'은 관심사 아님)
            profile = profile.model_copy(update={"interests": []})
        kept = memory.get("edits") or {}
        if follow and intent == Intent.COURSE and kept:
            # 조건만 바꾼 이어 묻기도 지금까지 고친 것(뺀 장소·줄인 날·속도) 유지
            # 일수 줄였으면 없어진 날의 '줄인 날'만 버림
            n = days or 1
            params["edits"] = {"exclude": list(kept.get("exclude", [])), "exclude_names": dict(kept.get("exclude_names", {})),
                               "day_caps": {int(d): c for d, c in (kept.get("day_caps") or {}).items() if int(d) <= n},
                               "pace": kept.get("pace")}
        return AnalysisRequest(question=q, intent=intent, region=region, days=days, month=month, profile=profile,
                               origin=origin, start_date=start_date, place_ids=place_ids, params=params)

    def _merge(self, req: AnalysisRequest, got: LLMPlan, memory: dict, said: str | None = None,
               base: TravelerProfile | None = None) -> AnalysisRequest:
        """LLM 해석이 먼저고 규칙은 검증만
        - LLM 값이 범위 안이면 그대로, null인 칸만 base 값
        - 이용자가 직접 말한 이동·감각 조건, 필요 시설은 LLM이 빠뜨려도 지킴
        - 못 찾는 시설('근처 병원')만 물었으면 답 못함으로 둠
        """
        valid_regions = set(self.regions().values())
        keep = req.intent == Intent.NEARBY and got.intent == Intent.UNSUPPORTED
        out_of_scope = (got.intent == Intent.NEARBY and req.intent == Intent.UNSUPPORTED and not req.params.get("unparsed")
                        and not parse_nearby_kinds(said if said is not None else req.question))
        upd: dict[str, Any] = {"intent": req.intent if keep or out_of_scope else got.intent}
        if got.region in valid_regions:
            upd["region"] = got.region
        day_ref_only = has_day_ref(req.question) and parse_days(req.question) is None   # '1일차·둘째 날'은 일수 아님
        if got.days and 1 <= got.days <= 5 and not day_ref_only:
            upd["days"] = got.days
        if got.month and 1 <= got.month <= 12:
            upd["month"] = got.month
        if got.origin in valid_regions or got.origin == AIRPORT:
            upd["origin"] = got.origin
        start = (base or req.profile).model_dump()
        prof = dict(start)
        dropped = set(got.dropped or [])
        guard, _ = parse_profile(said if said is not None else req.question)
        default = TravelerProfile().model_dump()
        check: list[str] = []
        for k in ("mobility", "vision", "hearing", "cognition", "transport"):
            v = getattr(got, k)
            if v is not None:
                try:
                    TravelerProfile(**{**prof, k: v})
                    prof[k] = v
                except ValidationError:
                    pass
            if prof[k] in dropped:
                prof[k] = default[k]
            g = guard.get(k)
            if k != "transport" and g and prof[k] == default[k]:   # LLM이 빠뜨렸거나 잘못 뺀 안전 조건
                prof[k] = g
                if g in dropped:
                    check.append(DROP_KO.get(g, g))
        if base is not None:   # 대화 중엔 지금 조건 없애려면 LLM이 dropped를 내야 함 (빈칸을 none으로 채운 거랑 구별)
            for k in ("mobility", "vision", "hearing", "cognition"):
                if start[k] != default[k] and prof[k] == default[k] and start[k] not in dropped:
                    prof[k] = start[k]
        allowed = set(get_args(Need))
        needs = [n for n in got.needs if n in allowed] if got.needs is not None else list(start["needs"])
        if base is not None:
            needs += [n for n in start["needs"] if n in SAFETY_NEEDS and n not in needs and n not in dropped]
        needs = [n for n in needs if n not in dropped]
        for n in guard.get("needs", []):   # 몸 상태 때문에 필요한 시설만 지킴 (실내 선호는 취향이라 LLM대로)
            if n in SAFETY_NEEDS and n not in needs:
                needs.append(n)
                if n in dropped:
                    check.append(DROP_KO.get(n, n))
        prof["needs"] = list(dict.fromkeys(needs))
        if got.interests is not None:
            prof["interests"] = [i for i in dict.fromkeys(got.interests) if i in get_args(Interest)]
        prof["interests"] = [i for i in prof["interests"] if i not in dropped]
        if got.companions and 1 <= got.companions <= 20:
            prof["companions"] = got.companions
        if got.max_ride_min and 10 <= got.max_ride_min <= 180:
            prof["max_ride_min"] = got.max_ride_min
        upd["profile"] = TravelerProfile(**prof)
        params = {**req.params, "follow_up": bool(req.params.get("follow_up") or got.follow_up)}
        # 뺀 조건: 대화 중이면 지금 조건에서 없어진 것, 첫 질문이면 LLM이 dropped로 낸 것
        was_gone = (lambda v: True) if base is not None else (lambda v: v in dropped)
        gone = [DROP_KO.get(start[k], start[k]) for k in ("mobility", "vision", "hearing", "cognition")
                if start[k] != default[k] and prof[k] != start[k] and was_gone(start[k])]
        gone += [DROP_KO.get(n, n) for n in start["needs"] if n not in prof["needs"] and was_gone(n)]
        senses = {k: getattr(got, k) for k in ("mobility", "vision", "hearing", "cognition")}
        last = memory.get("last_request")
        changed_to_none = last is not None and any(v == "none" and getattr(last.profile, k) != "none" for k, v in senses.items())
        if any(v not in (None, "none") for v in senses.values()) or gone or changed_to_none:
            params["profile_said"] = True   # 몸 상태를 말했음 → 조건 되묻기 안 함
        if gone:
            params["dropped"] = list(dict.fromkeys(gone))
        if check:
            params["check"] = list(dict.fromkeys(check))
        upd["params"] = params
        return req.model_copy(update=upd)

    def _apply_form(self, req: AnalysisRequest, form: dict) -> AnalysisRequest:
        upd: dict[str, Any] = {}
        prof = form.get("profile")
        if prof is not None:
            upd["profile"] = prof if isinstance(prof, TravelerProfile) else TravelerProfile(**prof)
        for k in ("days", "month", "region", "origin", "start_date"):
            if k in form:
                upd[k] = _region_arg(form[k]) if k == "region" else form[k]
        if form.get("intent"):
            upd["intent"] = Intent(form["intent"])
        elif req.intent == Intent.UNSUPPORTED and not _has(req.question, "unsupported"):
            upd["intent"] = Intent.COURSE if (form.get("days") or req.days) else Intent.ACCESS
        return req.model_copy(update=upd)

    def _make_plan(self, req: AnalysisRequest, parser: str) -> Plan:
        parts = [req.region or "제주 전체", INTENT_KO[req.intent]]
        if req.intent == Intent.NEARBY:
            if req.params.get("anchor_name"):
                parts[0] = f"{req.params['anchor_name']} 근처"
            kinds = req.params.get("nearby_kinds") or []
            if kinds:
                parts[1] = "근처 " + "·".join(KIND_KO[k] for k in kinds) + " 찾기"
        if req.intent in (Intent.ACCESS, Intent.COURSE, Intent.NEARBY):
            parts += req.profile.describe() or ["이동 불편 없음"]
        if req.intent == Intent.COURSE:
            parts.append(f"{req.days or 1}일")
        if req.start_date and req.intent == Intent.COURSE:
            parts.append(f"{date_label(req.start_date)} 출발")
        elif req.month is not None:
            parts.append(f"{req.month}월")
        if req.origin and req.intent == Intent.COURSE:
            parts.append(f"{req.origin} 출발")
        edit = req.params.get("edit") or {}
        if edit.get("kind") == "place_q":
            parts = [edit["name"], f"{FAC_LABEL.get(edit['facility'], edit['facility'])} 확인"]
        elif req.place_ids and req.intent != Intent.NEARBY:   # 근처 질문의 place_ids는 기준 장소임
            parts.append(f"직전 결과 {len(req.place_ids)}곳 대상")
        if edit and edit.get("kind") not in ("place_q", "ask"):
            parts.insert(0, "직전 코스 고치기: " + EDIT_KO[edit["kind"]](edit))
        if req.params.get("dropped"):
            parts.append("뺀 조건: " + ", ".join(req.params["dropped"]))
        if req.params.get("check"):
            parts.append("확인: " + ", ".join(req.params["check"]) + "은(는) 필요하다는 말로 읽었어요(아니면 '필요 없어'라고 말씀해 주세요)")
        if req.params.get("profile_from_chat"):
            parts.append("앞 대화의 조건 이어받음")
        if req.params.get("course_from_chat"):
            parts.append("지금 코스에 이어서")
        refusal = None
        if edit.get("kind") == "ask":   # 수정 대화에서 확실하지 않으면 코스 그대로 두고 되물음
            return Plan(request=req, understood="확인이 필요해요", steps=["무엇을 고칠지 확인"], parser=parser,  # type: ignore[arg-type]
                        refusal_reason=edit["message"])
        if req.intent == Intent.UNSUPPORTED:
            refusal = ("지금 가진 자료(제주 무장애 관광지 조사 자료, 센터 날씨·버스 정류소)로는 근거 있게 답할 수 없는 질문입니다. "
                       "조건에 맞는 장소 찾기, 여행 코스 짜기, 지역별 무장애 관광 현황은 도와드릴 수 있어요. "
                       "운영시간·예약·실시간 상황은 방문 전에 해당 기관에 직접 확인해 주세요.")
        return Plan(request=req, understood=" · ".join(parts) + " — 이렇게 이해했어요. 맞나요?",
                    steps=STEPS[req.intent], parser=parser, refusal_reason=refusal)  # type: ignore[arg-type]

    # ── 2단계: 실행 ──
    def _call_agent(self, name: str, req: AnalysisRequest, memory: dict) -> AgentResult:
        t0 = time.perf_counter()
        # 하위 에이전트도 LLM으로 판단함(규칙 범위 안에서). 다른 에이전트가 부른 호출엔 called_by 붙여서
        # 부른 쪽이 정한 조건으로 규칙 판단하게 함 (LLM 호출이 겹겹이 늘면 안 됨)
        ctx = AgentContext(repo=self.repo, settings=self.s, memory=memory, llm=self.llm,
                           call=lambda n, r: self._call_agent(
                               n, r.model_copy(update={"params": {**r.params, "called_by": name}}), memory))
        result = REGISTRY[name]().run(req, ctx)
        if req.params.get("called_by"):
            self._nested.append(result)
        self._log(TraceStep(agent="orchestrator", action=f"call:{name}", detail=result.status,
                                     elapsed_ms=int((time.perf_counter() - t0) * 1000)))
        return result

    def _log(self, step: TraceStep) -> None:
        """실행 기록 한 줄 + 화면 '분석 과정' 알림"""
        self._trace.append(step)
        progress.emit(step.agent, step.action, step.detail)

    def execute(self, plan: Plan, memory: dict | None = None) -> OrchestratorResponse:
        """단계마다 progress 알림 보내고 같은 줄을 resp.progress에도 남김"""
        lines: list[str] = []
        with progress.listen(lambda st: lines.append(st.text) if st.kind == "done" else None):
            resp = self._execute(plan, memory)
        resp.progress = lines   # memory['last_response']랑 같은 객체라 같이 바뀜
        return resp

    def _execute(self, plan: Plan, memory: dict | None = None) -> OrchestratorResponse:
        memory = memory if memory is not None else {}
        if not self._planned:  # 폼처럼 plan() 없이 바로 실행하면 여기서 셈
            self.llm.new_question()
        self._planned = False
        self._trace = []
        self._log(TraceStep(agent="orchestrator", action="plan", detail=f"{plan.request.intent.value} by {plan.parser}"))
        if plan.request.params.get("dialog") or plan.request.params.get("said"):   # 이용자 말은 안 남기고 무엇으로 읽었는지만 남김
            kind = (plan.request.params.get("edit") or {}).get("kind") or plan.request.intent.value
            by = (plan.request.params.get("dialog") or {}).get("by", "rule")
            self._log(TraceStep(agent="orchestrator", action="dialog", detail=f"앞 대화로 읽음({by}) → {kind}"))
        self._nested = []
        self._explored = []
        req = plan.request

        if req.intent == Intent.REPORT:
            last: OrchestratorResponse | None = memory.get("last_response")
            if last is None:
                return OrchestratorResponse(plan=plan, answer="정리할 직전 분석이 없습니다. 먼저 분석을 요청해 주세요.", trace=self._trace,
                                            llm_usage=self.llm.question_usage())
            return OrchestratorResponse(plan=plan, results=last.results, trace=self._trace, verification=last.verification,
                                        llm_usage=self.llm.question_usage(),
                                        answer="직전 분석을 보고서로 정리했습니다. 아래 '보고서·그래프 내려받기'에서 받을 수 있습니다.")
        if req.intent == Intent.UNSUPPORTED:
            return OrchestratorResponse(plan=plan, answer=plan.refusal_reason or "", trace=self._trace,
                                        llm_usage=self.llm.question_usage())   # 되묻기·거절도 해석 호출 비용 셈

        # 데이터 탐색: 분석 전에 쓸 자료 고르고 점검. 분석 에이전트는 이 목록 따름(base.uses)
        explored = self._call_agent("explore", req, memory)
        self._explored = [explored] if explored.status == "ok" else []
        if self._explored:
            req = req.model_copy(update={"params": {**req.params, "datasets": explored.payload["selected"]}})
            plan = plan.model_copy(update={"request": req})

        # 모델이 지원하면 확인 카드 범위 안에서만 function tool 실행
        # 실패하거나 안 맞으면 규칙 실행으로 돌아감
        tool_result, tool_answer, tool_output = self._execute_tool_call(plan, memory)
        if tool_result is not None:
            results = self._support(req) + [tool_result]
            blocked = self._blocked_answer(plan, results, memory)
            if blocked is not None:
                return blocked
            draft = self._template_answer(plan, results)
            # LLM이 tool 결과 보고 답 쓰니까 대조 기준도 초안 + tool 결과로 함
            # 질문 문장은 안 넣음 (질문 속 숫자를 사실처럼 따라 쓰는 걸 막으려고)
            reference = draft + "\n" + tool_output
            problem = answer_problem(tool_answer, reference, _evidence_ids(results))
            if problem is None:
                self._log(TraceStep(agent="orchestrator", action="tool_answer", detail="llm 설명 채택(숫자·근거표 대조 통과)"))
                return self._finish(plan, results, tool_answer.strip(), "llm", memory)
            self._log(TraceStep(agent="orchestrator", action="tool_answer", detail=f"문장틀 사용({problem})"))
            return self._finish(plan, results, draft, "template", memory)

        result = self._call_agent(AGENT_FOR.get(req.intent, req.intent.value), req, memory)
        results = self._support(req) + [result]

        # 근거 부족하면 한계·필요 데이터 안내
        blocked = self._blocked_answer(plan, results, memory)
        if blocked is not None:
            return blocked

        draft = self._template_answer(plan, results)
        answer, source = draft, "template"
        if self.llm.enabled:
            progress.now("LLM이 설명을 쓰는 중…")
            prompt = (PROMPT_DIR / "explain.md").read_text(encoding="utf-8").replace("{draft}", draft)
            text, _ = self.llm.chat_text("한국어로 답한다.", prompt, purpose="explain")
            problem = answer_problem(text, draft, _evidence_ids(results))
            if problem is None:
                answer, source = text.strip(), "llm"
                self._log(TraceStep(agent="orchestrator", action="explain", detail="llm 설명 채택(숫자·근거표 대조 통과)"))
            else:
                self._log(TraceStep(agent="orchestrator", action="explain", detail=f"문장틀 사용({problem})"))
        return self._finish(plan, results, answer, source, memory)

    def _execute_tool_call(self, plan: Plan, memory: dict) -> tuple[AgentResult | None, str | None, str]:
        """확인된 계획 안에서만 LLM function calling 실행
        반환: (분석 결과, LLM 답, tool 결과 JSON). 세 번째는 숫자 대조 기준으로 씀
        """
        if not (self.llm.enabled and self.s.llm_tool_calling) or plan.request.intent not in TOOL_INTENTS.values():
            return None, None, ""   # 근처 식당·숙소는 고를 도구가 없어서 바로 규칙 실행
        req = plan.request
        chosen: dict = {}

        def execute_tool(name: str, arguments: dict) -> dict:
            intent = TOOL_INTENTS.get(name)
            # 확인한 intent랑 다르거나 계획이랑 안 맞는 인자는 실행 안 함
            if intent != req.intent:
                return {"ok": False, "error": "confirmed_intent_mismatch"}
            for field in ("region", "days"):
                if field not in arguments:
                    continue
                got = _region_arg(arguments[field]) if field == "region" else arguments[field]
                want = getattr(req, field) if field == "region" else (req.days or 1)
                if got != want:
                    return {"ok": False, "error": f"confirmed_{field}_mismatch"}
            result = self._call_agent(intent.value, req, memory)
            chosen["result"] = result
            # payload엔 기계용 값이 있어서 LLM엔 안 보냄
            public = {"ok": True, "agent": result.agent, "status": result.status, "summary": result.summary,
                      "findings": [f.model_dump() for f in result.findings],
                      "evidence": [e.model_dump() for e in result.evidence],
                      "warnings": [w.model_dump() for w in result.warnings], "limitations": result.limitations,
                      "confidence": result.confidence, "data_labels": result.data_labels}
            chosen["output"] = json.dumps(public, ensure_ascii=False, default=str)
            return public

        system = (PROMPT_DIR / "analysis_tool.md").read_text(encoding="utf-8")
        user = (f"사용자 질문: {req.question}\n확인된 분석: {plan.understood}\n"
                f"확정 조건: intent={req.intent.value}, region={req.region}, days={req.days or 1}")
        progress.now("LLM이 분석 도구를 고르는 중…")
        run = self.llm.call_tool(system, user, ANALYSIS_TOOLS, execute_tool)
        if run.text is None or "result" not in chosen:
            self._log(TraceStep(agent="orchestrator", action="tool_call", detail=f"fallback:{run.record.error}"))
            return None, None, ""
        self._log(TraceStep(agent="orchestrator", action="tool_call", detail=run.called_tool or "unknown"))
        return chosen["result"], run.text, chosen["output"]

    def _support(self, req: AnalysisRequest) -> list[AgentResult]:
        """앞에 붙는 결과: 데이터 탐색, (코스면) 접근성 진단. 주 결과는 항상 마지막(results[-1])"""
        if req.intent != Intent.COURSE:
            return list(self._explored)
        acc = [r for r in self._nested if r.agent == "access" and r.status in ("ok", "partial")]
        return list(self._explored) + acc[-1:]

    def _blocked_answer(self, plan: Plan, results: list[AgentResult], memory: dict) -> OrchestratorResponse | None:
        """근거 없거나 오류뿐이면 tool·설명 호출 없이 같은 안내로 끝냄"""
        if not results or results[-1].status not in ("insufficient", "error"):
            return None
        self._log(TraceStep(agent="orchestrator", action="verify", detail="근거 부족"))
        # 탐색은 요약 대신 근거표 D1로 (탐색 요약 숫자가 답에 섞이면 안 됨)
        answer = "분석에 필요한 근거가 부족합니다.\n" + "\n".join(f"- {r.summary}" for r in results if r.agent != "explore")
        if any(r.agent == "explore" and any(e.id == "D1" for e in r.evidence) for r in results):
            answer += "\n- 살펴본 자료는 [근거표 D1]에 있습니다."
        return self._finish(plan, results, answer, "template", memory)

    def ask(self, question: str, memory: dict | None = None, form: dict | None = None) -> OrchestratorResponse:
        memory = memory if memory is not None else {}
        return self.execute(self.plan(question, memory, form), memory)

    def _finish(self, plan, results, answer, source, memory) -> OrchestratorResponse:
        # 검증 에이전트: 분석한 에이전트랑 따로 최종 답·결과 다시 확인
        progress.now("검증 중…")
        draft = self._template_answer(plan, results)
        check = verify(plan, results, answer, draft, self._trace)
        if source == "llm" and answer_failed(check):
            bad = [c.name for c in check.checks if not c.ok]
            self._log(TraceStep(agent="verifier", action="revert", detail=f"LLM 설명 대신 문장틀({', '.join(bad)})"))
            answer, source = draft, "template"
            check = verify(plan, results, answer, draft, self._trace)
        self._log(TraceStep(agent="verifier", action="verify",
                                     detail=f"{sum(c.ok for c in check.checks)}/{len(check.checks)} 통과"))
        resp = OrchestratorResponse(plan=plan, results=results, answer=answer, answer_source=source,  # type: ignore[arg-type]
                                    trace=self._trace, llm_usage=self.llm.question_usage(), verification=check)
        ok = [r for r in results if r.status in ("ok", "partial")]
        if (plan.request.params.get("edit") or {}).get("kind") == "place_q":
            ok = []   # 장소 하나 물어보기는 직전 코스를 안 덮음 (이어서 '2일차 빡빡해'도 되게)
        if ok and plan.request.intent == Intent.COURSE:
            memory["last_itinerary"] = ok[-1].payload.get("itinerary", [])
            memory["edits"] = plan.request.params.get("edits")   # 새 코스 질문이면 None → 쌓인 수정 비움
        if ok:
            memory["last_response"] = resp
            memory["last_intent"] = plan.request.intent.value
            memory["last_request"] = plan.request
            p = ok[-1].payload
            memory["last_place_ids"] = p.get("place_ids") or p.get("shown_ids") or []
        return resp

    @staticmethod
    def _template_answer(plan: Plan, results: list[AgentResult]) -> str:
        lines = []
        if plan.refusal_reason:
            lines.append(plan.refusal_reason)
        results = [r for r in results if r.agent != "explore"]   # 탐색은 화면 '분석 과정'이랑 근거표 D1·D2로 보임
        for i, r in enumerate(results):
            lines.append(r.summary)
            if i < len(results) - 1:   # 앞에 붙은 접근성 진단은 한 줄 요약만
                continue
            for f in r.findings:
                refs = " ".join(f"[근거표 {e}]" for e in f.evidence_ids)
                lines.append(f"- {f.text} {refs}".rstrip())
            for w in r.warnings:
                if w.severity != "info":
                    lines.append(f"- 주의: {w.message}")
            if r.limitations:
                lines.append(f"- 한계: {r.limitations[0]}")
        return "\n".join(lines)
