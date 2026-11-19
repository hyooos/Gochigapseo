"""이용자 조건(장애·건강 정보)과 원본 값이 기록·로그·LLM 관찰값에 새지 않는지."""
import json

from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator
from jeju_agent.ui_helpers import a11y_css, kakao_map_html, tts_html

SENSITIVE = ["휠체어", "wheelchair", "시각장애", "blind", "화장실 필요", "acc_toilet"]


def test_trace_has_no_profile(orch):
    resp = orch.ask("시각장애가 있고 휠체어를 타요. 장애인 화장실 필요. 서귀포 2일 코스 짜줘")
    steps = resp.trace + [t for r in resp.results for t in r.trace]
    text = json.dumps([s.model_dump() for s in steps], ensure_ascii=False)
    assert not any(w in text for w in SENSITIVE)


def test_usage_log_has_no_question_text(api_settings, repo, tmp_path):
    llm = LLMClient(api_settings, runtime_dir=tmp_path, transport=lambda m, model: ('{"intent":"access"}', 10, 5))
    Orchestrator(api_settings, repo=repo, llm=llm).ask("휠체어 타는 어머니와 갈 만한 곳")
    log = (tmp_path / "llm_usage.jsonl").read_text(encoding="utf-8")
    assert "휠체어" not in log and "어머니" not in log


def test_clear_cache(api_settings, tmp_path):
    llm = LLMClient(api_settings, runtime_dir=tmp_path, transport=lambda m, model: ("답", 10, 5))
    llm.chat_text("s", "휠체어", purpose="x")
    assert llm._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 1
    llm.clear_cache()
    assert llm._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0


def test_stt_off_by_default(settings, tmp_path):
    text, rec = LLMClient(settings, runtime_dir=tmp_path).transcribe(b"RIFF")
    assert text is None and rec.error == "stt_off"


def test_html_snippets_escape_script_breakout():
    h = tts_html("</script><script>alert(1)</script> 안녕하세요 [근거표 A1] [근거표 A2, C1] 뺐습니다(이유: 근거표 A2). 1일차 가 → 나")
    assert "</script><script>alert" not in h and "안녕하세요" in h
    assert "근거표" not in h and "C1" not in h and "뺐습니다." in h and "가, 나" in h          # 근거표 번호는 소리 내어 읽지 않는다
    k = kakao_map_html([{"name": "</script>x", "lat": 33.4, "lng": 126.5, "day": 1, "order": 1}], "KEY\"<")
    assert "</script>x" not in k and "KEY&quot;&lt;" in k
    assert "font-size: 118%" in a11y_css(True, False) and "#ffff00" in a11y_css(False, True)
    assert "focus-visible" in a11y_css(False, False)


def test_photo_on_route_map(tmp_path):
    """경로 위 사진: 작은 JPEG data URI(정사각형 자르기)와 카카오맵 이름표 안 사진."""
    from PIL import Image

    from jeju_agent.ui_helpers import photo_data_uri
    p = tmp_path / "a.jpg"
    Image.new("RGB", (800, 600), "#3a7").save(p)
    small = photo_data_uri(str(p), 96, ratio=1)
    assert small.startswith("data:image/jpeg;base64,") and len(small) < 20_000
    big = photo_data_uri(str(p), 240)
    with Image.open(__import__("io").BytesIO(__import__("base64").b64decode(big.split(",", 1)[1]))) as im:
        assert im.size == (240, 180)                                    # 비율 유지
    k = kakao_map_html([{"name": "가", "lat": 33.4, "lng": 126.5, "day": 1, "order": 1, "photo": small}], "KEY")
    assert small in k and "img.alt" in k


def test_map_is_numbers_plus_side_list():
    """지도: 이름표 대신 번호 점 + 왼쪽 위 목록. 번호는 늘 제자리에 하나씩(예전 '3,4' 모으기는 확대해도 앞 번호 자리에 붙어 틀려 보여 없앰).
    거의 같은 자리(약 25m 안)는 spread로 옆으로 비켜 둘 다 보이게."""
    import json
    import re
    pts = [{"name": n, "lat": lat, "lng": 126.5, "day": 1, "order": k, "label": "1일차"} for k, (n, lat) in enumerate(
        [("가", 33.50), ("나", 33.30), ("다", 33.3001)], 1)]
    k = kakao_map_html(pts, "KEY")
    P = json.loads(re.search(r"const P = (.*);", k).group(1))
    assert [p["num"] for p in P] == ["1", "2", "3"]
    assert [p["spread"] for p in P] == [0, 0, 1]
    assert P[1]["tip"] == "나 · 1일차"
    assert "legend" in k and "목록 접기" in k


def test_kakao_days_side_by_side_and_clear_shop_icons():
    """같은 길을 여러 날이 지나면 날짜마다 비켜 나란히(확대하면 다시 계산), 식당은 주황 동그라미·포크, 숙소는 보라 네모·침대(일정 목록과 같은 흰 그림),
    식당·숙소는 번호 점(30px)보다 살짝 작게(26px)."""
    from jeju_agent.ui_helpers import BED_D
    pts = [{"name": "가", "lat": 33.5, "lng": 126.5, "day": 1, "order": 1}, {"name": "나", "lat": 33.51, "lng": 126.51, "day": 1, "order": 2},
           {"name": "다", "lat": 33.5, "lng": 126.5, "day": 2, "order": 3}, {"name": "라", "lat": 33.51, "lng": 126.51, "day": 2, "order": 4}]
    shops = [{"lat": 33.505, "lng": 126.505, "kind": "restaurant", "name": "점심 가", "label": "1일차"},
             {"lat": 33.506, "lng": 126.506, "kind": "lodging", "name": "숙소 나", "label": "1일차"}]
    k = kakao_map_html(pts, "KEY", extras=shops)
    assert "zoom_changed" in k and "containerPointFromCoords" in k and "const GAP" in k
    assert '"kind": "meal"' in k and '"kind": "lodge"' in k and "#d96a0b" in k and "#5b4a9e" in k
    assert "🛏" not in k and "🍴" not in k   # 작은 그림 글자 대신 색 표시 + 흰 그림
    assert BED_D in k and ".shop {" in k and "width: 26px" in k and "min-width: 30px" in k
