"""설정은 여기 한 곳. os.environ 말고 get_settings()로 읽을 것
우선순위: .env → 환경변수 → 아래 기본값
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"          # 캐시·사용 로그 (git 제외)
OUTPUT_DIR = ROOT / "outputs"           # 내려받는 보고서·PNG (git 제외)
DATA_DIR = ROOT / "data"
PROMPT_DIR = ROOT / "prompts"
CONFIG_DIR = ROOT / "config"


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "on", "yes", "y"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _float(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


REASONING_EFFORTS = ("minimal", "low", "medium", "high")


@dataclass(frozen=True)
class Settings:
    llm_mode: str               # "offline" | "api"
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_max_calls_per_question: int
    llm_monthly_budget_usd: float
    llm_cache: bool
    llm_tool_calling: bool
    jeju_datahub_key: str
    data_mode: str              # "sample" | "real"
    agent_llm: bool = True      # 하위 에이전트도 LLM으로 판단 (규칙 범위 안). off면 규칙만
    agent_max_steps: int = 4    # 하위 에이전트 하나가 질문 하나에서 LLM한테 물을 수 있는 최대 횟수
    app_name: str = "고치갑서"   # 화면 제목 (제주말 '같이 갑시다'). APP_NAME으로 바꿀 수 있음
    stt_mode: str = "off"       # 음성 입력: off | api (키 필요)
    stt_model: str = "gpt-4o-mini-transcribe"
    kakao_js_key: str = ""      # 카카오맵 JS 키 (없으면 기본 지도)
    llm_reasoning_effort: str = ""   # 추론형 모델 생각 깊이: minimal|low|medium|high. 빈칸이면 안 보냄
    kakao_rest_key: str = ""    # 카카오 REST 키: 이동 시간을 실제 길찾기로 (없으면 직선거리 추정)
    place_info_online: bool = True   # 주변 사진·볼거리·식당·숙소를 공개 서비스에서 (인터넷 필요, 좌표만 보냄)

    @property
    def llm_enabled(self) -> bool:
        """API 모드 + 키 있을 때만 True. 아니면 오프라인"""
        return self.llm_mode == "api" and bool(self.llm_api_key)

    def public_view(self) -> dict:
        """화면·로그에 보여도 되는 값만 (키는 절대 안 넣음)"""
        return {
            "llm_mode": self.llm_mode,
            "llm_enabled": self.llm_enabled,
            "llm_model": self.llm_model if self.llm_enabled else "-",
            "llm_base_url": self.llm_base_url if self.llm_enabled else "-",
            "data_mode": self.data_mode,
            "agent_llm": self.agent_llm and self.llm_enabled,
            "jeju_datahub_api": "설정됨" if self.jeju_datahub_key else "없음",
            "stt": "켜짐" if self.stt_enabled else "꺼짐",
            "kakao_map": "설정됨" if self.kakao_js_key else "없음",
            "kakao_route": "설정됨" if self.kakao_rest_key else "없음",
            "place_info_online": "켜짐" if self.place_info_online else "꺼짐",
        }

    @property
    def stt_enabled(self) -> bool:
        return self.stt_mode == "api" and bool(self.llm_api_key)


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)
    e = os.environ.get
    mode = (e("LLM_MODE") or "offline").strip().lower()
    if mode not in {"offline", "api"}:
        mode = "offline"
    data_mode = (e("DATA_MODE") or "sample").strip().lower()   # sample: 센터 CSV 없으면 [가상] 센터 표로 대신
    if data_mode not in {"sample", "real"}:
        data_mode = "sample"
    return Settings(
        llm_mode=mode,
        llm_base_url=(e("LLM_BASE_URL") or "https://api.openai.com/v1").strip(),
        llm_api_key=(e("LLM_API_KEY") or "").strip(),
        llm_model=(e("LLM_MODEL") or "gpt-4.1-mini").strip(),
        llm_max_calls_per_question=_int(e("LLM_MAX_CALLS_PER_QUESTION"), 12),
        llm_monthly_budget_usd=_float(e("LLM_MONTHLY_BUDGET_USD"), 20.0),
        llm_cache=_bool(e("LLM_CACHE"), True),
        llm_tool_calling=_bool(e("LLM_TOOL_CALLING"), False),
        jeju_datahub_key=(e("JEJU_DATAHUB_KEY") or "").strip(),
        data_mode=data_mode,
        agent_llm=_bool(e("AGENT_LLM"), True),
        agent_max_steps=max(2, _int(e("AGENT_MAX_STEPS"), 4)),
        app_name=(e("APP_NAME") or "고치갑서").strip(),
        stt_mode="api" if (e("STT_MODE") or "off").strip().lower() == "api" else "off",
        stt_model=(e("STT_MODEL") or "gpt-4o-mini-transcribe").strip(),
        kakao_js_key=(e("KAKAO_JS_KEY") or "").strip(),
        llm_reasoning_effort=r if (r := (e("LLM_REASONING_EFFORT") or "").strip().lower()) in REASONING_EFFORTS else "",
        kakao_rest_key=(e("KAKAO_REST_KEY") or "").strip(),
        place_info_online=_bool(e("PLACE_INFO_ONLINE"), True),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


@lru_cache(maxsize=1)
def catalog() -> dict:
    """데이터 카탈로그 (config/catalog.yaml)"""
    return yaml.safe_load((CONFIG_DIR / "catalog.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def access_rules() -> dict:
    """접근성 판정·코스 기준 (config/access_rules.yaml)"""
    return yaml.safe_load((CONFIG_DIR / "access_rules.yaml").read_text(encoding="utf-8"))
