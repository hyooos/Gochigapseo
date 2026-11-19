"""에이전트끼리 주고받는 입출력 규격
필드 더할 땐 기본값 꼭 주고, 지우거나 이름 바꾸면 안 됨
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DataLabel = Literal["가상", "실데이터"]


class Intent(str, Enum):
    ACCESS = "access"            # 조건에 맞는 장소 찾기(적합·주의·부적합)
    COURSE = "course"            # 날짜별 코스
    STATUS = "status"            # 지역별 무장애 관광 인프라 현황 (기관·정책용)
    REPORT = "report"            # 직전 결과를 보고서로
    UNSUPPORTED = "unsupported"  # 근거 데이터 없는 질문 → 거절
    NEARBY = "nearby"            # 근처 식당·카페·숙소. access 에이전트가 처리


Mobility = Literal["none", "wheelchair_manual", "wheelchair_power", "walker", "senior", "stroller"]
Vision = Literal["none", "low", "blind"]
Hearing = Literal["none", "hard", "deaf"]
Cognition = Literal["none", "intellectual"]   # 발달·지적장애 (rec_intellectual 열)
Need = Literal["acc_toilet", "acc_parking", "rest_often", "indoor_pref"]
Transport = Literal["car", "taxi", "bus"]
Interest = Literal["sea", "forest", "museum", "village"]

MOBILITY_KO: dict[str, str] = {"none": "이동 불편 없음", "wheelchair_manual": "수동 휠체어", "wheelchair_power": "전동 휠체어",
                               "walker": "지팡이·보행보조기", "senior": "고령·쉽게 지침", "stroller": "유아차 동반"}
VISION_KO: dict[str, str] = {"none": "시각 불편 없음", "low": "저시력", "blind": "시각장애(전맹)"}
HEARING_KO: dict[str, str] = {"none": "청각 불편 없음", "hard": "난청", "deaf": "청각장애"}
COGNITION_KO: dict[str, str] = {"none": "인지·발달 불편 없음", "intellectual": "발달·지적장애"}
NEED_KO: dict[str, str] = {"acc_toilet": "장애인 화장실 필요", "acc_parking": "장애인 주차 필요",
                           "rest_often": "자주 쉬어야 함", "indoor_pref": "실내 선호"}
TRANSPORT_KO: dict[str, str] = {"car": "자가용·렌터카", "taxi": "택시·교통약자 콜택시", "bus": "버스"}
INTEREST_KO: dict[str, str] = {"sea": "바다·해안", "forest": "숲길·오름·공원", "museum": "박물관·실내 전시", "village": "마을·올레길"}
# 관심사 → 장소 분류(keywords.yaml categories). 식당은 무장애 자료에 없어서 안 받음
INTEREST_CATEGORIES: dict[str, tuple[str, ...]] = {"sea": ("해변", "해안·포구"), "forest": ("숲길", "오름", "공원"),
                                                   "museum": ("실내 전시", "테마파크"), "village": ("마을", "올레길")}
RIDE_OPTIONS = (30, 45, 60, 90)   # 한 번에 이동할 수 있는 시간(분) 선택지


class TravelerProfile(BaseModel):
    """이용자 조건. 폼이나 질문에서 만듦
    건강·장애 정보라 로그·기록·파일에 남기면 안 됨. 화면이랑 계산에만 씀
    """
    mobility: Mobility = "none"
    vision: Vision = "none"
    hearing: Hearing = "none"
    cognition: Cognition = "none"
    needs: list[Need] = Field(default_factory=list)
    transport: Transport = "car"
    companions: int = Field(default=1, ge=1, le=20)      # 함께 가는 사람 수 (본인 포함)
    max_ride_min: int | None = Field(default=None, ge=10, le=180)  # 한 번에 이동할 수 있는 시간(분). 코스에서 절대 안 넘음
    interests: list[Interest] = Field(default_factory=list)       # 관심사 (점수 가산)

    @field_validator("needs", "interests")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    def is_default(self) -> bool:
        return self == TravelerProfile()

    def describe(self) -> list[str]:
        """확인 카드·보고서용 짧은 설명. 기본값(불편 없음)은 뺌"""
        out = []
        if self.mobility != "none":
            out.append(MOBILITY_KO[self.mobility])
        if self.vision != "none":
            out.append(VISION_KO[self.vision])
        if self.hearing != "none":
            out.append(HEARING_KO[self.hearing])
        if self.cognition != "none":
            out.append(COGNITION_KO[self.cognition])
        out += [NEED_KO[n] for n in self.needs]
        if self.transport != "car":
            out.append(TRANSPORT_KO[self.transport] + " 이동")
        if self.companions > 1:
            out.append(f"{self.companions}명")
        if self.max_ride_min is not None:
            out.append(f"한 번에 이동 {self.max_ride_min}분 이내")
        if self.interests:
            out.append("관심: " + "·".join(INTEREST_KO[i] for i in self.interests))
        return out


class AnalysisRequest(BaseModel):
    """질문 해석 결과. 하위 에이전트는 이것만 받음"""
    question: str
    intent: Intent
    region: str | None = None             # "제주시" | "서귀포시" | 읍면동 | None(제주 전체)
    place_ids: list[str] = Field(default_factory=list)   # 이어 묻기: 직전 결과 장소들
    profile: TravelerProfile = Field(default_factory=TravelerProfile)
    days: int | None = Field(default=None, ge=1, le=5)    # 코스 일수
    month: int | None = Field(default=None, ge=1, le=12)  # 여행 달 (날씨 계산용)
    origin: str | None = None             # 출발지(집·숙소) 읍면동 or "제주국제공항". 코스 1일차 출발점
    start_date: str | None = None         # 출발일 "YYYY-MM-DD". 있으면 month는 이 날의 달
    params: dict[str, Any] = Field(default_factory=dict)  # 에이전트별 추가 옵션 (opts, called_by 등)


PlaceKind = Literal["attraction", "restaurant", "cafe", "lodging"]
# nearby.csv 원본 분류 → kind
NEARBY_KIND: dict[str, str] = {"음식점": "restaurant", "카페/편의점": "cafe", "숙박 시설": "lodging"}
KIND_KO: dict[str, str] = {"attraction": "관광지", "restaurant": "식당", "cafe": "카페·편의점", "lodging": "숙소"}


class PlaceItem(BaseModel):
    """access → 코스·화면으로 넘기는 장소 1곳 (관광지·주변 가게 같은 모양, payload["items"])
    주변 장소 번호 "C01-N3"의 3 = nearby.csv 행 순서라 좌표 붙일 때 순서 바꾸면 안 됨
    """
    item_id: str                          # 관광지 = place_id, 주변 장소 = 기준 장소 + 순번 (예: "C01-N3")
    name: str
    kind: PlaceKind
    category: str | None = None           # 원본 분류 그대로
    verdict: Literal["적합", "주의", "부적합"]   # 주의 = 방문 전 확인 필요
    reasons: list[str] = Field(default_factory=list)          # 판정 근거 문장
    facilities: list[dict[str, Any]] = Field(default_factory=list)  # key·label·state·mark·evidence (장소 카드랑 같은 모양)
    phone: str | None = None              # 없으면 None → 화면엔 '정보 없음'
    lat: float | None = None              # 관광공사 식당은 자료 좌표, 데이터허브 가게는 코스에서 카카오로 찾았을 때만 (저장 안 함)
    lng: float | None = None
    anchor_id: str | None = None          # 주변 장소의 기준 place_id. 관광지는 None
    distance_km: float | None = Field(default=None, ge=0)     # 기준 장소에서 거리
    source: str                           # 자료 이름
    data_date: str | None = None          # 조사·기준일
    stale: bool = False                   # 기준일 오래됐는지 (access_rules.yaml stale_years)
    call_script: list[str] = Field(default_factory=list)      # 전화로 물어볼 말


class Plan(BaseModel):
    """실행 전 '이렇게 이해했어요' 카드 + 단계 목록"""
    request: AnalysisRequest
    understood: str                       # 확인 카드 문장
    steps: list[str]                      # 실행 순서
    parser: Literal["rule", "llm", "form"] = "rule"
    refusal_reason: str | None = None     # UNSUPPORTED일 때 이유


class EvidenceTable(BaseModel):
    """[근거표 N]. 집계 결과만 (센터 원본 행·개인정보 금지)"""
    id: str                               # "A1", "C1", "S1" … 문장에서 [근거표 A1]로 씀
    title: str
    dataset: str                          # 쓴 데이터셋 이름
    data_label: DataLabel
    columns: list[str]
    rows: list[list[Any]]
    note: str | None = None


class Finding(BaseModel):
    """해설 문장 1개. 숫자는 numbers에도 넣음 (설명 단계에서 대조)"""
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    numbers: dict[str, float] = Field(default_factory=dict)


class AgentWarning(BaseModel):
    code: str                             # 예: STALE_DATA, INFO_GAP, NO_WEATHER
    message: str
    severity: Literal["info", "warn", "block"] = "warn"


class TraceStep(BaseModel):
    """분석 기록 1줄. 원본 행·건강 정보 넣으면 안 됨 (재현용 정보랑 집계 수만)"""
    agent: str
    action: str
    detail: str = ""
    elapsed_ms: int = 0


class AgentResult(BaseModel):
    agent: str                            # "access" | "course" | "status"
    status: Literal["ok", "partial", "insufficient", "error"]
    summary: str                          # 한 줄 결론 (문장틀로 만듦)
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[EvidenceTable] = Field(default_factory=list)
    warnings: list[AgentWarning] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    limitations: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)      # 이어 묻기 추천 질문
    payload: dict[str, Any] = Field(default_factory=dict)    # 다음 질문·다른 에이전트·화면이 쓰는 기계용 값
    trace: list[TraceStep] = Field(default_factory=list)
    data_labels: list[DataLabel] = Field(default_factory=list)


class VerifyCheck(BaseModel):
    name: str                             # 예: "출처", "정보 없음 표현", "코스 조건"
    ok: bool
    detail: str = ""


class Verification(BaseModel):
    """검증 결과. 화면·보고서에 그대로 보임"""
    passed: bool
    checks: list[VerifyCheck] = Field(default_factory=list)


class LLMUsage(BaseModel):
    calls: int = 0
    cached: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0.0


class OrchestratorResponse(BaseModel):
    plan: Plan
    results: list[AgentResult] = Field(default_factory=list)
    answer: str                           # 이용자한테 보이는 최종 답
    answer_source: Literal["template", "llm"] = "template"
    trace: list[TraceStep] = Field(default_factory=list)
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    verification: Verification | None = None
    progress: list[str] = Field(default_factory=list)   # 화면 '분석 과정' 목록 (trace랑 같은 내용)
