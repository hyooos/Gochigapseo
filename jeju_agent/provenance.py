"""출처 표기랑 [가상]/[실데이터] 표시. 화면·보고서·차트는 꼭 이걸 거쳐야 함"""
from __future__ import annotations

from typing import Literal

DataLabel = Literal["가상", "실데이터"]

# 센터 자료 쓸 때 꼭 넣는 문구. 글자 바꾸면 안 됨
SOURCE_NOTICE = (
    "본 자료는 제주빅데이터센터에서 제공하는 자료를 활용한 것이며, "
    "연구결과는 제주특별자치도 및 제주테크노파크와 관련이 없음을 밝힙니다."
)


def tag(label: DataLabel) -> str:
    return f"[{label}]"


def footer(labels: set[str] | None = None) -> str:
    """화면 하단·보고서 끝 문구"""
    lab = " ".join(tag(x) for x in sorted(labels or [])) if labels else ""
    return f"{lab} {SOURCE_NOTICE}".strip()
