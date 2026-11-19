"""관광공사 무장애 제주 식당 (kto_bf_restaurant.csv만 읽음, 실행 중 API 안 부름)
출처: 공공데이터포털 15101897 (이용허락범위 제한 없음)
빈칸은 '없음' 말고 '기재 안 됨'. 화장실·출입 문장을 있음/가능/주의/정보 없음으로 나눔
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

FILE = "kto_bf_restaurant.csv"
SOURCE = "한국관광공사 무장애 여행 정보"
CAFE_CODES = ("FD05",)
NEG = re.compile(r"진입\s*(어려움|주의)|통로가\s*좁|계단이\s*있")
POS = re.compile(r"턱이?\s*(거의\s*)?없|경사로|자동문|진입\s*쉬|접근\s*(쉬|가능)|출입\s*가능|통과\s*가능|단차\s*없|"
                 r"원활한\s*이동|이용\s*(쉬|편리)")
COLUMNS = ["contentid", "name", "kind", "address", "lat", "lng", "toilet", "toilet_ev", "entry", "entry_ev",
           "parking_ev", "data_date", "fetched"]


def clean(v) -> str:
    """원문 칸 → 한 줄 (<br/>·'_무장애 편의시설' 꼬리표 뗌)"""
    if not isinstance(v, str):
        return ""
    t = re.sub(r"<br\s*/?>", " ", v)
    t = re.sub(r"_(무장애|시각장애인) 편의(시설|정보)", "", t)
    return re.sub(r"\s+", " ", t).strip()


def toilet_state(text: str) -> str:
    t = clean(text)
    if not t:
        return "정보 없음"
    return "없음" if "없" in t else "있음"


def entry_state(route: str, exit_: str) -> tuple[str, str]:
    """(가능|주의|정보 없음, 근거 원문). 출입구(exit) 문장 먼저, 없으면 접근로(route)"""
    r, e = clean(route), clean(exit_)
    both = f"{r} {e}"
    ev = e or r
    if NEG.search(both):
        return "주의", ev
    if POS.search(both):
        return "가능", ev
    return "정보 없음", ev


def _date(v) -> str | None:
    s = re.sub(r"[^0-9]", "", str(v or ""))[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else None


def parse(raw: pd.DataFrame) -> pd.DataFrame:
    """받은 표 → 코스용 표(COLUMNS). 좌표 없는 줄은 뺌"""
    if raw.empty:
        return pd.DataFrame(columns=COLUMNS)
    rows = []
    for r in raw.fillna("").to_dict("records"):
        lat, lng = pd.to_numeric(r.get("mapy"), errors="coerce"), pd.to_numeric(r.get("mapx"), errors="coerce")
        if pd.isna(lat) or pd.isna(lng):
            continue
        entry, entry_ev = entry_state(r.get("route", ""), r.get("exit", ""))
        rows.append({
            "contentid": str(r.get("contentid", "")), "name": str(r.get("title", "")).strip(),
            "kind": "cafe" if str(r.get("lclsSystm2", "")).startswith(CAFE_CODES) else "restaurant",
            "address": " ".join(x for x in (str(r.get("addr1", "")).strip(), str(r.get("addr2", "")).strip()) if x),
            "lat": float(lat), "lng": float(lng),
            "toilet": toilet_state(r.get("restroom", "")), "toilet_ev": clean(r.get("restroom", "")),
            "entry": entry, "entry_ev": entry_ev, "parking_ev": clean(r.get("parking", "")),
            "data_date": _date(r.get("modifiedtime")), "fetched": str(r.get("fetched", "")) or None})
    return pd.DataFrame(rows, columns=COLUMNS)


def load(folder: Path) -> pd.DataFrame:
    """kto_bf_restaurant.csv → parse(). 파일 없으면 빈 표"""
    p = folder / FILE
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return parse(pd.read_csv(p, encoding="utf-8-sig", dtype=str, keep_default_na=False))


def norm_name(s: str) -> str:
    """같은 가게 맞추기용 이름 (띄어쓰기·괄호·'본점'·'제주점' 무시)"""
    return re.sub(r"[\s()（）\[\]]|본점$|제주점$|제주본점$", "", str(s or ""))
