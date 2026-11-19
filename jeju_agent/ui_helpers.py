"""화면용 HTML 조각. 문자열만 만들어서 화면 없이도 테스트 가능"""
from __future__ import annotations

import base64
import html
import io
import json
import re
from functools import lru_cache

from PIL import Image, ImageOps


@lru_cache(maxsize=512)
def photo_data_uri(path: str, px: int, ratio: float | None = None) -> str:
    """사진 → 작은 JPEG data URI (파일 서버 없이 화면·지도 말풍선에 씀)
    ratio(높이/너비) 주면 가운데 잘라서 맞춤 (동선 타일은 0.75), 없으면 비율 유지
    """
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im = ImageOps.fit(im, (px, round(px * ratio))) if ratio else ImageOps.contain(im, (px, px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=75)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _js(value) -> str:
    """<script>에 넣을 JS 값. '</script>'로 끊기면 안 돼서 '<'를 바꿈"""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def tts_html(text: str, label: str = "🔊 읽어 주기") -> str:
    """누르면 한국어로 읽어 줌. 다시 누르면 멈춤"""
    # 브라우저 내장 음성 씀(밖으로 안 보냄). 근거표 번호는 빼고, 화살표는 쉼표로 읽음
    clean = re.sub(r"\s*[\[(](?:이유:\s*)?근거표[^\])]*[\])]", "", text).replace(" → ", ", ")
    return f"""<div style="font-family:sans-serif">
<button id="b" style="font-size:16px;padding:8px 16px;border-radius:999px;border:1px solid #9aa7b5;background:#fff;cursor:pointer"
 aria-label="답변을 소리 내어 읽기">{html.escape(label)}</button>
<script>
const t = {_js(clean)};
const b = document.getElementById('b');
b.onclick = () => {{
  const s = window.speechSynthesis;
  if (!s) {{ b.textContent = '이 브라우저는 읽어 주기를 지원하지 않습니다'; return; }}
  if (s.speaking) {{ s.cancel(); b.textContent = {_js(label)}; return; }}
  const u = new SpeechSynthesisUtterance(t); u.lang = 'ko-KR'; u.rate = 0.95;
  u.onend = () => {{ b.textContent = {_js(label)}; }};
  s.speak(u); b.textContent = '⏹ 멈추기';
}};
</script></div>"""


DAY_COLORS = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9"]


# 식당(포크·나이프)·숙소(침대) 흰 그림. 일정 목록·지도·범례 전부 이걸 씀 (이모지는 기기마다 모양이 달라서 안 씀)
FORK_D = ("M6 2h1.6v6.2h1.1V2h1.6v6.2h1.1V2H13v7.4a2.6 2.6 0 0 1-2 2.5V22H8.6V11.9a2.6 2.6 0 0 1-2-2.5z"
          "M17.4 2C19 2 20 4.4 20 7.6v5.2h-2v9.2h-2.2V2z")
BED_D = ("M2 5h2.2v7.6h17.8V19h-2.2v-2.2H4.2V19H2z"
         "M5.6 9.6a2.1 2.1 0 1 0 4.2 0a2.1 2.1 0 1 0-4.2 0z"
         "M11 7.6h7.6a3.4 3.4 0 0 1 3.4 3.4v.6H11z")
ICON_D = {"meal": FORK_D, "lodge": BED_D}


def icon_svg(kind: str, size: int = 14) -> str:
    """식당('meal')·숙소('lodge') 흰 그림 (인라인 SVG)"""
    return (f"<svg viewBox='0 0 24 24' width='{size}' height='{size}' aria-hidden='true' focusable='false'>"
            f"<path fill='#fff' d='{ICON_D[kind]}'/></svg>")


FORK_SVG, BED_SVG = icon_svg("meal"), icon_svg("lodge")


def kakao_map_html(points: list[dict], app_key: str, height: int = 460, extras: list[dict] | None = None) -> str:
    """카카오맵: 번호 점 + 날짜별 경로선 + 식당·숙소
    같은 길 지나는 날은 몇 픽셀씩 비켜 그림. points: name, lat, lng, (day, order, color, …)
    """
    # KAKAO_JS_KEY 있을 때만 씀. 카카오 개발자 사이트에 http://localhost:8501 등록해야 지도 뜸 [센터 PC 미검증]
    pts = [{"name": p["name"], "lat": p["lat"], "lng": p["lng"], "day": p.get("day"), "order": p.get("order"),
            "color": p.get("color") or DAY_COLORS[((p.get("day") or 1) - 1) % len(DAY_COLORS)],
            "color_cvd": p.get("color_cvd"),
            "tip": p["name"] + (f" · {p['label']}" if p.get("label") else ""), "photo": p.get("photo"),
            "path": [[a, b] for a, b in p.get("path") or []], "stop": p.get("stop")}
           for p in points if p.get("lat") is not None]
    for i, p in enumerate(pts):   # 번호는 제자리. 거의 같은 자리면 비켜 그림(spread)
        p["num"] = str(p["order"]) if p["order"] is not None else ""
        p["spread"] = sum(1 for q in pts[:i] if abs(q["lat"] - p["lat"]) < 0.00025 and abs(q["lng"] - p["lng"]) < 0.0003)
    shops = [{"lat": x["lat"], "lng": x["lng"], "kind": "lodge" if "🛏" in str(x.get("icon", "")) or x.get("kind") == "lodging" else "meal",
              "tip": x["name"] + " · " + x["label"]} for x in extras or []]
    return f"""<style>
#wrap {{ position: relative }}
#map {{ width: 100%; height: {int(height)}px; border-radius: 18px }}
.pin {{ position: relative; cursor: pointer; outline: none }}
.pin:focus-visible .num, .pin:focus-visible .shop {{ outline: 3px solid #ffbf47; outline-offset: 2px }}
.num {{ min-width: 30px; height: 30px; padding: 0 5px; box-sizing: border-box; border-radius: 15px; border: 4px solid;
        background: #fff; color: #1c2531; font: 800 14px/22px sans-serif; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,.4) }}
.shop {{ width: 26px; height: 26px; display: grid; place-items: center; box-sizing: border-box; border: 3px solid #fff;
         box-shadow: 0 2px 6px rgba(0,0,0,.45) }}
.shop.meal {{ background: #d96a0b; border-radius: 50% }}
.shop.lodge {{ background: #5b4a9e; border-radius: 6px }}
.bus {{ font-size: 15px; line-height: 1; filter: drop-shadow(0 1px 2px rgba(0,0,0,.35)) }}
.tip {{ display: none; position: absolute; bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%); width: max-content;
        max-width: 240px; white-space: pre-line; background: #fff; color: #1c2531; border-radius: 10px; padding: 6px 10px;
        font: 600 13px/1.45 sans-serif; box-shadow: 0 2px 8px rgba(0,0,0,.3) }}
.tip img {{ display: block; width: 96px; height: 72px; object-fit: cover; border-radius: 6px; margin: 0 auto 4px }}
.pin:hover .tip, .pin:focus .tip, .pin.on .tip {{ display: block }}
.legend {{ position: absolute; top: 10px; left: 10px; z-index: 10; max-width: min(46%, 300px); max-height: calc(100% - 20px);
           overflow: auto; box-sizing: border-box; background: rgba(255,255,255,.95); color: #1c2531; border-radius: 12px;
           padding: 8px 12px; font: 500 12px/1.6 sans-serif; box-shadow: 0 2px 8px rgba(0,0,0,.2) }}
.legend.min ol, .legend.min .day, .legend.min .keys {{ display: none }}
.legend .day {{ display: flex; align-items: center; gap: 6px; font-weight: 700; margin-top: 4px }}
.legend .bar {{ width: 18px; height: 4px; border-radius: 2px }}
.legend ol {{ margin: 0; padding: 0; list-style: none }}
.legend li {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis }}
.legend li b {{ display: inline-block; min-width: 18px }}
.legend .keys {{ display: flex; gap: 10px; margin-top: 6px; padding-top: 6px; border-top: 1px solid #e3e8ee; font-weight: 700 }}
.legend .keys span {{ display: inline-flex; align-items: center; gap: 4px }}
.legend .keys i {{ display: inline-grid; place-items: center; width: 18px; height: 18px; box-sizing: border-box; border: 2px solid #fff;
                   box-shadow: 0 0 0 1px #c9d2db }}
.legend .keys i svg {{ width: 10px; height: 10px }}
.legend button {{ border: 0; background: none; color: #3e6a94; font: 600 12px sans-serif; cursor: pointer; padding: 0 }}
</style>
<div id="wrap"><div id="map"></div></div>
<script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey={html.escape(app_key)}&autoload=false"></script>
<script>
const P = {_js(pts)};
const S = {_js(shops)};
const ICON = {{ meal: {_js(FORK_SVG)}, lodge: {_js(BED_SVG)} }};
const CVD = (() => {{ try {{ return parent.document.documentElement.classList.contains('cvd'); }} catch (e) {{ return false; }} }})();   // 화면이 '색 구별 쉽게'면 판정 색 바꿈
const el = (tag, cls, text) => {{ const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }};
if (!window.kakao) {{ document.getElementById('map').innerText = '카카오맵을 불러오지 못했습니다. 카카오 개발자 사이트에서 Web 도메인(http://localhost:8501) 등록과 카카오맵 사용 설정(ON), 인터넷 연결을 확인하세요.'; }}
else kakao.maps.load(() => {{
  const map = new kakao.maps.Map(document.getElementById('map'), {{center: new kakao.maps.LatLng(33.38, 126.55), level: 10}});
  const bounds = new kakao.maps.LatLngBounds();
  const days = {{}};
  P.forEach(p => {{ if (p.day) (days[p.day] = days[p.day] || []).push(p); }});
  const dayKeys = Object.keys(days).sort((a, b) => a - b);
  // 날짜별 경로: 같은 길을 여러 날이 지나도 다 보이게 날짜마다 옆으로 몇 픽셀씩 비켜 나란히 그린다
  const segs = [];   // [{{day, road, coords:[LatLng], color}}]
  dayKeys.forEach(d => {{
    const seq = days[d].sort((a, b) => a.order - b.order);
    for (let i = 1; i < seq.length; i++) {{
      const a = seq[i - 1], b = seq[i];
      const road = b.path && b.path.length > 1;
      const coords = road ? b.path.map(([la, lo]) => new kakao.maps.LatLng(la, lo))
                          : [new kakao.maps.LatLng(a.lat, a.lng), new kakao.maps.LatLng(b.lat, b.lng)];
      segs.push({{ day: d, road, coords, color: b.color }});
    }}
  }});
  const GAP = 6;   // 날짜 사이 간격(픽셀)
  const shift = (coords, px) => {{   // 선을 법선 방향으로 px만큼 옮긴다(화면 좌표에서 계산)
    if (!px) return coords;
    const pr = map.getProjection();
    const pts = coords.map(c => pr.containerPointFromCoords(c));
    return pts.map((p, i) => {{
      const a = pts[Math.max(0, i - 1)], b = pts[Math.min(pts.length - 1, i + 1)];
      let dx = b.x - a.x, dy = b.y - a.y; const L = Math.hypot(dx, dy) || 1; dx /= L; dy /= L;
      let nx = -dy, ny = dx;   // 법선. 가는 방향과 상관없이 같은 쪽을 향하게 맞춘다(같은 길을 반대로 가도 날짜별 자리가 바뀌지 않게)
      if (ny < -1e-6 || (Math.abs(ny) <= 1e-6 && nx < 0)) {{ nx = -nx; ny = -ny; }}
      return pr.coordsFromContainerPoint(new kakao.maps.Point(p.x + nx * px, p.y + ny * px));
    }});
  }};
  let lines = [];
  const drawLines = () => {{
    lines.forEach(l => l.setMap(null)); lines = [];
    const n = dayKeys.length;
    const off = d => (dayKeys.indexOf(d) - (n - 1) / 2) * GAP;
    const placed = segs.map(s => ({{ ...s, path: shift(s.coords, off(s.day)) }}));
    placed.forEach(s => lines.push(new kakao.maps.Polyline({{map, path: s.path, strokeWeight: s.road ? 9 : 7, strokeColor: '#ffffff', strokeOpacity: 0.95}})));
    placed.forEach(s => lines.push(new kakao.maps.Polyline({{map, path: s.path, strokeWeight: s.road ? 5 : 4, strokeColor: s.color,
                                                             strokeOpacity: 1, strokeStyle: s.road ? 'solid' : 'shortdash'}})));
  }};
  const pin = (content, tipText, pos, z, xAnchor) => {{
    const box = el('div', 'pin');
    box.append(content, el('div', 'tip', tipText));
    box.tabIndex = 0; box.setAttribute('role', 'button'); box.setAttribute('aria-label', tipText);
    const ov = new kakao.maps.CustomOverlay({{map, position: pos, content: box, xAnchor: xAnchor ?? 0.5, yAnchor: 0.5, zIndex: z, clickable: true}});
    const lift = on => ov.setZIndex(on ? 20 : z);   // 말풍선이 다른 표시에 가리지 않게
    box.addEventListener('mouseenter', () => lift(true));
    box.addEventListener('mouseleave', () => lift(box.classList.contains('on')));
    box.addEventListener('focus', () => lift(true));
    box.addEventListener('blur', () => {{ box.classList.remove('on'); lift(false); }});
    box.addEventListener('click', () => lift(box.classList.toggle('on')));
    return box;
  }};
  S.forEach(x => {{   // 식당(주황 동그라미 + 포크·나이프), 숙소(보라 네모 + 침대). 번호 점(30px)보다 살짝 작게(26px)
    const pos = new kakao.maps.LatLng(x.lat, x.lng); bounds.extend(pos);
    const mark = el('div', 'shop ' + x.kind); mark.innerHTML = ICON[x.kind];
    pin(mark, x.tip, pos, 3);
  }});
  P.forEach(p => {{   // 버스: 장소마다 가장 가까운 정류소
    if (!p.stop) return;
    pin(el('div', 'bus', '🚏'), '정류소 ' + p.stop.name + ' · ' + p.name + '에서 약 ' + p.stop.m + 'm', new kakao.maps.LatLng(p.stop.lat, p.stop.lng), 2);
  }});
  P.forEach(p => {{   // 번호 점: 장소마다 제자리(거의 같은 자리면 옆으로 비켜서 둘 다 보이게)
    const pos = new kakao.maps.LatLng(p.lat, p.lng); bounds.extend(pos);
    const mark = p.num ? el('div', 'num', p.num) : el('div', 'num', '•');
    mark.style.borderColor = (CVD && p.color_cvd) || p.color;
    const box = pin(mark, (p.num ? p.num + '. ' : '') + p.tip, pos, 5, 0.5 - p.spread * 1.1);
    if (p.num) box.addEventListener('click', () => {{ try {{ parent.postMessage({{ kakaoStop: p.num }}, '*'); }} catch (e) {{}} }});   // 옆 일정의 그 장소 카드로
    if (p.photo) {{ const img = document.createElement('img'); img.src = p.photo; img.alt = p.name + ' 사진'; box.querySelector('.tip').prepend(img); }}
  }});
  let lg = null;
  if (dayKeys.length) {{   // 왼쪽 위 목록: 날짜 색 막대 + 번호와 이름, 식당·숙소 표시 뜻
    lg = el('div', 'legend');
    const btn = el('button', null, '목록 접기');
    btn.addEventListener('click', () => {{ btn.textContent = lg.classList.toggle('min') ? '목록 펼치기' : '목록 접기'; }});
    lg.append(btn);
    dayKeys.forEach(d => {{
      const head = el('div', 'day');
      const bar = el('span', 'bar'); bar.style.background = days[d][0].color;
      head.append(bar, el('span', null, d + '일차'));
      const ol = el('ol');
      days[d].forEach(p => {{ const li = el('li'); li.title = p.name; li.append(el('b', null, p.order), p.name); ol.append(li); }});
      lg.append(head, ol);
    }});
    if (S.length) {{
      const keys = el('div', 'keys');
      [['meal', '식당', '#d96a0b', '50%'], ['lodge', '숙소', '#5b4a9e', '4px']].forEach(([k, name, bg, r]) => {{
        if (!S.some(x => x.kind === k)) return;
        const i = el('i'); i.innerHTML = ICON[k]; i.style.background = bg; i.style.borderRadius = r;
        const sp = el('span'); sp.append(i, name); keys.append(sp);
      }});
      lg.append(keys);
    }}
    document.getElementById('wrap').append(lg);
  }}
  if (P.length) map.setBounds(bounds, 40, 30, 30, lg ? lg.offsetWidth + 30 : 30);   // 목록 아래로 점이 숨지 않게
  drawLines();
  kakao.maps.event.addListener(map, 'zoom_changed', drawLines);   // 픽셀 간격이라 확대·축소하면 다시 계산
}});
</script>"""


def a11y_css(big_text: bool, high_contrast: bool) -> str:
    """접근성 설정 CSS (큰 글씨·고대비). 키보드 초점 표시랑 움직임 줄이기는 항상 켬"""
    css = ["*:focus-visible { outline: 3px solid #ffbf47 !important; outline-offset: 2px !important; }",
           "@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }"]
    if big_text:
        css.append("html { font-size: 118% !important; } [data-testid='stMarkdownContainer'] p, "
                   "[data-testid='stMarkdownContainer'] li { font-size: 1.12rem !important; line-height: 1.8 !important; }")
    if high_contrast:
        css.append("[data-testid='stApp'] { background: #ffffff !important; } "
                   "[data-testid='stApp'] *, [data-testid='stApp'] [data-testid='stCaptionContainer'] p, "
                   "[data-testid='stApp'] [data-testid='stTab'] p, [data-testid='stSidebar'] * { color: #000000 !important; } "
                   "[class*='st-key-glass'], [class*='st-key-card'], [data-testid='stExpander'] details, [data-testid='stMetric'], "
                   "[data-testid='stForm'] "
                   "{ background: #ffffff !important; border: 2px solid #000000 !important; box-shadow: none !important; } "
                   "[data-testid='stApp'] [data-testid^='stBaseButton-primary'] { background: #000000 !important; border: 2px solid #000 !important; } "
                   "[data-testid='stApp'] [data-testid^='stBaseButton-primary'] * { color: #ffff00 !important; } "
                   "[data-testid='stApp'] [data-testid^='stBaseButton-secondary'] { background: #ffffff !important; border: 2px solid #000 !important; } "
                   "[data-testid='stSidebar'] { background: #ffffff !important; border-right: 2px solid #000 !important; }")
    return "<style>" + "\n".join(css) + "</style>"


# ── 결과 화면 조각 (순수 함수라 테스트 가능) ──
FAC_SHORT = {"acc_toilet": "화장실", "acc_parking": "주차", "ramp": "경사로", "elevator": "승강기",
             "wheelchair_rental": "휠체어 대여", "stroller_rental": "유아차 대여", "braille": "점자", "audio_guide": "음성 안내"}
_PILL_CLASS = {"✓": "ok", "✗": "no", "?": "unk"}
_PILL_WORD = {"✓": "있음", "✗": "없음", "?": "확인 필요"}


def pills_html(facilities: list[dict]) -> str:
    """시설 알약: 초록 = 있음, 빨강 = 없음, 회색 = 확인 필요. '꼭 필요'는 테두리
    색만으로 구별하면 안 돼서 기호(✓ ✗ ?)도 같이 쓰고, 화면 낭독기엔 '있음·없음·확인 필요'로 읽힘
    """
    out = []
    for f in facilities:
        m = f.get("mark", "?")
        lab = html.escape(FAC_SHORT.get(f["key"], f.get("label", f["key"])))
        need = " need" if f.get("needed") else ""
        title = f"{lab} {_PILL_WORD.get(m, '')}" + (" (꼭 필요)" if f.get("needed") else "")
        out.append(f"<span class='pill {_PILL_CLASS.get(m, 'unk')}{need}' title='{title}' aria-label='{title}'>{lab} {m}</span>")
    return "".join(out)


def verdict_badge(verdict: str, color: str, mark: str = "") -> str:
    return (f"<span class='vbadge' style='background:{color}'>{html.escape((mark + ' ' if mark else '') + verdict)}</span>")


def split_answer(text: str) -> tuple[str, list[str]]:
    """답을 본문이랑 '주의·한계' 줄로 나눔 (화면에선 작은 상자로). 검증은 원래 답 전체로"""
    body, notes = [], []
    for line in text.splitlines():
        t = line.strip()
        if t.startswith(("- 주의:", "- 한계:")):
            notes.append(t[2:])
        else:
            body.append(line)
    return "\n".join(body).strip(), notes


def merged_numbers(rows: list[dict], near: float = 0.003) -> list[str]:
    """거의 같은 자리(약 300m)의 번호는 한 곳에 '3,4'로 모으고 나머지는 비움 (겹치면 안 읽혀서)
    지도 글자 층 기본 글꼴이 ASCII뿐이라 가운뎃점 대신 쉼표
    """
    out = [str(r["num"]) for r in rows]
    used: set[int] = set()
    for i, a in enumerate(rows):
        if i in used:
            continue
        group = [j for j in range(i + 1, len(rows)) if j not in used
                 and abs(rows[j]["lat"] - a["lat"]) < near and abs(rows[j]["lng"] - a["lng"]) < near]
        if group:
            out[i] = ",".join([out[i]] + [out[j] for j in group])
            for j in group:
                out[j] = ""
            used.update(group)
    return out


# ── 장소 카드: 확인할 점 · 자세히 보기 ──
_KEY_CLASS = {"ok": "ok", "warn": "warn", "unknown": "unk"}
_KEY_WORD = {"ok": "확인됨", "warn": "주의", "unknown": "정보 없음"}


def keycheck_html(kc: dict | None, short: bool = True) -> str:
    """이동 조건별 '확인할 점' 한 줄 (판정이랑 별개). short면 카드용, 아니면 설명까지
    중요한 정보가 자료에 없으면 '📞 방문 전 추가 확인 필요' 배지
    """
    if not kc:
        return ""
    mark = {"ok": "✓", "warn": "⚠", "unknown": "?"}
    chips = []
    for i in kc["items"]:
        lab = html.escape(i["label"] + ("" if short else f" {i['text']}"))
        word = _KEY_WORD[i["state"]]
        chips.append(f"<span class='kchip {_KEY_CLASS[i['state']]}' title='{html.escape(i['label'])} {word}: {html.escape(i['text'])}' "
                     f"aria-label='{html.escape(i['label'])} {word}'>{mark[i['state']]} {lab}</span>")
    call = ("<span class='needcall' title='이동 조건에 중요한 정보가 자료에 없어요. 자세히 보기의 전화로 물어볼 말을 확인하세요.'>"
            "📞 방문 전 추가 확인 필요</span>") if kc["need_call"] else ""
    return f"<div class='keychk'><span class='keyttl'>확인할 점</span>{''.join(chips)}{call}</div>"


def kv_html(rows: list[tuple[str, str]], fold: int = 70) -> str:
    """'자세히 보기' 표: 왼쪽 항목, 오른쪽 내용. fold자보다 길면 '전체 보기'로 펼침"""
    out = []
    for k, v in rows:
        v = str(v)
        body = html.escape(v)
        if len(v) > fold:
            cut = v[:fold].rstrip()
            body = (f"<details><summary><span class='cut'>{html.escape(cut)}…</span> <span class='more'>전체 보기</span></summary>"
                    f"{html.escape(v)}</details>")
        out.append(f"<dt>{html.escape(k)}</dt><dd>{body}</dd>")
    return f"<dl class='kv'>{''.join(out)}</dl>" if out else ""
