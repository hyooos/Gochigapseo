"""거리·이동시간 계산. 기본은 직선거리 추정, KAKAO_REST_KEY 있으면 카카오 길찾기
카카오 응답은 저장 금지라 메모리에만 잠깐 둠 (파일·로그 X)
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

EARTH_KM = 6371.0088
KAKAO_DIRECTIONS = "https://apis-navi.kakaomobility.com/v1/directions"
KAKAO_ADDRESS = "https://dapi.kakao.com/v2/local/search/address.json"
ROUTE_TTL_S = 6 * 3600   # 길찾기 결과 메모리 보관 시간 (센터 PC는 계속 켜 두니까 무기한은 안 됨)
JEJU_BOX = (33.10, 33.60, 126.10, 126.99)   # 위도 최소·최대, 경도 최소·최대 (제주 밖 좌표는 버림)
_down_until = 0.0     # 네트워크·키 오류 나면 잠깐 쉼 (구간마다 시간 초과 기다리면 안 돼서)


def haversine_km(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(a))


def travel_min(km, transport: str, course_rules: dict):
    """직선거리(km) → 예상 이동시간(분). 버스는 기다리는 시간 더함"""
    speed = course_rules["speed_kmh"].get(transport, course_rules["speed_kmh"]["car"])
    minutes = np.asarray(km, dtype=float) * course_rules["road_factor"] / speed * 60
    if transport == "bus":
        minutes = minutes + course_rules["bus_wait_min"]
    return np.round(minutes).astype(int)


def road_min(a: tuple[float, float], b: tuple[float, float], key: str) -> int | None:
    """두 지점 자동차 길찾기 시간(분). 키 없음·실패·길 없음이면 None
    보내는 건 좌표뿐 (이용자 조건·센터 자료 안 보냄)
    """
    got = _road(a, b, key)
    return got[0] if got else None


def road_path(a: tuple[float, float], b: tuple[float, float], key: str) -> list[tuple[float, float]] | None:
    """같은 길찾기 응답의 도로 좌표 (캐시라 추가 호출 없음)
    지도 선으로만 써서 점 줄여 둠(PATH_MAX). 실패면 None → 지도는 직선 점선
    """
    got = _road(a, b, key)
    return list(got[1]) if got and got[1] else None


PATH_MAX = 200   # 한 구간 경로선 최대 점 수


def _road(a, b, key: str) -> tuple[int, tuple] | None:
    if not key or time.time() < _down_until:
        return None
    return _road_min(round(a[0], 5), round(a[1], 5), round(b[0], 5), round(b[1], 5), key)


def _thin(pts: list[tuple[float, float]], n: int) -> tuple:
    if len(pts) <= n:
        return tuple(pts)
    step = (len(pts) - 1) / (n - 1)
    return tuple(pts[round(i * step)] for i in range(n))


def _ttl_cache(seconds: float, maxsize: int):
    """메모리에만 seconds 동안 기억하는 캐시. cache_clear()로 비움"""
    def wrap(fn):
        box: OrderedDict = OrderedDict()

        def inner(*args):
            now = time.time()
            if args in box and now - box[args][0] < seconds:
                return box[args][1]
            val = fn(*args)
            box[args] = (now, val)
            box.move_to_end(args)
            while len(box) > maxsize:
                box.popitem(last=False)
            return val
        inner.cache_clear = box.clear   # type: ignore[attr-defined]
        return inner
    return wrap


@_ttl_cache(ROUTE_TTL_S, 2048)
def _road_min(lat1: float, lng1: float, lat2: float, lng2: float, key: str) -> tuple[int, tuple] | None:
    """(분, 도로 좌표). vertexes는 [x1, y1, x2, y2, …](경도, 위도) 평면 배열"""
    global _down_until
    q = urllib.parse.urlencode({"origin": f"{lng1},{lat1}", "destination": f"{lng2},{lat2}", "priority": "RECOMMEND"})
    req = urllib.request.Request(f"{KAKAO_DIRECTIONS}?{q}", headers={"Authorization": f"KakaoAK {key}"})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            route = json.load(r)["routes"][0]
    except Exception:   # 네트워크·키(401)·한도 오류: 5분 동안 추정치만 씀
        _down_until = time.time() + 300
        return None
    if route.get("result_code") != 0:   # 너무 가깝거나 길 없음 → 이 구간만 추정치
        return None
    pts: list[tuple[float, float]] = []
    for sec in route.get("sections") or []:
        for road in sec.get("roads") or []:
            v = road.get("vertexes") or []
            pts += [(round(v[i + 1], 6), round(v[i], 6)) for i in range(0, len(v) - 1, 2)]
    return max(1, round(route["summary"]["duration"] / 60)), _thin(pts, PATH_MAX)


class LiveXY:
    """가게 주소 → 좌표 (카카오, 실시간). 코스 하나 짜는 동안만 쓰고 버림
    오류 한 번 나면 그 코스에선 더 안 부름 (기준 코스 거친 추정 거리로)
    """

    def __init__(self, key: str, fetch: Callable[[str, str], tuple[float, float] | None] | None = None, workers: int = 6):
        self.key, self.workers = key, workers
        self._fetch = fetch or _kakao_address
        self._got: dict[str, tuple[float, float] | None] = {}
        self.calls = self.found = 0
        self.down = False

    def fill(self, items: list[dict], need: Callable[[dict], bool], anchor_xy: dict[str, tuple[float, float]]) -> None:
        """좌표 없는 가게 중 need가 True인 것만 좌표 찾아 붙임
        기준 코스 좌표 있으면 '조사 거리 + 2km' 안일 때만 받음 (잘못 찾은 것 거르기)
        """
        if self.down or not self.key:
            return
        todo = [i for i in items if i.get("lat") is None and i.get("address") and need(i)]
        addrs = [a for a in dict.fromkeys(i["address"] for i in todo) if a not in self._got]
        if addrs:
            self.calls += len(addrs)
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                for a, xy in zip(addrs, ex.map(lambda a: self._safe(a), addrs)):
                    self._got[a] = xy
        for i in todo:
            xy = self._got.get(i["address"])
            if xy is None:
                continue
            a = anchor_xy.get(i.get("anchor_id"))
            if a is not None and float(haversine_km(a[0], a[1], xy[0], xy[1])) > float(i.get("distance_km") or 0) + 2:
                continue
            i["lat"], i["lng"], i["xy_from"] = xy[0], xy[1], "카카오 주소 검색(실시간)"
            self.found += 1

    def _safe(self, address: str) -> tuple[float, float] | None:
        if self.down:
            return None
        try:
            xy = self._fetch(address, self.key)
        except Exception:   # 키·한도·네트워크 → 이 코스에선 더 안 부름
            self.down = True
            return None
        if xy is None or not (JEJU_BOX[0] <= xy[0] <= JEJU_BOX[1] and JEJU_BOX[2] <= xy[1] <= JEJU_BOX[3]):
            return None
        return xy


def _kakao_address(address: str, key: str) -> tuple[float, float] | None:
    """카카오 주소 검색 1건 → (위도, 경도). 결과 없으면 None, 오류는 예외"""
    q = urllib.parse.urlencode({"query": address, "size": 1})
    req = urllib.request.Request(f"{KAKAO_ADDRESS}?{q}", headers={"Authorization": f"KakaoAK {key}"})
    with urllib.request.urlopen(req, timeout=3) as r:
        docs = json.load(r).get("documents") or []
    return (float(docs[0]["y"]), float(docs[0]["x"])) if docs else None
