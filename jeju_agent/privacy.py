"""이용자 조건(장애·건강·이동 방법)을 trace에 안 남기기
하위 에이전트 LLM이 쓴 판단 이유에 "휠체어 이용자라서…" 같은 게 섞일 수 있어서 기록 전에 '(조건)'으로 가림
"""
from __future__ import annotations

import re

from .contracts import COGNITION_KO, HEARING_KO, MOBILITY_KO, NEED_KO, VISION_KO, TravelerProfile

# 질문 해석 조건 낱말 + 화면 이름표. 긴 말부터 가림
TERMS = sorted({*[v for k, v in MOBILITY_KO.items() if k != "none"], *[v for k, v in VISION_KO.items() if k != "none"],
                *[v for k, v in HEARING_KO.items() if k != "none"], *[v for k, v in NEED_KO.items() if k != "indoor_pref"],
                *[k for k in MOBILITY_KO if k != "none"], *[k for k in NEED_KO if k != "indoor_pref"],
                "전동휠체어", "휠체어 이용", "휠체어를 쓰", "휠체어", "전동 스쿠터", "전동스쿠터", "유모차", "유아차", "목발", "지팡이",
                "보행기", "보행 보조", "보행보조", "걷기 힘", "걷는 게 힘", "오래 못 걸", "거동", "어르신", "노인", "고령",
                "시각장애", "시각 장애", "저시력", "전맹", "안내견", "청각장애", "청각 장애", "난청", "보청기", "수어",
                "발달·지적장애", "발달장애", "발달 장애", "지적장애", "지적 장애", "자폐", "다운증후군",
                "장애인", "장애"}, key=len, reverse=True)
_RX = re.compile("|".join(re.escape(t) for t in TERMS))


def scrub(text: str, profile: TravelerProfile | None = None) -> str:
    """조건 낱말을 '(조건)'으로 바꿈. profile 있으면 그 사람 조건 이름표도 가림"""
    if not text:
        return text
    out = _RX.sub("(조건)", text)
    if profile is not None:
        for w in profile_words(profile):
            out = out.replace(w, "(조건)")
    return out


def profile_words(p: TravelerProfile) -> list[str]:
    """이 이용자 조건 낱말 (화면 이름표·코드값)"""
    health = [n for n in p.needs if n != "indoor_pref"]     # 실내 선호는 취향이라 뺌
    words = [NEED_KO[n] for n in health] + health
    if p.mobility != "none":
        words += [MOBILITY_KO[p.mobility], p.mobility]
    if p.vision != "none":
        words += [VISION_KO[p.vision]]
    if p.hearing != "none":
        words += [HEARING_KO[p.hearing]]
    if p.cognition != "none":
        words += [COGNITION_KO[p.cognition]]
    return words
