"""새 화면(web/) — FastAPI 서버를 TestClient로 실제로 돌려 본다(키 없음, 인터넷 없음)."""
from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import web.server as srv  # noqa: E402
from jeju_agent.config import load_settings  # noqa: E402
from jeju_agent.data import Repository  # noqa: E402
from jeju_agent.llm import LLMClient  # noqa: E402
from jeju_agent.orchestrator import Orchestrator  # noqa: E402
from web import jeju_map  # noqa: E402

# 개발자 PC의 .env(API 키·카카오 키 등)가 섞이지 않게 비우는 값 — conftest.settings 와 같다
ENV_KEYS = ("LLM_MODE", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "DATA_MODE", "JEJU_DATAHUB_KEY", "APP_NAME",
            "STT_MODE", "STT_MODEL", "KAKAO_JS_KEY", "KAKAO_REST_KEY", "LLM_MAX_CALLS_PER_QUESTION", "AGENT_LLM",
            "AGENT_MAX_STEPS", "LLM_TOOL_CALLING", "LLM_CACHE", "PLACE_INFO_ONLINE")


def wait(c: TestClient, job: str) -> list[str]:
    """SSE를 끝까지 읽어 단계 글을 돌려준다. 마지막은 done이어야 한다."""
    steps, last = [], None
    with c.stream("GET", f"/api/events/{job}") as r:
        for line in r.iter_lines():
            if line.startswith("event:"):
                last = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and last is None:
                steps.append(json.loads(line[5:])["text"])
            if last:
                break
    assert last == "done", steps[-3:]
    return steps


def trip(c: TestClient, **v) -> list[str]:
    form = {"mob": ["wheelchair_manual"], "needs": ["acc_toilet"], "region": "제주 전체", "days": "2", "what": "course",
            "extras": ["lunch", "lodging"], "start": "2026-12-01", **v}
    r = c.post("/api/trip", data={"f": json.dumps(form)})
    return wait(c, r.json()["job"])


@pytest.fixture()
def c(monkeypatch, tmp_path):
    """항상 오프라인·샘플 센터 자료로 서버를 띄운다(.env에 API 키가 있어도 실제 LLM·카카오를 부르지 않음)."""
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PLACE_INFO_ONLINE", "0")
    st = load_settings(env_file=tmp_path / "none.env")
    assert st.llm_mode == "offline" and not st.kakao_js_key
    srv.configure(st, Orchestrator(st, repo=Repository("sample"), llm=LLMClient(st, runtime_dir=tmp_path / "rt")))
    with TestClient(srv.app) as client:
        client.get("/")   # 세션 쿠키
        yield client


def test_home_has_wizard_and_source_notice(c):
    r = c.get("/")
    assert r.status_code == 200
    assert 'id="wizard"' in r.text and "제주테크노파크와 관련이 없음" in r.text


def test_course_flow_result_dialog_and_downloads(c):
    steps = trip(c)
    assert any("질문 이해" in s for s in steps) and any("검증" in s for s in steps)   # 실제 에이전트 단계가 실시간으로 온다
    page = c.get("/").text
    assert 'id="result-trip"' in page and "일차" in page and "근거 자료표" in page
    pid = page.split('data-open="', 1)[1].split('"', 1)[0]
    d = c.get(f"/place/trip/{pid}")
    assert d.status_code == 200 and "전화로 물어볼 말" in d.text
    png = c.get("/download/trip/itinerary.png")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    md = c.get("/download/trip/report.md")
    assert md.status_code == 200 and "제주테크노파크와 관련이 없음" in md.text
    assert c.get("/download/trip/chart/0.png").content[:4] == b"\x89PNG"


def test_chat_edit_replaces_trip_and_compares(c):
    trip(c)
    r = c.post("/api/ask", data={"q": "1일차 빡빡해"}).json()
    assert "job" in r   # 고칠 것이 분명한 말은 확인 없이 바로 실행
    wait(c, r["job"])
    page = c.get("/").text
    assert "위의 결과를 새로 바꿨어요" in page and "이번에 바뀐 점" in page


def test_new_question_asks_confirmation_first(c):
    r = c.post("/api/ask", data={"q": "제주시 읍면동별 무장애 관광지 현황 보여줘"}).json()
    assert "confirm" in r
    r = c.post("/api/ask", data={"q": "", "confirmed": 1}).json()
    wait(c, r["job"])
    assert "그래프" in c.get("/").text


def test_chat_only_answer_shows_evidence_and_trace(c):
    """11/19 실측: 입력 폼 없이 말로만 물으면 '근거 보기'가 갈 곳(맨 아래 근거와 검증)이 없었다(입력 화면이라 숨김)."""
    r = c.post("/api/ask", data={"q": "제주시 읍면동별 무장애 관광지 현황 보여줘"}).json()
    r = c.post("/api/ask", data={"q": "", "confirmed": 1}).json()
    wait(c, r["job"])
    page = c.get("/").text
    assert "근거와 검증" in page and "<summary>분석 기록</summary>" in page and 'id="ev-chat0"' in page


def test_profile_not_logged_to_disk(c):
    """이용자 조건은 세션 메모리에만: 분석하는 동안 새로 쓰이거나 바뀐 파일에 조건 낱말이 없다.
    (출력 폴더에는 평가 스크립트 보고서처럼 원래 있던 파일이 있을 수 있어, 이 테스트가 만든·고친 파일만 본다)"""
    import time

    from jeju_agent.config import OUTPUT_DIR, ROOT
    start = time.time() - 1
    trip(c)
    for folder in (OUTPUT_DIR, ROOT / "runtime", ROOT / "logs"):
        for p in folder.rglob("*") if folder.exists() else []:
            if p.is_file() and p.stat().st_mtime >= start and p.suffix in {".json", ".jsonl", ".log", ".txt", ".md"}:
                assert "수동 휠체어" not in p.read_text(encoding="utf-8", errors="ignore"), p


def test_map_projection_and_zoom():
    x1, _ = jeju_map.xy(33.5, 126.2)
    x2, _ = jeju_map.xy(33.5, 126.9)
    assert x1 < x2   # 동쪽이 오른쪽
    vb, z = jeju_map.focus([(33.25, 126.40), (33.24, 126.42)])
    assert z > 1 and len(vb.split()) == 4
    assert jeju_map.focus([(33.2, 126.17), (33.56, 126.95)])[1] < 1.6   # 섬 전체에 퍼지면 가장자리 바다만 조금 잘라 낸다


def test_kakao_tab_online_nearby_and_voice(c, monkeypatch):
    """키가 있을 때: 카카오 지도 탭(처음 누를 때 불러옴), 장소 창의 주변 공개 정보, 말로 묻기. 실제 네트워크는 쓰지 않는다."""
    import dataclasses

    import web.server as srv
    monkeypatch.setattr(srv, "s", dataclasses.replace(srv.s, kakao_js_key="test-js-key", place_info_online=True,
                                                      llm_mode="api", llm_api_key="dummy", stt_mode="api"))
    fake = {"sights": [{"title": "가상 오름", "url": "https://ko.wikipedia.org/wiki/x", "km": 0.8}],
            "around": {"먹을 곳": [{"name": "가상 식당", "type": "한식", "km": 0.3, "url": ""}], "숙소": [], "source": "photon"},
            "photos": []}
    monkeypatch.setattr(srv, "place_info", lambda lat, lng, kakao_key="": fake)
    srv.nearby_info.cache_clear()
    trip(c)
    page = c.get("/").text
    assert 'data-maptab="kakao"' in page and "data-srcdoc=" in page and "dapi.kakao.com" in page
    # 카카오 키가 있으면 카카오 지도가 기본(첫 탭, 그림 지도는 숨김), 번호 점은 옆 일정 카드로 이동
    assert page.index('data-maptab="kakao"') < page.index('data-maptab="art"')
    assert '<div class="mapart" hidden>' in page and "kakaoStop" in page and "옆 일정에서 그 장소 카드" in page
    pid = page.split('data-open="', 1)[1].split('"', 1)[0]
    d = c.get(f"/place/trip/{pid}").text
    assert "주변에 뭐가 있나요" in d and "가상 식당" in d and "휠체어로 들어갈 수 있는지가 없어요" in d
    monkeypatch.setattr(srv.orch.llm, "transcribe", lambda audio, filename="": ("2일차 빡빡해", None))
    r = c.post("/api/stt", files={"audio": ("voice.webm", b"fake", "audio/webm")}).json()
    assert r == {"text": "2일차 빡빡해"}
    assert 'id="micBtn"' in c.get("/").text
    srv.nearby_info.cache_clear()


def test_screen_follows_photo_dataset_choice(c, monkeypatch):
    """데이터 탐색이 조사 사진 자료를 고르지 않으면 화면도 사진을 쓰지 않는다(예전 Streamlit 화면 테스트를 옮김)."""
    from jeju_agent.agents import explore
    orig = explore.rule_options
    monkeypatch.setattr(explore, "rule_options", lambda req: {**orig(req), "photos": False})
    trip(c)
    page = c.get("/").text
    assert 'id="result-trip"' in page and "/photo/" not in page and "사진 없음" in page


def test_restored_streamlit_features(c):
    """예전 화면에 있던 것: 이해한 조건 한 줄, 분석 기록, 보고서 미리 보기, 버스 정류소, 지난 답 다시 보기,
    대화 답 내려받기는 현황·보고서 정리에만(D30)."""
    trip(c, trans="bus")
    page = c.get("/").text
    assert 'class="understood"' in page and "<summary>분석 기록</summary>" in page and "보고서 미리 보기" in page
    assert "class='bus'" in page and "가장 가까운 정류소" in page
    prev = c.get("/download/trip/report.md?view=1")
    assert prev.status_code == 200 and prev.headers["content-type"].startswith("text/plain")
    r = c.post("/api/ask", data={"q": "1일차 빡빡해"}).json()   # 위 결과를 고치는 말 → 위로 옮겨진다
    wait(c, r["job"])
    r = c.post("/api/ask", data={"q": "서귀포매일올레시장 화장실 있어?"}).json()   # 짧은 대화 답
    if "confirm" in r:
        r = c.post("/api/ask", data={"q": "", "confirmed": 1}).json()
    wait(c, r["job"])
    page = c.get("/").text
    last = page.split('id=chat-last', 1)[1] if "id=chat-last" in page else page.split('id="chat-last"', 1)[1]
    assert "/download/chat" not in last   # 짧은 대화 답에는 내려받기를 붙이지 않는다
    r = c.post("/api/ask", data={"q": "보고서로 정리해줘"}).json()   # 보고서 정리는 내려받기가 붙는다
    wait(c, r["job"])
    page = c.get("/").text
    assert "/download/chat2/report.md" in page
    assert 'data-again="1"' in page   # 앞선 짧은 답(1번)은 접혀 있고 '자세히 다시 보기'로 다시 그린다
    assert c.post("/api/again", data={"i": 1}).json() == {"ok": True}
    assert 'id=chat-last' in c.get("/").text


def test_logo_starts_over_and_intro_map(c):
    """로고(/new)를 누르면 결과·대화를 지우고 첫 화면으로. 첫 화면은 그림 지도 이미지, 결과 지도는 확대·이동되는 SVG."""
    home = c.get("/").text
    assert "intro_map.webp" in home and 'href="/new"' in home and "갈 수 있는 곳" in home
    assert c.get("/static/img/intro_map.webp").status_code == 200
    trip(c)
    page = c.get("/").text
    assert 'id="result-trip"' in page and "intro_map.webp" not in page and 'data-has-result="1"' in page
    assert 'data-zoom="in"' in page and "class='mk'" in page and "--z:" in page   # 확대 단추, 크기 고정 표시
    r = c.get("/new", follow_redirects=True)
    assert r.status_code == 200 and 'id="wizard"' in r.text and 'id="result-trip"' not in r.text and 'data-has-result="0"' in r.text


def test_map_auto_zoom_separates_clustered_course():
    """한 동네에 몰린 코스는 더 크게 확대해서 시작한다(번호가 겹치지 않게)."""
    near = [(33.512, 126.512), (33.515, 126.517), (33.510, 126.522)]   # 제주시 용담·삼도 근처 약 1km
    _, z = jeju_map.focus(near)
    assert z > 5


def test_page_script_parses():
    """화면 스크립트에 문법 오류가 있으면 입력·지도·창이 모두 멈춘다. Node가 있는 PC에서만 검사한다(없으면 건너뜀)."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    js = Path(srv.__file__).parent / "static" / "js" / "app.js"
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_explain_is_tidied_into_lead_keys_and_cautions():
    """'왜 이렇게 골랐나요': 한 덩어리 문단을 요약 한 줄 + 핵심 목록 + 확인할 점으로(내용은 그대로). 목록이 이미 있으면 그대로."""
    from web.views import explain_html
    t = ("제주 2일 코스는 1일차에 가, 2일차에 나를 둘러봅니다. 하루 이동은 12분에서 28분 사이입니다 [근거표 C1]. "
         "입장료가 확인된 3곳은 무료입니다 [근거표 C2]. 식당은 영업 여부를 방문 전에 전화 확인이 필요합니다 [근거표 C5]. "
         "이동 시간은 추정치가 포함되어 참고 바랍니다.")
    h = explain_html(t, tidy=True)
    assert h.startswith("<p class='ex-lead'>제주 2일 코스는")
    assert h.count("<li>") == 4 and "<h3>확인할 점</h3>" in h
    keys, warn = h.split("ex-warn")
    assert "12분에서 28분" in keys and "무료" in keys and "전화 확인" in warn and "추정치" in warn
    assert "(자료표 C1)" in h
    assert "ex-lead" not in explain_html("- 이미 목록\n- 둘째", tidy=True)
    assert "ex-lead" not in explain_html(t)   # 대화 답변 등은 그대로


def test_meal_lodge_icons_same_white_svg_in_list_and_maps(c):
    """일정 목록의 식당·숙소 표시도 지도와 같은 흰 그림(그림 글자는 기기마다 회색으로 보여 안 씀)."""
    from jeju_agent.ui_helpers import BED_D, FORK_D
    trip(c)
    page = c.get("/").text
    assert "🍴" not in page and "🛏" not in page
    assert 'class="stopby meal' in page and 'class="stopby lodge' in page and FORK_D in page and BED_D in page


def test_result_order_history_and_changes(c):
    """가져가기 → 더 물어보기(이 결과에 이어 물을 버튼) → 맨 아래 근거와 검증. 대화로 고치면 결과 맨 위에 '이번에 바뀐 점',
    왼쪽 결과 기록으로 이전 결과를 읽기만으로 보고, '처음으로'(/new)를 누르면 기록이 지워진다."""
    trip(c)
    page = c.get("/").text
    assert page.index("가져가기") < page.index('id="chatT"') < page.index('id="laterT"')
    assert "<h3>이어서 물어보기</h3>" not in page and "눌러서 바로 물어보기" in page and 'data-ask="2일차 빡빡해"' in page
    assert page.index("센터 자료") > page.index('id="chatT"') if "넣으면 더 정확해져요" in page else True
    assert 'class="hist"' not in page   # 결과가 하나면 기록 탭 없음

    wait(c, c.post("/api/ask", data={"q": "2일차 빡빡해"}).json()["job"])
    page = c.get("/").text
    import re
    assert "이번에 바뀐 점" in page and re.search(r"2일차 \d곳 → \d곳: .+ 빠짐", page) and "바뀐 날" in page
    assert 'class="hist"' in page and "처음 결과" in page and "‘2일차 빡빡해’ 반영" in page and 'aria-current="page"' in page
    old = c.get("/?v=0").text
    assert "이전 기록" in old and 'id="chatT"' not in old and "이번에 바뀐 점" not in old
    assert "/download/ver0/report.md" in old and c.get("/download/ver0/report.md").status_code == 200

    c.get("/new")
    assert 'class="hist"' not in c.get("/").text


def test_changes_text_for_course_edits(settings):
    from jeju_agent.compare import changes
    from jeju_agent.contracts import TravelerProfile
    from jeju_agent.orchestrator import Orchestrator
    o, mem = Orchestrator(settings), {}   # 개발자 .env(API 키)가 아니라 오프라인 설정으로 — 실제 모델을 부르지 않게
    form = {"profile": TravelerProfile(), "days": 2, "region": "제주시", "intent": "course", "origin": None,
            "start_date": "2026-11-21", "extras": ["lunch"]}
    a = o.execute(o.plan_form("q", form), mem)
    b = o.execute(o.plan("3일로 늘려줘", mem, ask_profile=True), mem)
    ch = changes(a, b)
    texts = [r["text"] for r in ch["rows"]]
    assert any("2일 코스 → 3일 코스" in t for t in texts) and any(t.startswith("3일차가 새로 생겼어요") for t in texts)
    assert ch["new_ids"] and 3 in ch["changed_days"]
    assert changes(a, a)["rows"][0]["kind"] == "same"


def test_meal_lodge_card_has_bold_basics_and_more(c):
    """식당·숙소 칸: 이름·전화·화장실·출입을 위에 한눈에, 출입 메모·전화로 물어볼 말·지도 찾기는 '더 보기'에."""
    trip(c)
    page = c.get("/").text
    box = page[page.index('class="stopby meal'):]
    box = box[:box.index("sb-src")]
    assert 'class="sb-chips"' in box and "휠체어 화장실" in box and "휠체어 출입" in box
    assert 'class="sb-more"' in box and "지도에서 찾기" in box and "map.kakao.com" in box
    assert "가기 전에 영업 여부를 전화로 확인하세요" in page


def test_app_starts_without_lifespan(monkeypatch, tmp_path):
    """CI 시작 검사처럼 `with` 없이 TestClient(app).get('/')만 불러도 뜬다(설정은 첫 요청 때 읽음)."""
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PLACE_INFO_ONLINE", "0")
    monkeypatch.setattr(srv, "orch", None)
    monkeypatch.setattr(srv, "get_settings", lambda: load_settings(env_file=tmp_path / "none.env"))
    monkeypatch.setattr(srv, "s", srv.s)
    r = TestClient(srv.app).get("/")
    assert r.status_code == 200 and srv.orch is not None


def test_view_toggles_and_static_version(c):
    """큰 글씨 클래스가 사진용 .big이랑 겹치면 안 됨, css·js 주소엔 버전이 붙어야 함 (예전 app.js 캐시 방지)"""
    page = c.get("/").text
    assert 'data-view="bigtext"' in page and 'data-view="big"' not in page and 'id="viewMsg"' in page
    assert re.search(r'/static/css/app\.css\?v=\d+', page) and re.search(r'/static/js/app\.js\?v=\d+', page)
    assert c.get(re.search(r'/static/js/app\.js\?v=\d+', page).group(0)).status_code == 200


def test_trip_bad_input_returns_json_error(c):
    """입력값이 이상하면 500 말고 JSON 오류 (화면이 이유를 보여 줌)"""
    r = c.post("/api/trip", data={"f": json.dumps({"people": "99", "what": "course"})})
    assert r.status_code == 400 and "error" in r.json()


def test_unknown_place_question_does_not_replace_trip(c):
    """자료에 없는 장소를 물어도 위 코스는 그대로, 대화 답으로 '자료에 없음'"""
    trip(c)
    before = c.get("/").text.count('class="day')
    r = c.post("/api/ask", data={"q": "성산일출봉 화장실 있어?"}).json()
    if "job" in r:
        wait(c, r["job"])
    page = c.get("/").text
    assert "자료에 없어서" in page and "위의 결과를 새로 바꿨어요" not in page
    assert page.count('class="day') == before
