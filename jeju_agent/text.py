"""문장틀용 작은 도구 (받침 맞는 조사 등)"""
from __future__ import annotations

from datetime import date, timedelta

_PAIRS = {"은": ("은", "는"), "는": ("은", "는"), "이": ("이", "가"), "가": ("이", "가"),
          "을": ("을", "를"), "를": ("을", "를"), "으로": ("으로", "로"), "로": ("으로", "로"), "과": ("과", "와"), "와": ("과", "와")}


def _batchim(ch: str) -> int | None:
    """받침 번호(0=없음). 한글 아니면 None"""
    code = ord(ch) - 0xAC00
    return code % 28 if 0 <= code <= 11171 else None


def josa(word: str, j: str) -> str:
    """josa('장애인 화장실', '은') → '장애인 화장실은', josa('휠체어 대여', '으로') → '휠체어 대여로'"""
    w = word.rstrip()
    b = _batchim(w[-1]) if w else None
    with_b, without_b = _PAIRS[j]
    if b is None:            # 숫자·영문으로 끝나면 받침 있는 쪽 (읽는 소리를 몰라서)
        return word + with_b
    if j in ("으로", "로") and b == 8:   # ㄹ 받침 + 로
        return word + without_b
    return word + (with_b if b else without_b)


def date_label(iso: str, day: int = 1) -> str:
    """'2026-11-18', 1 → '11/18(수)'. day일차 날짜"""
    d = date.fromisoformat(iso) + timedelta(days=day - 1)
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]})"


def fee_text(fee) -> str | None:
    """입장료 한 줄. 0 → '입장료 없음', 3000 → '입장료 3,000원', 자료 없으면 None
    판정엔 안 쓰고 보여 주기만 함
    """
    try:
        n = float(fee)
    except (TypeError, ValueError):
        return None
    if n != n or n < 0:   # NaN
        return None
    return "입장료 없음" if n == 0 else f"입장료 {int(n):,}원"
