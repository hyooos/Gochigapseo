"""장소 주변 사진·볼거리·가게를 공개 서비스에서 (선택 기능, PLACE_INFO_ONLINE=0이면 끔)
사진 Wikimedia, 볼거리 위키백과, 가게는 카카오(키 있을 때) 아니면 Photon(OSM)
좌표·분류만 보냄. 인터넷 없으면 빈 결과
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import quote

from ..tools.geo import haversine_km

UA = {"User-Agent": "jeju-trip-agent/0.1 (educational project)"}   # 이메일 같은 개인정보는 안 넣음
TIMEOUT = 6

Fetch = Callable[..., Any]   # (url, params, headers=None) → JSON. 테스트에서 가짜로 바꿔 끼움


def _requests_fetch(url: str, params: dict, headers: dict | None = None) -> Any:
    """표준 라이브러리만 씀 (새 패키지 없음)"""
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ── 사진 ──
def photos(lat: float, lng: float, radius_m: int = 1500, n: int = 6, fetch: Fetch | None = None) -> list[dict]:
    """좌표 근처 공개 사진. [{thumb, page, title, author, license}]"""
    fetch = fetch or _requests_fetch
    try:
        data = fetch("https://commons.wikimedia.org/w/api.php", {
            "action": "query", "format": "json", "generator": "geosearch", "ggscoord": f"{lat}|{lng}",
            "ggsradius": radius_m, "ggsnamespace": 6, "ggslimit": n * 2, "prop": "imageinfo|coordinates",
            "iiprop": "url|extmetadata", "iiurlwidth": 480, "colimit": n * 2})
    except Exception:
        return []
    out = []
    for p in (data or {}).get("query", {}).get("pages", {}).values():
        info = (p.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl")
        if not thumb or not re.search(r"\.(jpe?g|png|webp)$", p.get("title", ""), re.I):
            continue
        meta = info.get("extmetadata", {})
        c = (p.get("coordinates") or [{}])[0]
        km = haversine_km(lat, lng, c["lat"], c["lon"]) if "lat" in c else None
        out.append({"thumb": thumb, "page": info.get("descriptionurl", ""),
                    "title": re.sub(r"^File:|\.\w+$", "", p.get("title", "")),
                    "author": _strip_tags(meta.get("Artist", {}).get("value", ""))[:40] or "작성자 미상",
                    "license": _strip_tags(meta.get("LicenseShortName", {}).get("value", "")) or "라이선스 확인",
                    "km": None if km is None else round(float(km), 2)})
    out.sort(key=lambda x: x["km"] if x["km"] is not None else 99)
    return out[:n]


# ── 주변 볼거리 (위키백과) ──
_NOT_SIGHT = re.compile(r"(학교|대학교|우편|우편집중국|우체국|영사관|대사관|경찰서|소방서|주민센터|행정복지센터|사무소|청사|청|법원|세무서|은행|병원|아파트|교회|성당|방송국)$")


def sights(lat: float, lng: float, radius_m: int = 5000, n: int = 5, fetch: Fetch | None = None) -> list[dict]:
    """근처 볼거리. [{title, extract, thumb, url, km}]"""
    fetch = fetch or _requests_fetch
    try:
        data = fetch("https://ko.wikipedia.org/w/api.php", {
            "action": "query", "format": "json", "generator": "geosearch", "ggscoord": f"{lat}|{lng}",
            "ggsradius": radius_m, "ggslimit": 20, "prop": "pageimages|extracts|coordinates", "piprop": "thumbnail",
            "pithumbsize": 400, "colimit": 20, "exintro": 1, "explaintext": 1, "exsentences": 2, "exlimit": 20})
    except Exception:
        return []
    out = []
    for p in (data or {}).get("query", {}).get("pages", {}).values():
        title = p.get("title", "")
        if _NOT_SIGHT.search(title):
            continue
        c = (p.get("coordinates") or [{}])[0]
        if "lat" not in c:   # 좌표 없으면 거리 몰라서 뺌
            continue
        km = haversine_km(lat, lng, c["lat"], c["lon"])
        thumb = (p.get("thumbnail") or {}).get("source")
        if thumb and "Replace_this_image" in thumb:   # 위키백과 '사진 없음' 안내 그림은 뺌
            thumb = None
        out.append({"title": title, "extract": (p.get("extract") or "").strip()[:160],
                    "thumb": thumb, "url": "https://ko.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                    "km": round(float(km), 1), "lat": c["lat"], "lng": c["lon"]})
    out.sort(key=lambda x: x["km"] if x["km"] is not None else 99)
    return out[:n]


# ── 먹을 곳·숙소·기념품 ──
# 1순위 카카오 로컬: 반경 안 가까운 순, 전화·카카오맵 링크까지 (KAKAO_REST_KEY 있을 때)
# 2순위 Photon(OSM, 키 없음): 무료 서버라 느리거나 응답 없을 때 있음
KAKAO_LOCAL = "https://dapi.kakao.com/v2/local/search"
KAKAO_KINDS: dict[str, list[tuple[str, str]]] = {   # 화면 칸 → (카카오 분류 코드 or 'kw:검색어')
    "먹을 곳": [("FD6", "식당"), ("CE7", "카페")],
    "숙소": [("AD5", "숙소")],
    "기념품": [("kw:기념품", "기념품점")],
}
KINDS: dict[str, list[tuple[str, str]]] = {
    "먹을 곳": [("amenity:restaurant", "restaurant"), ("amenity:cafe", "cafe")],
    "숙소": [("tourism:hotel", "hotel"), ("tourism:guest_house", "guest house")],
    "기념품": [("shop:gift", "gift"), ("shop:souvenir", "souvenir")],
}
KIND_KO = {"restaurant": "식당", "cafe": "카페", "hotel": "호텔", "guest_house": "게스트하우스", "gift": "기념품점",
           "souvenir": "기념품점"}


def _kakao(lat: float, lng: float, code: str, kind: str, key: str, radius_m: int, fetch: Fetch) -> tuple[list[dict], bool]:
    """(장소 목록, 응답 받았는지). 보내는 건 좌표·반경·분류뿐"""
    params = {"x": lng, "y": lat, "radius": radius_m, "sort": "distance", "size": 15}
    if code.startswith("kw:"):
        url, params = f"{KAKAO_LOCAL}/keyword.json", {**params, "query": code[3:]}
    else:
        url, params = f"{KAKAO_LOCAL}/category.json", {**params, "category_group_code": code}
    try:
        data = fetch(url, params, headers={"Authorization": f"KakaoAK {key}"})
    except Exception:
        return [], False
    out = []
    for d in (data or {}).get("documents", []):
        try:
            km = int(d.get("distance") or 0) / 1000
            y, x = float(d["y"]), float(d["x"])
        except (KeyError, TypeError, ValueError):
            continue
        sub = (d.get("category_name") or "").split(">")[-1].strip()   # '음식점 > 한식 > 해장국' → '해장국'
        out.append({"name": d.get("place_name", ""), "type": sub or kind, "km": round(km, 2), "lat": y, "lng": x,
                    "addr": d.get("road_address_name") or d.get("address_name") or "", "phone": d.get("phone") or "",
                    "url": d.get("place_url") or ""})
    return out, True


def _photon(lat: float, lng: float, tag: str, word: str, fetch: Fetch) -> tuple[list[dict], bool]:
    try:
        data = fetch("https://photon.komoot.io/api/", {"q": word, "lat": lat, "lon": lng, "osm_tag": tag, "limit": 20})
    except Exception:
        return [], False
    out = []
    for f in (data or {}).get("features", []):
        pr, g = f.get("properties", {}), f.get("geometry", {}).get("coordinates")
        if not g or not pr.get("name"):
            continue
        km = float(haversine_km(lat, lng, g[1], g[0]))
        addr = " ".join(str(x) for x in (pr.get("district") or pr.get("locality"), pr.get("street"), pr.get("housenumber")) if x)
        out.append({"name": pr["name"], "type": KIND_KO.get(pr.get("osm_value"), pr.get("osm_value", "")), "km": round(km, 1),
                    "lat": g[1], "lng": g[0], "addr": addr, "phone": "", "url": ""})
    return out, True


def around(lat: float, lng: float, max_km: float = 2.0, n: int = 5, fetch: Fetch | None = None,
           kakao_key: str = "") -> dict:
    """{'먹을 곳', '숙소', '기념품', 'source', 'failed'}. 가까운 순
    failed=True면 서버가 하나도 응답 안 한 것 (화면은 '검색이 안 됨'으로)
    """
    fetch = fetch or _requests_fetch
    source = "kakao" if kakao_key else "osm"
    if kakao_key:
        jobs = [(kind, code, ko) for kind, codes in KAKAO_KINDS.items() for code, ko in codes]
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            found = list(ex.map(lambda j: (j[0], *_kakao(lat, lng, j[1], j[2], kakao_key, int(max_km * 1000), fetch)), jobs))
        if not any(ok for _, _, ok in found):
            source = "osm"
    if source == "osm":
        jobs = [(kind, tag, word) for kind, tags in KINDS.items() for tag, word in tags]
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            found = list(ex.map(lambda j: (j[0], *_photon(lat, lng, j[1], j[2], fetch)), jobs))
    out: dict = {k: [] for k in KINDS}
    for kind, items, _ in found:
        out[kind] += items
    for kind in KINDS:
        seen, keep = set(), []
        for x in sorted(out[kind], key=lambda x: x["km"]):
            if x["km"] <= max_km and x["name"] not in seen:
                seen.add(x["name"])
                keep.append(x)
        out[kind] = keep[:n]
    out["source"] = source
    out["failed"] = not any(ok for _, _, ok in found)
    return out


def place_info(lat: float, lng: float, fetch: Fetch | None = None, kakao_key: str = "") -> dict:
    """사진·볼거리·주변 시설 동시에 가져옴. 화면이 캐시해서 씀"""
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_ph = ex.submit(photos, lat, lng, fetch=fetch)
        f_si = ex.submit(sights, lat, lng, fetch=fetch)
        f_ar = ex.submit(around, lat, lng, fetch=fetch, kakao_key=kakao_key)
        return {"photos": f_ph.result(), "sights": f_si.result(), "around": f_ar.result()}
