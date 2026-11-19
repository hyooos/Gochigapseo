import dataclasses
import os

import pytest

# 장소 주변 사진·볼거리 조회(위키·오픈스트리트맵)는 인터넷을 쓴다. 테스트(CI 포함)에서는 끄고,
# 조회 자체는 tests/test_nearby_online.py가 가짜 응답으로 확인한다.
os.environ["PLACE_INFO_ONLINE"] = "0"

from jeju_agent.config import load_settings
from jeju_agent.data import Repository
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator


@pytest.fixture()
def settings(monkeypatch, tmp_path):
    # 개발자 PC의 .env가 테스트에 섞이지 않게 비운다 → 항상 오프라인·샘플 데이터
    for k in ("LLM_MODE", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "DATA_MODE", "JEJU_DATAHUB_KEY", "APP_NAME",
              "STT_MODE", "STT_MODEL", "KAKAO_JS_KEY", "KAKAO_REST_KEY",
              "LLM_MAX_CALLS_PER_QUESTION", "AGENT_LLM", "AGENT_MAX_STEPS", "LLM_TOOL_CALLING", "LLM_CACHE",
              "LLM_REASONING_EFFORT", "PLACE_INFO_ONLINE"):
        monkeypatch.delenv(k, raising=False)
    return load_settings(env_file=tmp_path / "none.env")


@pytest.fixture()
def repo():
    return Repository("sample")


@pytest.fixture()
def orch(settings, repo, tmp_path):
    return Orchestrator(settings, repo=repo, llm=LLMClient(settings, runtime_dir=tmp_path / "rt"))


@pytest.fixture()
def api_settings(settings):
    """API 모드 설정(가짜 키). 실제 네트워크 호출은 transport로 가로챈다.
    하위 에이전트 LLM 판단은 끈다(가짜 tool 응답을 준비한 test_agent_llm.py에서만 켠다)."""
    return dataclasses.replace(settings, llm_mode="api", llm_api_key="dummy", llm_max_calls_per_question=2, agent_llm=False)
