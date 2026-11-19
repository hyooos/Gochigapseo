"""일러스트 제주 지도(SVG). 인터넷 없이 그림 (카카오맵 없어도 코스 보이게)
해안선은 Natural Earth(퍼블릭 도메인), 측량 지도 아님
"""
from __future__ import annotations

import html
import json
import math
from functools import lru_cache
from pathlib import Path
from jeju_agent.ui_helpers import BED_D, FORK_D

COAST = Path(__file__).resolve().parent.parent / "data" / "geo" / "jeju_coast.json"

W, H = 1000, 560                     # SVG 좌표계 (화면에선 폭에 맞춰 늘고 줄어듦)
LNG0, LNG1, LAT0, LAT1 = 126.08, 127.02, 33.08, 33.62
KX = math.cos(math.radians(33.38))
PAD = 28

DAY_COLORS = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9"]   # charts.DAY_COLORS랑 같음 (일정표랑 맞춤)

HALLA = (33.3617, 126.5292)
TOWNS = [("제주시", 33.497, 126.531), ("서귀포시", 33.246, 126.565), ("애월", 33.462, 126.331), ("한림", 33.413, 126.265),
         ("대정", 33.226, 126.255), ("안덕", 33.255, 126.37), ("남원", 33.28, 126.72), ("표선", 33.327, 126.83),
         ("성산", 33.43, 126.89), ("구좌", 33.52, 126.84), ("조천", 33.535, 126.65)]
ISLETS = [("가파도", 33.168, 126.271, 7), ("마라도", 33.118, 126.267, 5)]


def _scale() -> tuple[float, float, float]:
    sx = (W - 2 * PAD) / ((LNG1 - LNG0) * KX)
    sy = (H - 2 * PAD) / (LAT1 - LAT0)
    s = min(sx, sy)
    ox = (W - (LNG1 - LNG0) * KX * s) / 2
    oy = (H - (LAT1 - LAT0) * s) / 2
    return s, ox, oy


def xy(lat: float, lng: float) -> tuple[float, float]:
    """위경도 → SVG 좌표"""
    s, ox, oy = _scale()
    return round(ox + (lng - LNG0) * KX * s, 1), round(oy + (LAT1 - lat) * s, 1)


def _smooth(points: list[tuple[float, float]], closed: bool = True) -> str:
    """Catmull-Rom → 3차 베지에 SVG 경로"""
    p = points[:-1] if closed and points[0] == points[-1] else points
    n = len(p)
    d = [f"M{p[0][0]:.1f},{p[0][1]:.1f}"]
    for i in range(n if closed else n - 1):
        p0, p1, p2, p3 = p[(i - 1) % n], p[i], p[(i + 1) % n], p[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return "".join(d) + ("Z" if closed else "")


@lru_cache(maxsize=1)
def coast_paths() -> tuple[str, str]:
    data = json.loads(COAST.read_text(encoding="utf-8"))
    main = _smooth([xy(lat, lng) for lng, lat in data["main"]])
    udo = _smooth([xy(lat, lng) for lng, lat in data["udo"]])
    return main, udo


def _mountain(cx: float, cy: float) -> str:
    """한라산: 낮고 넓은 방패 화산 + 눈 덮인 꼭대기"""
    return (f"<g class='halla' aria-hidden='true'>"
            f"<path d='M{cx - 118},{cy + 26} Q{cx - 50},{cy - 30} {cx - 14},{cy - 34} L{cx + 14},{cy - 34} "
            f"Q{cx + 50},{cy - 30} {cx + 118},{cy + 26} Z' fill='url(#gHalla)'/>"
            f"<path d='M{cx - 30},{cy - 26} Q{cx - 16},{cy - 36} {cx - 12},{cy - 34} L{cx + 12},{cy - 34} "
            f"Q{cx + 16},{cy - 36} {cx + 30},{cy - 26} Q{cx + 14},{cy - 22} {cx + 6},{cy - 27} "
            f"Q{cx - 4},{cy - 21} {cx - 14},{cy - 27} Q{cx - 20},{cy - 22} {cx - 30},{cy - 26} Z' fill='#fbfdfc'/>"
            f"<text x='{cx}' y='{cy + 6}' class='peak'>한라산</text></g>")


def focus(points: list[tuple[float, float]], max_zoom: float = 9.0) -> tuple[str, float]:
    """점들이 한쪽에 몰려 있으면 그쪽으로 확대한 viewBox랑 배율. W:H 비율은 유지"""
    if not points:
        return f"0 0 {W} {H}", 1.0
    xs = [xy(a, b)[0] for a, b in points]
    ys = [xy(a, b)[1] for a, b in points]
    pad = 46   # 번호 점 지름쯤 (번호 점은 배율 상관없이 같은 크기라 여백도 화면 기준)
    zoom = 1.0
    for _ in range(4):   # 여백이 배율 따라 달라져서 몇 번 다시 잼
        bw, bh = max(xs) - min(xs) + 2 * pad / zoom, max(ys) - min(ys) + 2 * pad / zoom
        zoom = max(1.0, min(max_zoom, W / bw, H / bh))
    vw, vh = W / zoom, H / zoom
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    x0 = min(max(cx - vw / 2, 0), W - vw)
    y0 = min(max(cy - vh / 2, 0), H - vh)
    return f"{x0:.0f} {y0:.0f} {vw:.0f} {vh:.0f}", zoom


def base_svg(inner: str = "", title: str = "제주 지도", desc: str = "", compact: bool = False, viewbox: str | None = None,
             zoom: float = 1.0) -> str:
    """섬 그림 + inner(경로·점). 경로가 잘 보이게 오름은 빼고 한라산·지명만 옅게
    화면에서 확대·이동하면 app.js가 viewBox랑 --z를 바꿈 (점·선·글씨는 --z로 나눠서 크기 유지)
    """
    main, udo = coast_paths()
    hx, hy = xy(*HALLA)
    rings = "".join(f"<path d='{main}' class='ring r{i}'/><path d='{udo}' class='ring r{i}'/>" for i in range(4))
    islets = "".join(f"<circle cx='{xy(a, b)[0]}' cy='{xy(a, b)[1]}' r='{r}' class='islet'><title>{n}</title></circle>"
                     for n, a, b, r in ISLETS)
    towns = "" if compact else "".join(f"<text x='{xy(a, b)[0]}' y='{xy(a, b)[1]}' class='town'>{n}</text>" for n, a, b in TOWNS)
    return f"""<svg class='jeju' style='--z:{zoom:.3f}' viewBox='{viewbox or f"0 0 {W} {H}"}' role='img' aria-labelledby='mapT mapD' xmlns='http://www.w3.org/2000/svg'>
<title id='mapT'>{html.escape(title)}</title><desc id='mapD'>{html.escape(desc)}</desc>
<defs>
  <radialGradient id='gSea' cx='50%' cy='46%' r='70%'><stop offset='0' stop-color='#b4e5fd'/><stop offset='1' stop-color='#a3dcfc'/></radialGradient>
  <linearGradient id='gLand' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='#eef2e6'/><stop offset='1' stop-color='#dfe7d6'/></linearGradient>
  <linearGradient id='gHalla' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='#7d9a78'/><stop offset='1' stop-color='#b9cdaf' stop-opacity='.15'/></linearGradient>
</defs>
<rect x='-{W}' y='-{H}' width='{3 * W}' height='{3 * H}' fill='url(#gSea)'/>
{rings}{islets}
<path d='{main}' class='land'/><path d='{udo}' class='land'/>
{_mountain(hx, hy)}{towns}
{inner}
</svg>"""


def route_layer(stops: list[dict], extras: list[dict] | None = None) -> str:
    """stops: day, order, num, lat, lng, name, (path). extras: kind, lat, lng, name, day
    경로선은 날짜 색, 도로 좌표 없으면 점선 직선. 번호 점은 마지막에 그려서 위로
    """
    lines, dots, shops = [], [], []
    by_day: dict[int, list[dict]] = {}
    for p in stops:
        by_day.setdefault(int(p["day"]), []).append(p)
    for d, g in by_day.items():
        g.sort(key=lambda p: int(p["order"]))
        col = DAY_COLORS[(d - 1) % len(DAY_COLORS)]
        for a, b in zip(g, g[1:]):
            if b.get("path"):
                pts = " ".join(f"{x},{y}" for x, y in (xy(la, lo) for la, lo in b["path"]))
                dash = ""
            else:
                (x1, y1), (x2, y2) = xy(a["lat"], a["lng"]), xy(b["lat"], b["lng"])
                pts, dash = f"{x1},{y1} {x2},{y2}", " stroke-dasharray='2 9'"
            lines.append(f"<polyline points='{pts}' class='halo'/><polyline points='{pts}' class='leg' stroke='{col}'{dash}/>")
    for p in stops:
        x, y = xy(p["lat"], p["lng"])
        col = DAY_COLORS[(int(p["day"]) - 1) % len(DAY_COLORS)]
        dots.append(f"<g class='stop' data-num='{p['num']}' transform='translate({x} {y})' tabindex='0' role='button' "
                    f"aria-label='{p['num']}번 {html.escape(p['name'])}'><g class='pop'><g class='mk'>"
                    f"<circle r='17' fill='#fff' stroke='{col}' stroke-width='4.5'/><text class='num'>{p['num']}</text></g></g>"
                    f"<title>{p['num']}. {html.escape(p['name'])}</title></g>")
    for p in stops:   # 버스: 장소마다 제일 가까운 정류소 (센터 3-03, 반출 가능 표)
        st = p.get("stop")
        if not st or st.get("lat") is None:
            continue
        x, y = xy(st["lat"], st["lng"])
        shops.append(f"<g class='bus' transform='translate({x} {y})'><g class='pop'><g class='mk'><rect x='-6' y='-6' width='12' height='12' rx='2'/>"
                     f"</g></g><title>정류소 {html.escape(str(st.get('name', '')))} · {html.escape(p['name'])}에서 약 {st.get('m', '?')}m(직선)</title></g>")
    for e in extras or []:
        if e.get("lat") is None:
            continue
        x, y = xy(e["lat"], e["lng"])
        cls = "lodge" if e["kind"] == "lodging" else "meal"
        shape = (f"<circle r='11'/><g transform='translate(-7 -7) scale(.5833)'><path class='ic' d='{FORK_D}'/></g>" if cls == "meal" else
                 f"<rect x='-11' y='-11' width='22' height='22' rx='5'/><g transform='translate(-7 -7) scale(.5833)'><path class='ic' d='{BED_D}'/></g>")
        shops.append(f"<g class='shop {cls}' transform='translate({x} {y})'><g class='pop'><g class='mk'>{shape}</g></g>"
                     f"<title>{html.escape(e['name'])}</title></g>")
    return "<g class='routes'>" + "".join(lines) + "</g><g class='shops'>" + "".join(shops) + "</g><g class='stops'>" + "".join(dots) + "</g>"


VERDICT_COLORS = {"적합": "#1e7a4c", "주의": "#c27a00", "부적합": "#b3261e"}


def points_layer(points: list[dict]) -> str:
    """장소 찾기 지도: 추천 순번 + 판정 색 테두리"""
    out = []
    for p in points:
        x, y = xy(p["lat"], p["lng"])
        cls = {"적합": "v-ok", "주의": "v-warn", "부적합": "v-bad"}.get(p["verdict"], "v-ok")
        out.append(f"<g class='stop {cls}' data-num='{p['num']}' transform='translate({x} {y})' tabindex='0' role='button' "
                   f"aria-label='{p['num']}번 {html.escape(p['name'])} {p['verdict']}'><g class='pop'><g class='mk'>"
                   f"<circle r='17' fill='#fff' stroke-width='4.5' data-v='{p['verdict']}'/><text class='num'>{p['num']}</text></g></g>"
                   f"<title>{p['num']}. {html.escape(p['name'])} ({p['verdict']})</title></g>")
    return "<g class='stops'>" + "".join(out) + "</g>"
