"""비짓제주 관광정보 API → 우리 관광지랑 이름·좌표로 짝 맞추기
저장은 scripts/fetch_visitjeju.py가 하고 화면은 그 표만 읽음
태그는 빈칸 시설을 '있음'으로 채울 때만 씀 (없다고 '없음'으로 두진 않음)
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

import pandas as pd

from ..tools.geo import haversine_km

URL = "http://api.visitjeju.net/vsjApi/contents/searchList"
PHOTO_HOST = "https://api.cdn.visitjeju.net/"   # 화면에 넣는 사진은 이 주소로 시작하는 것만 믿음
TAG_PREFIX = "비짓제주 태그"                    # 태그로 채운 근거 문장 머리말 (조사 원문이랑 구별용)
MAX_KM = 2.0                                    # 이름 맞아도 이보다 멀면 다른 곳
COLUMNS = ["place_id", "contentsid", "title", "distance_km", "thumbnail", "image", "alltag", "phoneno"]
# 태그는 쉼표로 나뉨. 한 칸에 이 낱말 있으면 '있음'으로만 채움
# 경사로는 '경사로'만 (단차없음·데크는 아님)
# 부정어는 그 칸 안에서만 봄 ('승강기, 단차없음'의 '없음'이 승강기를 지우면 안 됨)
_FILL = {
    "acc_toilet": (["장애인화장실", "장애인용화장실", "다목적화장실"], "장애인 화장실"),
    "acc_parking": (["장애인전용주차장", "장애인전용주차", "장애인주차구역", "장애인주차장", "장애인주차"], "장애인 주차"),
    "wheelchair_rental": (["휠체어무료대여", "휠체어대여", "휠체어비치", "휠체어구비"], "휠체어 대여"),
    "elevator": (["엘리베이터", "승강기"], "엘리베이터"),
    "ramp": (["경사로"], "경사로"),
}
_NEGATIVE = ("없음", "없다", "없어", "미설치", "불가", "미비치", "없슴")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def tag_facilities(alltag: str) -> dict[str, str]:
    """태그에서 확인된 시설 → 근거 문장. 적힌 것만"""
    parts = [p for p in (_compact(s) for s in str(alltag or "").split(",")) if p]
    out: dict[str, str] = {}
    for col, (words, label) in _FILL.items():
        for part in parts:
            filled = False
            for w in words:
                i = part.find(w)
                if i < 0:
                    continue
                if any(n in part[i + len(w):i + len(w) + 8] for n in _NEGATIVE):
                    break
                out[col] = f"{TAG_PREFIX}: {label}"
                filled = True
                break
            if filled:
                break
    return out


def is_tag_evidence(ev) -> bool:
    """근거 문장이 비짓제주 태그로 채운 건지"""
    return isinstance(ev, str) and ev.startswith(TAG_PREFIX)


def fill_blank_facilities(places: pd.DataFrame, tags: pd.DataFrame) -> pd.DataFrame:
    """빈칸인 시설만 태그로 1이랑 근거 채움. 0·1 있으면 그대로, 원본 표는 안 바꿈"""
    if places.empty or tags is None or tags.empty or "place_id" not in tags or "alltag" not in tags:
        return places
    out = places.copy()
    by_id = tags.drop_duplicates("place_id").set_index("place_id")["alltag"]
    for pid, tag in by_id.items():
        hits = tag_facilities(str(tag))
        if not hits:
            continue
        idx = out.index[out["place_id"].astype(str) == str(pid)]
        if len(idx) == 0:
            continue
        i = idx[0]
        for col, evidence in hits.items():
            if col not in out.columns or pd.notna(out.at[i, col]):
                continue
            out.at[i, col] = 1
            ev = f"ev_{col}"
            if ev in out.columns:   # 값을 태그로 채웠으니 근거도 태그로
                out.at[i, ev] = evidence
    return out


def fetch_all(key: str, timeout: int = 15) -> list[dict]:
    """전체 목록(국문). 키 틀리면 ValueError"""
    items: list[dict] = []
    page, pages = 1, 1
    while page <= pages:
        q = urllib.parse.urlencode({"apiKey": key, "locale": "kr", "page": page})
        req = urllib.request.Request(f"{URL}?{q}", headers={"User-Agent": "Mozilla/5.0"})   # 기본 UA는 방화벽이 막을 수 있음
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
        if "items" not in body:
            raise ValueError(f"비짓제주 API 오류: {body.get('result')} {body.get('resultMessage')}")
        items += body["items"] or []
        pages = int(body.get("pageCount") or 1)
        page += 1
    return items


def norm(name: str) -> str:
    """이름 비교용: 괄호 설명 빼고 한글·영문·숫자만. '성산일출봉(UNESCO …)' → '성산일출봉'"""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", re.sub(r"\(.*?\)|\[.*?\]", "", str(name or "")))


def photo_of(item: dict) -> tuple[str, str] | None:
    """(썸네일, 큰 사진) 주소. 비짓제주 CDN 아니면 None (HTML에 넣으니까)"""
    p = ((item.get("repPhoto") or {}).get("photoid") or {})
    thumb, img = str(p.get("thumbnailpath") or ""), str(p.get("imgpath") or "")
    ok = [u if u.startswith(PHOTO_HOST) else "" for u in (thumb, img)]
    return (ok[0] or ok[1], ok[1] or ok[0]) if any(ok) else None


def match(places: pd.DataFrame, items: list[dict]) -> pd.DataFrame:
    """우리 장소마다 비짓제주 콘텐츠 하나: 이름 같거나 포함(3글자 이상) + MAX_KM 안
    포함 매칭은 '관광지'만. 애매하면 안 붙임 (다른 곳 사진이면 판단을 잘못 이끔)
    """
    cands = []
    for it in items:
        ph = photo_of(it)
        n = norm(it.get("title"))
        if ph and len(n) >= 2:
            cands.append((n, it, ph, pd.to_numeric(it.get("latitude"), errors="coerce"),
                          pd.to_numeric(it.get("longitude"), errors="coerce")))
    rows = []
    for p in places.itertuples():
        pn = norm(p.name)
        best = None
        for n, it, ph, lat, lng in cands:
            same = n == pn
            spot = (it.get("contentscd") or {}).get("label") == "관광지"
            if not same and not (spot and min(len(n), len(pn)) >= 3 and (n in pn or pn in n)):
                continue
            if pd.isna(lat) or pd.isna(lng) or pd.isna(p.lat) or pd.isna(p.lng):
                if not same:
                    continue
                km = None
            else:
                km = float(haversine_km(p.lat, p.lng, lat, lng))
                if km > MAX_KM:
                    continue
            key = (not same, km if km is not None else MAX_KM)
            if best is None or key < best[0]:
                best = (key, it, ph, km)
        if best:
            _, it, ph, km = best
            rows.append([p.place_id, it.get("contentsid"), it.get("title"), None if km is None else round(km, 2), ph[0], ph[1],
                         it.get("alltag") or "", it.get("phoneno") or ""])   # 태그·전화: 비어 있으면 '정보 없음'
    return pd.DataFrame(rows, columns=COLUMNS)
