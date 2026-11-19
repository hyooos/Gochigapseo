"""주변 사진·볼거리·식당 조회 — 인터넷 없이 가짜 응답으로 확인
(거리 거르기, 관공서 빼기, 카카오 우선·Photon 대신, 실패는 '없음'과 구별)."""
from jeju_agent.data.nearby_online import around, photos, place_info, sights

LAT, LNG = 33.5120, 126.5267   # 동문시장 근처
KEY = "test-key"


def fake(url, params, headers=None):
    if "commons" in url:
        return {"query": {"pages": {
            "1": {"title": "File:바다.jpg", "coordinates": [{"lat": 33.5122, "lon": 126.5270}],
                  "imageinfo": [{"thumburl": "https://x/1.jpg", "descriptionurl": "https://c/1",
                                 "extmetadata": {"Artist": {"value": "<a>홍길동</a>"}, "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]},
            "2": {"title": "File:지도.svg", "imageinfo": [{"thumburl": "https://x/2.png"}]}}}}
    if "wikipedia" in url:
        return {"query": {"pages": {
            "1": {"title": "산지천", "extract": "제주시를 흐르는 하천이다.", "coordinates": [{"lat": 33.5140, "lon": 126.5270}]},
            "2": {"title": "제주지방항공청", "coordinates": [{"lat": 33.5121, "lon": 126.5268}]},
            "3": {"title": "좌표 없는 문서"}}}}
    if "dapi.kakao.com" in url:
        assert headers == {"Authorization": f"KakaoAK {KEY}"}          # 키는 머리글로만, 주소에 넣지 않는다
        assert set(params) <= {"x", "y", "radius", "sort", "size", "category_group_code", "query"}   # 좌표·분류만 보낸다
        if params.get("category_group_code") == "FD6":
            return {"documents": [{"place_name": "시장 국수집", "category_name": "음식점 > 한식 > 국수", "distance": "120",
                                   "x": "126.5268", "y": "33.5121", "phone": "064-000-0000",
                                   "place_url": "http://place.map.kakao.com/1"}]}
        return {"documents": []}
    if "photon" in url:
        near = {"properties": {"name": "가까운 식당", "osm_value": "restaurant"}, "geometry": {"coordinates": [126.5270, 33.5122]}}
        far = {"properties": {"name": "먼 식당", "osm_value": "restaurant"}, "geometry": {"coordinates": [128.70, 36.56]}}
        return {"features": [far, near]}
    raise AssertionError(url)


def boom(url, params, headers=None):
    raise TimeoutError


def kakao_down(url, params, headers=None):
    if "dapi.kakao.com" in url:
        raise TimeoutError
    return fake(url, params, headers)


def test_photos_keep_images_with_credit():
    got = photos(LAT, LNG, fetch=fake)
    assert [p["title"] for p in got] == ["바다"]
    assert got[0]["author"] == "홍길동" and got[0]["license"] == "CC BY-SA 4.0"


def test_sights_drop_offices_and_no_coords():
    got = sights(LAT, LNG, fetch=fake)
    assert [s["title"] for s in got] == ["산지천"]


def test_around_uses_kakao_with_phone_when_key():
    got = around(LAT, LNG, fetch=fake, kakao_key=KEY)
    assert got["source"] == "kakao" and not got["failed"]
    x = got["먹을 곳"][0]
    assert (x["name"], x["type"], x["km"], x["phone"]) == ("시장 국수집", "국수", 0.12, "064-000-0000")


def test_around_without_key_uses_photon_and_filters_by_distance():
    got = around(LAT, LNG, fetch=fake)
    assert got["source"] == "osm" and [x["name"] for x in got["먹을 곳"]] == ["가까운 식당"]


def test_kakao_down_falls_back_to_photon():
    got = around(LAT, LNG, fetch=kakao_down, kakao_key=KEY)
    assert got["source"] == "osm" and [x["name"] for x in got["먹을 곳"]] == ["가까운 식당"]


def test_offline_is_marked_failed_not_empty_area():
    got = place_info(LAT, LNG, fetch=boom, kakao_key=KEY)
    assert got["photos"] == [] and got["sights"] == []
    assert got["around"]["failed"] is True and got["around"]["먹을 곳"] == []
