"""가족 공유 일정표 PNG: 큰 글씨 한 장, 출처 문구·[실데이터]/[가상], 이용자 조건은 넣지 않음."""
import io

from PIL import Image

from jeju_agent.provenance import SOURCE_NOTICE
from jeju_agent.report.share import itinerary_png, share_lines


def course(orch, q="휠체어로 서귀포 2일 코스 짜줘"):
    return orch.ask(q, {}).results[-1]


def test_lines_have_every_stop_source_and_labels(orch):
    c = course(orch)
    text = "\n".join(t for _, t in share_lines(c, 3))
    for p in c.payload["itinerary"]:
        assert p["name"] in text
    assert SOURCE_NOTICE in text and "[실데이터]" in text
    assert "1일차" in text and "2일차" in text and "방문 전 전화로 확인" in text


def test_no_user_condition_or_call_script(orch):
    """다른 사람에게 건너가는 파일이라 이용자의 장애·건강 조건과 '꼭 필요' 표시, 전화 대본을 넣지 않는다."""
    c = course(orch, "휠체어로 서귀포 2일 코스 짜줘 장애인 화장실 꼭 있어야 해")
    text = "\n".join(t for _, t in share_lines(c))
    for word in ("휠체어를 쓰는", "수동 휠체어", "전동 휠체어", "꼭 필요", "안녕하세요"):
        assert word not in text, word


def test_png_is_one_tall_page(orch):
    png = itinerary_png(course(orch), 3)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.width >= 1200 and img.height > img.width * 0.9


def test_png_draws_photos_when_given(orch, repo):
    c = course(orch)
    ph = {x["place_id"]: (repo.photos(x["place_id"]) or repo.photos(x["place_id"], "route"))[0][0]
          for x in c.payload["itinerary"] if repo.photos(x["place_id"]) or repo.photos(x["place_id"], "route")}
    with_ph, without = itinerary_png(c, 3, ph), itinerary_png(c, 3)
    assert Image.open(io.BytesIO(with_ph)).size == Image.open(io.BytesIO(without)).size   # 사진 유무와 상관없이 같은 틀
    assert (with_ph != without) == bool(ph)


def test_long_course_fits(orch):
    c = orch.ask("휠체어로 제주 5일 코스 짜줘", {}).results[-1]
    img = Image.open(io.BytesIO(itinerary_png(c)))
    assert img.height > 3000   # 날짜·장소 수에 맞춰 세로로 늘어난다


def test_same_basic_facilities_regardless_of_condition(orch):
    """조건(장애인 주차만 필요 등)과 상관없이 일정표에는 같은 기본 시설 4가지가 보인다(조건이 드러나지 않게)."""
    a = orch.ask("장애인 주차 되는 서귀포 2일 코스 짜줘", {}).results[-1]
    b = orch.ask("휠체어로 서귀포 2일 코스 짜줘", {}).results[-1]
    for c in (a, b):
        for card in c.payload["cards"]:
            assert [f["key"] for f in card["basic_facilities"]] == ["acc_toilet", "acc_parking", "ramp", "elevator"]


def test_dates_and_fee_on_share(orch):
    from jeju_agent.text import date_label, fee_text
    r = orch.execute(orch.plan_form("코스", {"profile": {}, "days": 2, "region": "제주 전체", "intent": "course",
                                            "start_date": "2026-11-28"}), {})
    assert r.plan.request.month == 11 and "11/28(토) 출발" in r.plan.understood
    lines = share_lines(r.results[-1], 11, "2026-11-28")
    assert ("sub", "2일 · 11/28(토) ~ 11/29(일)") in lines or any(t.endswith("11/28(토) ~ 11/29(일)") for k, t in lines if k == "sub")
    assert ("day", "2일차 11/29(일)") in lines
    assert date_label("2026-11-30", 2) == "12/1(화)" and fee_text(0) == "입장료 없음" and fee_text(None) is None
    assert itinerary_png(r.results[-1], 11, None, "2026-11-28")[:4] == b"\x89PNG"


def test_meals_lodging_transport_but_no_condition(orch):
    """새 일정표: 식당·숙소와 이동 수단은 넣고, 이용자 장애·건강 조건과 출입 원문(조건 낱말이 섞일 수 있음)은 넣지 않는다."""
    from jeju_agent.contracts import TravelerProfile
    form = {"profile": TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"]), "days": 2, "region": "제주 전체",
            "intent": "course", "origin": None, "start_date": "2026-11-21", "extras": ["lunch", "dinner", "lodging"]}
    c = orch.execute(orch.plan_form("제주 전체 2일 여행 코스", form), {}).results[-1]
    lines = share_lines(c, 11, "2026-11-21", "자가용·렌터카")
    kinds = {k for k, _ in lines}
    assert ("transport", "자가용·렌터카로 다녀요") in lines and "note" in kinds
    if c.payload.get("extras"):
        assert "extra" in kinds and any(t.startswith(("점심", "저녁", "숙소")) for k, t in lines if k == "extra")
    text = "\n".join(t for _, t in lines)
    for word in ("수동 휠체어", "전동 휠체어", "꼭 필요", "안녕하세요"):
        assert word not in text, word
    assert itinerary_png(c, 11, None, "2026-11-21", "자가용·렌터카")[:4] == b"\x89PNG"
