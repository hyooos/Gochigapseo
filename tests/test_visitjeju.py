"""비짓제주 사진 짝 맞추기: 애매하면 붙이지 않는다(다른 곳 사진은 접근성 판단을 잘못 이끈다)."""
import io
import json

import pandas as pd
import pytest

from jeju_agent.data import visitjeju as vj

CDN = "https://api.cdn.visitjeju.net/photomng/"


def item(title, lat=None, lng=None, cid="C", img=CDN + "a.jpg", kind="관광지"):
    return {"contentsid": cid, "title": title, "latitude": lat, "longitude": lng, "contentscd": {"label": kind},
            "alltag": "화장실,장애인 화장실,실외", "phoneno": "064-783-0959",
            "repPhoto": {"photoid": {"imgpath": img, "thumbnailpath": img.replace("a.jpg", "t.jpg")}}}


def test_norm_drops_bracket_notes():
    assert vj.norm("성산일출봉(UNESCO 세계자연유산)[홍보관 등 실내미개방]") == "성산일출봉"
    assert vj.norm("사려니숲길-무장애나눔길") == "사려니숲길무장애나눔길"


def test_match_rules():
    places = pd.DataFrame([["P1", "성산일출봉", 33.4621, 126.9364], ["P2", "사려니숲길-무장애나눔길", 33.40, 126.62],
                           ["P3", "어영공원", 33.51, 126.49], ["P4", "동문시장", 33.512, 126.528],
                           ["P5", "해녀박물관", 33.52, 126.86], ["P6", "이호테우해변", 33.50, 126.45]],
                          columns=["place_id", "name", "lat", "lng"])
    items = [item("성산일출봉(UNESCO 세계자연유산)", 33.4622, 126.9365, "A"),
             item("성산일출봉", 33.30, 126.30, "far"),                # 이름은 같지만 2km 밖 → 안 붙임
             item("사려니숲길", 33.405, 126.625, "B"),                # 포함 + 가까움 → 붙임
             item("어영", 33.51, 126.49, "short"),                    # 2글자 포함은 너무 느슨 → 안 붙임
             item("동문시장", cid="nocoord", kind="쇼핑"),             # 좌표 없음 + 이름 같음 → 관광지가 아니어도 붙임
             item("제주선물가게 해녀박물관점", 33.52, 126.86, "shop", kind="쇼핑"),   # 포함은 관광지만 → 가게는 안 붙임
             item("제주해녀박물관", 33.521, 126.861, "museum"),
             item("에귤타르트 이호테우해변점", 33.50, 126.45, "tart", kind="음식점"),
             item("동문재래시장", 33.512, 126.528, "bad", img="http://evil/x.jpg")]   # 비짓제주 CDN 아님 → 무시
    m = vj.match(places, items).set_index("place_id")
    assert m.loc["P1", "contentsid"] == "A" and m.loc["P1", "image"] == CDN + "a.jpg" and m.loc["P1", "thumbnail"] == CDN + "t.jpg"
    assert m.loc["P1", "alltag"] == "화장실,장애인 화장실,실외" and m.loc["P1", "phoneno"] == "064-783-0959"
    assert m.loc["P2", "contentsid"] == "B"
    assert "P3" not in m.index
    assert m.loc["P4", "contentsid"] == "nocoord" and pd.isna(m.loc["P4", "distance_km"])
    assert m.loc["P5", "contentsid"] == "museum" and "P6" not in m.index


def test_fetch_all_reads_every_page_and_reports_bad_key(monkeypatch):
    pages = {1: {"items": [item("가")], "pageCount": 2}, 2: {"items": [item("나")], "pageCount": 2}}
    seen = []

    def fake(req, timeout):
        page = int(req.full_url.split("page=")[1].split("&")[0])
        seen.append(page)
        return io.BytesIO(json.dumps(pages[page]).encode())
    monkeypatch.setattr(vj.urllib.request, "urlopen", fake)
    assert [i["title"] for i in vj.fetch_all("k")] == ["가", "나"] and seen == [1, 2]
    monkeypatch.setattr(vj.urllib.request, "urlopen",
                        lambda req, timeout: io.BytesIO(b'{"result":"403","resultMessage":"apiKey is invalid"}'))
    with pytest.raises(ValueError, match="apiKey is invalid"):
        vj.fetch_all("bad")


def test_tag_fills_only_blank_confirmed_facilities():
    places = pd.DataFrame([
        ["P1", None, None, None, None, None, None, None, None],
        ["P2", 0, 1, None, None, None, "조사 원문", None, None],
        ["P3", None, None, None, None, None, None, None, None],
    ], columns=["place_id", "acc_toilet", "acc_parking", "wheelchair_rental", "elevator", "ramp",
                "ev_acc_parking", "ev_acc_toilet", "ev_ramp"])
    tags = pd.DataFrame([
        ["P1", "장애인 화장실, 장애인 전용 주차장, 수동 휠체어 대여 가능, 승강기, 단차없음, 데크"],
        ["P2", "장애인 화장실, 경사로"],
        ["P3", "장애인 주차 없음"],
        ["P9", "엘리베이터"],
    ], columns=["place_id", "alltag"])
    out = vj.fill_blank_facilities(places, tags).set_index("place_id")
    assert out.loc["P1", "acc_toilet"] == 1 and out.loc["P1", "ev_acc_toilet"] == "비짓제주 태그: 장애인 화장실"
    assert out.loc["P1", "acc_parking"] == 1 and out.loc["P1", "wheelchair_rental"] == 1 and out.loc["P1", "elevator"] == 1
    assert pd.isna(out.loc["P1", "ramp"])
    assert out.loc["P2", "acc_toilet"] == 0 and out.loc["P2", "acc_parking"] == 1
    assert out.loc["P2", "ev_acc_parking"] == "조사 원문"
    assert out.loc["P2", "ramp"] == 1 and out.loc["P2", "ev_ramp"] == "비짓제주 태그: 경사로"
    assert pd.isna(out.loc["P3", "acc_parking"])
    assert pd.isna(places.loc[0, "acc_toilet"])


def test_repository_reads_matched_photo(tmp_path):
    from jeju_agent.data import Repository
    d = tmp_path / "data" / "visitjeju"
    d.mkdir(parents=True)
    pd.DataFrame([["P1", "A", "성산일출봉", "0.01", CDN + "t.jpg", CDN + "a.jpg", "실외", ""]], columns=vj.COLUMNS).to_csv(
        d / "visitjeju_photos.csv", index=False, encoding="utf-8-sig")
    repo = Repository("sample", data_dir=tmp_path / "data")
    assert repo.visitjeju_photo("P1")["image"] == CDN + "a.jpg"
    assert repo.visitjeju_photo("P9") is None


# ── 비짓제주 태그로 채운 시설의 출처 표시 ─────────────────────────────
def _explore(resp):
    return next(r for r in resp.results if r.agent == "explore")


def test_explore_lists_visitjeju_for_places_not_status(orch):
    """장소 찾기·코스는 비짓제주 자료를 '쓴 자료'(D1)에 올리고, 현황은 조사 자료 기준이라 뺀 이유를 D2에 남긴다."""
    acc = _explore(orch.ask("휠체어로 서귀포 갈 만한 곳"))
    assert "vj_tags" in acc.payload["selected"]
    d1 = next(e for e in acc.evidence if e.id == "D1")
    row = next(r for r in d1.rows if r[0].startswith("비짓제주"))
    assert row[1].startswith("비짓제주") and row[6] == "2026-11-16 받음"
    st = _explore(orch.ask("제주시 읍면동별 장애인 화장실 현황"))
    assert "vj_tags" not in st.payload["selected"] and "현황은 조사 자료 기준" in st.payload["skip_reasons"]["vj_tags"]


def test_tag_filled_facility_is_not_quoted_as_survey_text(orch):
    """태그로 채운 시설은 카드에 '원문:'으로 인용하지 않고, 근거표 출처에 비짓제주를 밝힌다."""
    from jeju_agent.report.cards import facility_lines
    r = orch.ask("오설록 화장실 있어?").results[-1]
    a5 = next(e for e in r.evidence if e.id == "A5")
    assert a5.rows[0][2] == "있음" and a5.dataset.startswith("비짓제주") and a5.rows[0][4].endswith("받음")
    card = next(c for c in r.payload["cards"] if c["place_id"] == "P014")
    line = next(x for x in facility_lines(card) if "장애인 화장실" in x)
    assert "원문" not in line and "비짓제주 태그" in line
    a1 = next(e for e in orch.ask("휠체어로 제주 전체 갈 만한 곳").results[-1].evidence if e.id == "A1")
    assert "비짓제주" in a1.dataset


def test_no_fill_when_visitjeju_not_selected(orch):
    """데이터 탐색이 비짓제주 자료를 고르지 않았으면 조사 빈칸을 채우지 않는다(조사 자료만으로 판정)."""
    from jeju_agent.agents.access import AccessAgent
    from jeju_agent.agents.base import AgentContext
    from jeju_agent.contracts import AnalysisRequest, Intent, TravelerProfile
    req = AnalysisRequest(question="t", intent=Intent.ACCESS, place_ids=["P014"],
                          profile=TravelerProfile(needs=["acc_toilet"]), params={"datasets": ["bf_places", "bf_points"]})
    ctx = AgentContext(repo=orch.repo, settings=orch.s, memory={}, llm=None, call=lambda n, r: None)
    res = AccessAgent().run(req, ctx)
    v = next(x for x in res.payload["verdicts"] if x["place_id"] == "P014")
    assert v["verdict"] == "주의" and "비짓제주" not in res.evidence[0].dataset


def test_fill_count_is_question_scope(orch):
    """'조사 빈칸 n칸 보완'은 이번 질문에서 판정한 장소만 센다(조사 표 전체가 아니라)."""
    def filled(q):
        r = orch.ask(q, {})
        acc = [x for x in r.results if x.agent == "access"][-1]
        line = next(t.detail for t in acc.trace if t.action == "fill:visitjeju")
        return int(line.split("조사 빈칸 ")[1].split("칸")[0])
    one, part, whole = filled("오설록 화장실 있어?"), filled("휠체어로 서귀포 갈 만한 곳"), filled("휠체어로 제주 전체 갈 만한 곳")
    assert 0 < one < part < whole
