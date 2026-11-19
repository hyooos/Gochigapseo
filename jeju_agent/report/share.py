"""가족 공유 일정표: 코스 결과를 그림 위주 PNG 한 장으로
장애·건강 조건이랑 전화 대본은 안 넣음 (파일이 다른 사람한테 감)
"""
from __future__ import annotations

import io
import json
import math
from functools import lru_cache
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Polygon, Rectangle  # noqa: E402

from ..config import ROOT  # noqa: E402
from ..data.visitjeju import is_tag_evidence  # noqa: E402
from ..provenance import footer  # noqa: E402
from ..text import date_label, fee_text  # noqa: E402
from .charts import DAY_COLORS, VERDICT_COLORS, setup_korean_font  # noqa: E402

W = 8.27                       # A4 폭(인치)
M = 0.42                       # 바깥 여백
GAP, PAD = 0.28, 0.2           # 블록 간격, 카드 안쪽 여백
CARD_H, MOVE_H, EXTRA_H = 1.62, 0.36, 0.74
TL_X = M + 0.3                 # 날짜별 세로 동선 선 x
BODY_X = M + 0.66              # 카드 시작 x
# 화면(app.css)이랑 같은 색
INK, INK2, MUTED, LINE = "#23272a", "#464d52", "#646c71", "#d6e0de"
SEA, SEA_DEEP = "#0f7d7e", "#0b4f5c"
MEAL, MEAL_TINT, MEAL_INK = "#d96a0b", "#fdeedd", "#9a4a05"
LODGE, LODGE_TINT = "#5b4a9e", "#ece8f7"
CALL_BG, CALL_INK = "#fdf4d3", "#6d4c00"
SKY = ("#eef7fd", "#dcecf8", "#d2eaf0")
SHADOW = "#0b2f3a"
FAC_SHORT = {"acc_toilet": "화장실", "acc_parking": "전용 주차", "ramp": "경사로", "elevator": "승강기",
             "wheelchair_rental": "휠체어 대여", "stroller_rental": "유아차 대여", "braille": "점자", "audio_guide": "음성 안내"}
PILL = {"✓": ("#e1f2e7", "#1e7a4c"), "✗": ("#fbe2df", "#b3261e"), "?": ("#f1f4f3", "#646c71")}
LOGO = ROOT / "web" / "static" / "img" / "logo.png"
COAST = ROOT / "data" / "geo" / "jeju_coast.json"
HALLA = (33.3617, 126.5292)
TOWNS = [("제주시", 33.497, 126.531), ("서귀포시", 33.246, 126.565), ("애월", 33.462, 126.331), ("한림", 33.413, 126.265),
         ("대정", 33.226, 126.255), ("안덕", 33.255, 126.37), ("남원", 33.28, 126.72), ("표선", 33.327, 126.83),
         ("성산", 33.43, 126.89), ("구좌", 33.52, 126.84), ("조천", 33.535, 126.65)]
K = math.cos(math.radians(33.38))     # 경도 보정 (x = 경도 × K)


# ── 글 내용 (그림이랑 같음). 화면 읽기·테스트용 ──
def _days(result) -> list[int]:
    return sorted({x["day"] for x in result.payload.get("itinerary", [])})


def _subtitle(result, month: int | None, start_date: str | None = None) -> str:
    p = result.payload
    days = _days(result)
    when = (date_label(start_date) + (f" ~ {date_label(start_date, days[-1])}" if days and days[-1] > 1 else "")
            if start_date else f"{month}월" if month else "")
    parts = [f"{len(days)}일", f"출발 {p['start']}" if p.get("start") else "", when]
    return " · ".join(x for x in parts if x)


def _legend_note(cards: dict) -> str:
    dates = sorted({c.get("data_date") for c in cards.values() if c.get("data_date")})
    y0, y1 = (dates[0][:4], dates[-1][:4]) if dates else ("", "")
    when = (f"{y0}년" if y0 == y1 else f"{y0}~{y1}년") if dates else "예전"
    tag = any(is_tag_evidence(f.get("evidence")) for c in cards.values() for f in c.get("facilities", []))
    extra = " 조사 기록이 없는 일부 시설은 비짓제주 관광정보 태그로 채웠습니다." if tag else ""
    return (f"시설은 {when} 무장애 조사 때 기록입니다.{extra} "
            "회색(확인 필요)은 자료에 언급이 없을 뿐이니 방문 전 전화로 확인하세요.")


def _missing_text(slot: str, food_blocked: bool) -> str:
    if food_blocked and slot != "숙소":
        return "장애인 화장실이 있다고 조사된 식당이 근처에 없어 넣지 않았어요."
    return f"코스 장소 근처에 휠체어로 들어갈 수 있다고 적힌 {'숙소' if slot == '숙소' else '식당'}가 자료에 없어요."


def _extra_line(x: dict) -> str:
    """식당·숙소 한 줄 (거리 · 더 가는 시간 · 화장실 · 전화). 출입 원문은 길어서 뺌"""
    toilet = (x.get("facilities") or [{"state": "정보 없음"}])[0].get("state", "정보 없음")
    parts = []
    if x.get("distance_km") is not None:
        parts.append(f"{x['anchor_name']}에서 {'약 ' if x.get('estimated') else ''}{x['distance_km']:g}km")
    if x.get("meal_min"):
        parts.append(f"동선에서 약 {x['meal_min']}분 더")
    parts.append(f"장애인 화장실 {toilet}")
    parts.append(f"전화 {x['phone']}" if x.get("phone") else "전화 정보 없음")
    return " · ".join(parts)


def flow(result) -> list[tuple[int, list[dict]]]:
    """날짜별 순서: 장소 → 식당 → … → 숙소. 화면(views.course)이랑 같은 순서"""
    p = result.payload
    it = [{**x, "num": n} for n, x in enumerate(p.get("itinerary", []), 1)]
    extras = p.get("extras", [])
    miss = p.get("extras_miss", {})
    out = []
    for d in _days(result):
        g = [x for x in it if x["day"] == d]
        rows: list[dict] = []
        for k, x in enumerate(g):
            rows.append({"type": "place", **x})
            rows += [{"type": "extra", **e} for e in extras if e["day"] == d and e["slot"] != "숙소" and e.get("after") == x["order"]]
            if k == len(g) // 2 - (0 if len(g) % 2 else 1) and d in miss.get("점심", []):
                rows.append({"type": "missing", "slot": "점심"})
        rows += [{"type": "extra", **e} for e in extras if e["day"] == d and e["slot"] == "숙소"]
        if d in miss.get("숙소", []) and d != _days(result)[-1]:
            rows.append({"type": "missing", "slot": "숙소"})
        out.append((d, rows))
    return out


def notes(result, month: int | None, transport: str | None) -> list[str]:
    """맨 아래 '가기 전에 확인하세요'"""
    p = result.payload
    cards = {c["place_id"]: c for c in p.get("cards", [])}
    out = [_legend_note(cards)]
    if p.get("extras"):
        out.append("식당·숙소는 조사 뒤 문을 닫거나 바뀌었을 수 있어요. 가기 전에 전화로 영업 여부와 휠체어 출입을 확인하세요.")
    if p.get("rain_ratio") is not None:
        m = f"{month}월" if month else "이 달"
        risky = " 실내 장소를 함께 알아 두세요." if p.get("weather_risky") else ""
        wind = f", 강풍일 {p['wind_ratio']:.0%}" if p.get("wind_ratio") is not None else ""
        out.append(f"{m} 과거 날씨 기록: 비 온 날 {p['rain_ratio']:.0%}{wind}.{risky}")
    road = all(x.get("leg_src") == "길찾기" for x in p.get("itinerary", []))
    out.append(("이동 시간은 카카오 길찾기(자동차) 기준이에요." if road else "이동 시간은 직선거리로 어림한 값이라 실제와 다를 수 있어요.")
               + (f" 이동 수단: {transport}." if transport else ""))
    return out


def share_lines(result, month: int | None = None, start_date: str | None = None, transport: str | None = None) -> list[tuple[str, str]]:
    """일정표에 들어가는 글 [(종류, 글)]"""
    p = result.payload
    cards = {c["place_id"]: c for c in p.get("cards", [])}
    out = [("title", "제주 여행 일정표"), ("sub", _subtitle(result, month, start_date))]
    if transport:
        out.append(("transport", f"{transport}로 다녀요"))
    for d, rows in flow(result):
        out.append(("day", f"{d}일차" + (f" {date_label(start_date, d)}" if start_date else "")))
        for r in rows:
            if r["type"] == "place":
                c = cards.get(r["place_id"], {})
                fee = fee_text(c.get("fee_won"))
                if r.get("leg_min"):
                    out.append(("move", f"약 {r['leg_min']}분 이동"))
                out.append(("place", f"{r['name']} [{r['verdict']}]" + (f" {fee}" if fee else "")))
                out.append(("fac", "  ".join(f"{FAC_SHORT.get(f['key'], f['label'])} {f['mark']}"
                                             for f in c.get("basic_facilities") or c.get("facilities", []))))
            elif r["type"] == "extra":
                out.append(("extra", f"{r['slot']} {r['name']} — {_extra_line(r)}"))
            else:
                out.append(("missing", f"{r['slot']} — {_missing_text(r['slot'], p.get('extras_food_blocked', False))}"))
    out += [("note", n) for n in notes(result, month, transport)]
    out.append(("foot", footer(set(result.data_labels))))
    return out


# ── 그리기 도구 (좌표는 인치, 위에서 아래로) ──
def th(size: float) -> float:
    """글자 크기(pt) → 실제 글자 높이(인치). Pretendard 한글은 대략 0.76배"""
    return size * 0.76 / 72


@lru_cache(maxsize=4096)
def text_w(s: str, size: float, bold: bool = True) -> float:
    """글 폭(인치)을 실제 글꼴로 잼 (어림하면 카드 밖으로 나감)"""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    if not s:
        return 0.0
    prop = FontProperties(family=plt.rcParams["font.family"], weight="bold" if bold else "normal")
    return TextPath((0, 0), s, size=size, prop=prop).get_extents().width / 72


def clip_text(s: str, size: float, width: float) -> str:
    if text_w(s, size) <= width:
        return s
    while s and text_w(s + "…", size) > width:
        s = s[:-1]
    return s.rstrip(" ·,") + "…"


def wrap(s: str, size: float, width: float) -> list[str]:
    """낱말 단위로 width에 맞춰 줄 나눔"""
    lines, cur = [], ""
    for word in s.split(" "):
        cand = f"{cur} {word}".strip()
        if cur and text_w(cand, size, bold=False) > width:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    return lines + [cur] if cur else lines or [""]


def pill_w(label: str) -> float:
    return text_w(f"{label} ✓", 11) + 0.26


def _rgb(c) -> np.ndarray:
    return np.array(to_rgb(c))


def _lighten(c, t: float) -> tuple:
    return tuple(_rgb(c) * (1 - t) + t)


class _Page:
    def __init__(self, h: float):
        self.h = h
        self.fig = plt.figure(figsize=(W, h), dpi=150)
        self.fig.patch.set_facecolor(SKY[1])
        self.ax = self.fig.add_axes((0, 0, 1, 1))
        self.ax.axis("off")
        self._background()
        self.reset()

    def reset(self):
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(self.h, 0)

    def _background(self):
        """화면 바탕이랑 같게: 하늘색 → 바다색 + 옅은 파도 무늬"""
        ny = 256
        t = np.linspace(0, 1, ny)[:, None, None]
        top, mid, bot = (_rgb(c)[None, None] for c in SKY)
        img = np.where(t < 0.45, top * (1 - t / 0.45) + mid * (t / 0.45), mid * (1 - (t - 0.45) / 0.55) + bot * ((t - 0.45) / 0.55))
        # 오른쪽 위 햇빛 번짐
        nx = 96
        yy, xx = np.mgrid[0:ny, 0:nx]
        fy, fx = yy / (ny - 1) * self.h, xx / (nx - 1) * W
        r = np.hypot((fx - W * 0.98) / 4.6, (fy + 0.5) / 3.6)
        glow = np.clip(1 - r, 0, 1) ** 1.6
        sun = _rgb("#fffbe4")[None, None]
        base = np.repeat(img, nx, axis=1)
        img = base * (1 - glow[..., None] * 0.9) + sun * (glow[..., None] * 0.9)
        self.ax.imshow(img, extent=(0, W, self.h, 0), zorder=0, interpolation="bicubic", aspect="auto")
        for x0, x1, x2, x3, a in ((W, W - 0.45, W * 0.38, W * 0.47, 0.22), (W, W, W * 0.52, W * 0.58, 0.14),
                                  (W * 0.88, W * 0.85, W * 0.22, W * 0.26, 0.14)):   # 옅은 빛줄기
            self.ax.add_patch(Polygon([(x0, 0), (x1, 0), (x2, min(self.h, 9.5)), (x3, min(self.h, 9.5))], closed=True,
                                      fc="white", ec="none", alpha=a, zorder=0.1))
        rng = np.random.default_rng(7)   # 시드 고정: 같은 코스면 같은 그림
        step = 1.15
        for row, yy in enumerate(np.arange(0.5, self.h, step * 0.62)):
            for xx in np.arange(0.2 + (row % 2) * step / 2, W, step):
                x0, y0 = xx + rng.uniform(-0.12, 0.12), yy + rng.uniform(-0.08, 0.08)
                s = np.linspace(0, 2 * math.pi, 40)
                self.ax.plot(x0 + s / (2 * math.pi) * 0.34, y0 - 0.025 * np.sin(s), color="#79b2d4", alpha=0.28, lw=1.1, zorder=0.2)

    def box(self, x, y, w, h, color, r=0.12, edge=None, lw=1.0, z=1, ls="solid", alpha=1.0):
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=color, alpha=alpha,
                                         ec=edge or color, lw=lw, zorder=z, ls=ls))

    def shadow(self, x, y, w, h, r, blur=0.14, dy=0.08, inset=0.1, alpha=0.16, z=0.5):
        """번지는 그림자 (둥근 사각형 마스크를 상자 흐림 3번)"""
        dpi = 40
        pad = blur * 3
        x0, y0, x1, y1 = x + inset - pad, y + dy + inset - pad, x + w - inset + pad, y + dy + h - inset + pad
        nx, ny = max(2, int((x1 - x0) * dpi)), max(2, int((y1 - y0) * dpi))
        yy, xx = np.mgrid[0:ny, 0:nx] / dpi
        px, py = xx + x0, yy + y0
        cx = np.clip(px, x + inset + r, x + w - inset - r)
        cy = np.clip(py, y + dy + inset + r, y + dy + h - inset - r)
        mask = (np.hypot(px - cx, py - cy) <= r).astype(float)
        k = max(1, int(blur * dpi))
        for _ in range(3):
            for axis in (0, 1):
                c = np.cumsum(np.pad(mask, [(k, k) if a == axis else (0, 0) for a in (0, 1)], mode="edge"), axis=axis)
                c = np.insert(c, 0, 0, axis=axis)
                mask = (np.take(c, range(2 * k, 2 * k + mask.shape[axis]), axis=axis)
                        - np.take(c, range(0, mask.shape[axis]), axis=axis)) / (2 * k)
        rgba = np.zeros((ny, nx, 4))
        rgba[..., :3] = to_rgb(SHADOW)
        rgba[..., 3] = mask * alpha
        self.ax.imshow(rgba, extent=(x0, x1, y1, y0), zorder=z, interpolation="bilinear", aspect="auto")

    def card(self, x, y, w, h, r=0.18, z=1, color="white", edge=LINE):
        self.shadow(x, y, w, h, r, z=z - 0.5)
        self.box(x, y, w, h, color, r=r, edge=edge, lw=1.0, z=z)

    def text(self, x, y, s, size, color=INK, weight="normal", ha="left", va="center", z=5):
        self.ax.text(x, y, s, fontsize=size, color=color, fontweight=weight, ha=ha, va=va, zorder=z)

    def pill(self, x, y, label, mark) -> float:
        bg, fg = PILL.get(mark, PILL["?"])
        w = pill_w(label)
        self.box(x, y, w, 0.3, bg, r=0.07, z=3)
        self.text(x + w / 2, y + 0.155, f"{label} {mark}", 11, fg, "bold", ha="center", z=4)
        return w

    def chip(self, x, y, label, bg, fg, size=11) -> float:
        w = text_w(label, size) + 0.26
        self.box(x, y, w, 0.3, bg, r=0.07, z=3)
        self.text(x + w / 2, y + 0.155, label, size, fg, "bold", ha="center", z=4)
        return w

    def image(self, path: Path, x, y, w, h, r=0.08):
        """가운데 잘라서 w:h로 넣고 모서리 둥글게"""
        from PIL import Image
        img = Image.open(path).convert("RGB")
        ratio = w / h
        if img.width / img.height > ratio:
            nw = int(img.height * ratio)
            img = img.crop(((img.width - nw) // 2, 0, (img.width - nw) // 2 + nw, img.height))
        else:
            nh = int(img.width / ratio)
            img = img.crop((0, (img.height - nh) // 2, img.width, (img.height - nh) // 2 + nh))
        img = img.resize((int(w * 220), int(h * 220)))
        im = self.ax.imshow(img, extent=(x, x + w, y + h, y), zorder=3, interpolation="lanczos", aspect="auto")
        clip = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc="none", ec="none", transform=self.ax.transData)
        self.ax.add_patch(clip)
        im.set_clip_path(clip)

    def logo(self, x, y, h) -> float:
        """로고를 높이 h로 넣고 폭 돌려줌. 파일 없으면 0"""
        if not LOGO.exists():
            return 0.0
        from PIL import Image
        img = Image.open(LOGO).convert("RGBA")
        w = h * img.width / img.height
        self.ax.imshow(np.asarray(img), extent=(x, x + w, y + h, y), zorder=4, interpolation="lanczos", aspect="auto")
        return w


# ── 제주 지도 ──
@lru_cache(maxsize=1)
def _coast() -> tuple[np.ndarray, np.ndarray]:
    """해안선(본섬·우도)을 Catmull-Rom으로 부드럽게 (x = 경도×K, y = 위도)"""
    data = json.loads(COAST.read_text(encoding="utf-8"))

    def smooth(ring) -> np.ndarray:
        p = np.array([(lng * K, lat) for lng, lat in ring[:-1]])
        n = len(p)
        out = []
        for i in range(n):
            p0, p1, p2, p3 = p[(i - 1) % n], p[i], p[(i + 1) % n], p[(i + 2) % n]
            for t in np.linspace(0, 1, 8, endpoint=False):
                t2, t3 = t * t, t * t * t
                out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2 + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
        out.append(out[0])
        return np.array(out)

    return smooth(data["main"]), smooth(data["udo"])


FULL = (126.12 * K, 127.0 * K, 33.16, 33.6)    # 섬 전체 범위 (x0, x1, y0, y1)


def _view(points: list[tuple[float, float]], aspect: float) -> tuple[float, float, float, float]:
    """코스 쪽 확대 범위. 섬 전체보다 안 넓히고 확대는 4배까지"""
    fx0, fx1, fy0, fy1 = FULL
    fw, fh = fx1 - fx0, fy1 - fy0
    if fw / fh < aspect:                 # 지도 칸 비율에 맞춘 섬 전체 범위
        fw = fh * aspect
    else:
        fh = fw / aspect
    cx_full, cy_full = (fx0 + fx1) / 2, (fy0 + fy1) / 2
    if not points:
        return cx_full - fw / 2, cx_full + fw / 2, cy_full - fh / 2, cy_full + fh / 2
    xs = [lng * K for _, lng in points]
    ys = [lat for lat, _ in points]
    w = max(max(xs) - min(xs), (max(ys) - min(ys)) * aspect) * 1.5 + 0.05
    w = min(fw, max(w, fw / 4.0))
    h = w / aspect
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    x0 = min(max(cx - w / 2, cx_full - fw / 2), cx_full + fw / 2 - w)
    y0 = min(max(cy - h / 2, cy_full - fh / 2), cy_full + fh / 2 - h)
    return x0, x0 + w, y0, y0 + h


def draw_island(ax, view: tuple[float, float, float, float], zoom: float, labels: bool = True) -> None:
    """바다 띠 → 땅 → 해안선 → 한라산·오름 → 지명"""
    main, udo = _coast()
    x0, x1, y0, y1 = view
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.add_patch(Rectangle((x0 - 1, y0 - 1), x1 - x0 + 2, y1 - y0 + 2, fc="#a7e0fe", ec="none", zorder=0))
    for lw, col in ((26, "#bfe9fd"), (16, "#cdeefe"), (8, "#e2f6fe")):   # 해안에서 퍼지는 물결 띠 (굵기 고정)
        for ring in (main, udo):
            ax.plot(ring[:, 0], ring[:, 1], color=col, lw=lw, solid_joinstyle="round", zorder=1)
    for ring in (main, udo):
        ax.add_patch(Polygon(ring, closed=True, fc="#e7eddf", ec="#2f3a3d", lw=2.0, zorder=2, joinstyle="round"))
    for lat, lng, r in ((33.168, 126.271, 0.006), (33.118, 126.267, 0.0045)):   # 가파도·마라도
        ax.add_patch(Circle((lng * K, lat), r, fc="#e7eddf", ec="#2f3a3d", lw=1.4, zorder=2))
    hx, hy = HALLA[1] * K, HALLA[0]
    # 한라산 + 꼭대기 눈
    t = np.linspace(-1, 1, 60)
    hw, hh = 0.13, 0.072
    ridge = np.column_stack([hx + t * hw, hy - 0.035 + hh * (1 - np.abs(t) ** 1.6) ** 1.8])
    ax.add_patch(Polygon(ridge, closed=True, fc="#a3bb97", ec="none", alpha=0.9, zorder=2.2))
    top = hy - 0.035 + hh
    snow = [(hx - 0.032, top - 0.012), (hx - 0.016, top - 0.0015), (hx + 0.016, top - 0.0015), (hx + 0.032, top - 0.012),
            (hx + 0.014, top - 0.017), (hx + 0.004, top - 0.011), (hx - 0.008, top - 0.018)]
    ax.add_patch(Polygon(snow, closed=True, fc="white", ec="none", zorder=2.3))
    if labels:
        ax.text(hx, hy - 0.012, "한라산", fontsize=8 if zoom < 1.5 else 9, color="#4e6a4c", ha="center", va="top", fontweight="bold", zorder=2.4)
        for name, lat, lng in TOWNS:
            if x0 < lng * K < x1 and y0 < lat < y1:
                ax.text(lng * K, lat, name, fontsize=8.5, color="#7b8a85", ha="center", va="center", fontweight="bold", zorder=2.4)


def _draw_map(page: _Page, it: list[dict], y: float, shops: list[dict], h: float) -> None:
    """지도 카드: 섬 + 날짜별 동선·번호 + 식당·숙소 + (확대했으면) 귀퉁이에 섬 전체 + 범례"""
    x, w = M, W - 2 * M
    page.card(x, y, w, h, r=0.24)
    mx, my, mw, mh = x + 0.14, y + 0.14, w - 0.28, h - 0.62
    shops = [s for s in shops if s.get("lat") is not None and s.get("lng") is not None]
    view = _view([(p["lat"], p["lng"]) for p in it] + [(float(s["lat"]), float(s["lng"])) for s in shops], mw / mh)
    zoom = (FULL[1] - FULL[0]) / (view[1] - view[0])
    ax = page.fig.add_axes((mx / W, 1 - (my + mh) / page.h, mw / W, mh / page.h))
    draw_island(ax, view, zoom)
    by_day: dict[int, list[dict]] = {}
    for p in it:
        by_day.setdefault(p["day"], []).append(p)
    for d, g in by_day.items():
        col = DAY_COLORS[(d - 1) % len(DAY_COLORS)]
        for a, b in zip(g, g[1:]):
            if b.get("path"):
                xs, ys = [q[1] * K for q in b["path"]], [q[0] for q in b["path"]]
                ax.plot(xs, ys, color="white", lw=6, zorder=3, solid_capstyle="round")
                ax.plot(xs, ys, color=col, lw=3, zorder=3.1, solid_capstyle="round")
            else:
                ax.plot([a["lng"] * K, b["lng"] * K], [a["lat"], b["lat"]], color="white", lw=6, zorder=3, solid_capstyle="round")
                ax.plot([a["lng"] * K, b["lng"] * K], [a["lat"], b["lat"]], color=col, lw=2.6, zorder=3.1, ls=(0, (1.2, 2.2)),
                        dash_capstyle="round")
    for s in shops:
        lodging = s.get("kind") == "lodging"
        ax.scatter([float(s["lng"]) * K], [float(s["lat"])], s=95, marker="s" if lodging else "o", color=LODGE if lodging else MEAL,
                   edgecolor="white", linewidth=1.8, zorder=3.5)
    near = (view[1] - view[0]) * 0.03
    drawn: list[tuple[float, float]] = []
    for n, p in enumerate(it, 1):
        col = DAY_COLORS[(p["day"] - 1) % len(DAY_COLORS)]
        px, py = p["lng"] * K, p["lat"]
        while any(math.hypot(px - a, py - b) < near for a, b in drawn):   # 거의 같은 자리면 옆으로 비켜 그림
            px += near
        drawn.append((px, py))
        ax.scatter([px], [py], s=300, color="white", edgecolor=col, linewidth=3, zorder=4)
        ax.text(px, py, str(n), fontsize=10, color=INK, fontweight="bold", ha="center", va="center", zorder=5)
    clip = FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.035", transform=ax.transAxes, fc="none", ec="none")
    ax.add_patch(clip)
    for art in ax.get_children():
        try:
            art.set_clip_path(clip)
        except (AttributeError, TypeError):
            pass
    if zoom > 1.25:   # 확대했으면 오른쪽 위에 섬 전체
        iw = 1.5
        ih = iw * (FULL[3] - FULL[2]) / (FULL[1] - FULL[0])
        ix, iy = mx + mw - iw - 0.1, my + 0.1
        inset = page.fig.add_axes((ix / W, 1 - (iy + ih) / page.h, iw / W, ih / page.h), zorder=7)
        main, udo = _coast()
        inset.set_xlim(FULL[0] - 0.03, FULL[1] + 0.03)
        inset.set_ylim(FULL[2] - 0.03, FULL[3] + 0.03)
        inset.set_aspect("equal")
        inset.axis("off")
        inset.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.08", transform=inset.transAxes,
                                       fc=(1, 1, 1, 0.92), ec=LINE, lw=1.0, zorder=0))
        for ring in (main, udo):
            inset.add_patch(Polygon(ring, closed=True, fc="#e7eddf", ec="#2f3a3d", lw=0.8))
        inset.add_patch(Rectangle((view[0], view[2]), view[1] - view[0], view[3] - view[2], fill=False, ec="#b3261e", lw=1.4))
    # 범례 (지도 아래 한 줄)
    ly = y + h - 0.26
    lx = x + 0.3
    for d in sorted(by_day):
        col = DAY_COLORS[(d - 1) % len(DAY_COLORS)]
        page.box(lx, ly - 0.03, 0.3, 0.06, col, r=0.03, z=6)
        page.text(lx + 0.38, ly, f"{d}일차", 11, INK2, "bold", z=6)
        lx += 1.0
    if any(s.get("kind") != "lodging" for s in shops):
        page.ax.add_patch(Circle((lx + 0.08, ly), 0.07, fc=MEAL, ec="white", lw=1.2, zorder=6))
        page.text(lx + 0.22, ly, "식당", 11, INK2, "bold", z=6)
        lx += 0.8
    if any(s.get("kind") == "lodging" for s in shops):
        page.box(lx + 0.01, ly - 0.07, 0.14, 0.14, LODGE, r=0.02, z=6)
        page.text(lx + 0.22, ly, "숙소", 11, INK2, "bold", z=6)
        lx += 0.8
    road = any(p.get("path") for p in it)
    page.text(x + w - 0.3, ly, "실선 실제 도로 · 점선 직선 연결" if road else "점선은 장소를 직선으로 이은 것", 10, MUTED, ha="right", z=6)


def _name_size(name: str) -> float:
    return 17 if len(name) <= 11 else 15 if len(name) <= 15 else 13


def _draw_place(page: _Page, r: dict, card: dict, col: str, y: float, photo: Path | None) -> None:
    """장소 카드: 번호 원, 사진·이름·판정·시설·입장료·방문 전 확인·장애물"""
    page.ax.add_patch(Circle((TL_X, y + 0.36), 0.2, fc="white", ec=col, lw=3.2, zorder=4))
    page.text(TL_X, y + 0.36, str(r["num"]), 13, INK, "bold", ha="center", z=5)
    cx, cw = BODY_X, W - M - BODY_X
    page.card(cx, y, cw, CARD_H, r=0.16)
    ph_w, ph_h = 1.36, CARD_H - 2 * PAD
    ix = cx + PAD
    if photo is not None and Path(photo).exists():
        page.image(Path(photo), ix, y + PAD, ph_w, ph_h)
    else:
        page.box(ix, y + PAD, ph_w, ph_h, "#e3ebe9", r=0.08, z=2)
        page.text(ix + ph_w / 2, y + CARD_H / 2, "사진 없음", 10, MUTED, ha="center")
    tx = ix + ph_w + PAD
    right = cx + cw - PAD
    v = r["verdict"]
    vc = VERDICT_COLORS.get(v, MUTED)
    vw = text_w(f"○ {v}", 11.5) + 0.3
    page.box(right - vw, y + PAD, vw, 0.32, _lighten(vc, 0.84), r=0.16, z=3)
    page.text(right - vw / 2, y + PAD + 0.16, f"{'○' if v == '적합' else '△' if v == '주의' else '×'} {v}", 11.5, vc, "bold", ha="center")
    size = _name_size(r["name"])
    page.text(tx, y + PAD + 0.17, clip_text(r["name"], size, right - vw - 0.15 - tx), size, INK, "bold")
    meta = " · ".join(v for v in (card.get("emd"), card.get("category"), fee_text(card.get("fee_won"))) if v and v != "기타")
    if meta:
        page.text(tx, y + PAD + 0.48, meta, 10.5, MUTED)
    px = tx
    py = y + PAD + 0.64
    for f in card.get("basic_facilities") or card.get("facilities", []):   # 조건 상관없이 같은 기본 시설 4개
        lab = FAC_SHORT.get(f["key"], f["label"])
        if px + pill_w(lab) > right:
            break
        px += page.pill(px, py, lab, f["mark"]) + 0.07
    call = bool((card.get("key_checks") or {}).get("need_call"))
    if call:   # 시설 알약 아래 줄 왼쪽
        page.chip(tx, py + 0.38, "방문 전 전화 확인", CALL_BG, CALL_INK)
    bars = [str(b) for b in card.get("barriers", [])][:2]
    if bars and not call:
        page.text(tx, y + CARD_H - PAD - 0.08, clip_text("기록된 장애물: " + ", ".join(bars), 10.5, right - tx), 10.5, "#965800")


def _draw_extra(page: _Page, r: dict, y: float) -> None:
    lodging = r.get("kind") == "lodging"
    tint, ink, mark = (LODGE_TINT, LODGE, LODGE) if lodging else (MEAL_TINT, MEAL_INK, MEAL)
    if lodging:
        page.box(TL_X - 0.12, y + EXTRA_H / 2 - 0.12, 0.24, 0.24, mark, r=0.04, edge="white", lw=2, z=4)
    else:
        page.ax.add_patch(Circle((TL_X, y + EXTRA_H / 2), 0.12, fc=mark, ec="white", lw=2, zorder=4))
    cx, cw = BODY_X, W - M - BODY_X
    page.box(cx, y + 0.04, cw, EXTRA_H - 0.08, tint, r=0.14, z=1)
    slot = "하루 끝 숙소" if lodging else r["slot"]
    page.text(cx + PAD, y + 0.25, slot, 11.5, ink, "bold")
    nx = cx + PAD + text_w(slot, 11.5) + 0.14
    tail = "" if r.get("verdict") in (None, "적합") else f"  ({r['verdict']})"
    page.text(nx, y + 0.25, clip_text(r["name"] + tail, 14, cx + cw - PAD - nx), 14, INK, "bold")
    page.text(cx + PAD, y + 0.52, clip_text(_extra_line(r), 10.5, cw - 2 * PAD), 10.5, INK2)


def _draw_missing(page: _Page, r: dict, y: float, food_blocked: bool) -> None:
    page.ax.add_patch(Circle((TL_X, y + EXTRA_H / 2), 0.1, fc="#c9b59b", ec="white", lw=2, zorder=4))
    cx, cw = BODY_X, W - M - BODY_X
    page.box(cx, y + 0.06, cw, EXTRA_H - 0.12, (1, 1, 1, 0.55), r=0.14, edge="#c9a77f", lw=1.3, ls=(0, (4, 3)), z=1)
    page.text(cx + PAD, y + 0.27, r["slot"], 11.5, MEAL_INK, "bold")
    page.text(cx + PAD, y + 0.5, clip_text(_missing_text(r["slot"], food_blocked), 10.5, cw - 2 * PAD), 10.5, INK2)


HEAD_H = 1.62
NOTE_PT, NOTE_LH = 11.5, 0.27
FOOT_PT, FOOT_LH = 8.5, 0.2


def itinerary_png(result, month: int | None = None, photos: dict[str, Path] | None = None, start_date: str | None = None,
                  transport: str | None = None) -> bytes:
    """코스 결과 → 공유 일정표 PNG(바이트)
    photos: {place_id: 대표 사진}, transport: 이동 수단 이름 (장애·건강 조건은 안 받음)
    """
    setup_korean_font()
    p = result.payload
    it = [{**x, "num": n} for n, x in enumerate(p.get("itinerary", []), 1)]
    cards = {c["place_id"]: c for c in p.get("cards", [])}
    days = flow(result)
    usable = W - 2 * M
    note_lines = [wrap(n, NOTE_PT, usable - 0.6) for n in notes(result, month, transport)]
    foot = wrap(footer(set(result.data_labels)), FOOT_PT, usable)
    map_h = 4.2
    body = 0.0
    for d, rows in days:
        body += GAP + 0.5 + 0.12
        for r in rows:
            if r["type"] == "place":
                body += (MOVE_H if r.get("leg_min") is not None else 0) + CARD_H + 0.08
            else:
                body += EXTRA_H
    note_h = 0.5 + sum(len(x) for x in note_lines) * NOTE_LH + 0.12 * len(note_lines) + 0.25
    h = M + HEAD_H + GAP + map_h + body + GAP + note_h + GAP + 0.3 + 0.2 + len(foot) * FOOT_LH + M
    page = _Page(h)

    # 머리: 로고 + 제목·날짜·이동 수단
    page.card(M, M, usable, HEAD_H, r=0.26)
    lw = page.logo(M + 0.24, M + 0.16, HEAD_H - 0.32)
    tx = M + 0.24 + (lw + 0.3 if lw else 0)
    lines = [("제주 여행 일정표", 26, INK, "bold"), (_subtitle(result, month, start_date), 14, INK2, "normal")]
    if transport:
        lines.append((f"{transport}로 다녀요", 12.5, SEA_DEEP, "bold"))
    hs = [th(s) for _, s, _, _ in lines]
    gap = 0.16
    yy = M + (HEAD_H - (sum(hs) + gap * (len(hs) - 1))) / 2
    for (s, size, col, wt), hh in zip(lines, hs):
        page.text(tx, yy + hh / 2, s, size, col, wt)
        yy += hh + gap
    y = M + HEAD_H + GAP
    _draw_map(page, it, y, p.get("extras", []), map_h)
    y += map_h

    for d, rows in days:
        col = DAY_COLORS[(d - 1) % len(DAY_COLORS)]
        places = [r for r in rows if r["type"] == "place"]
        y += GAP
        page.text(M, y + 0.25, f"{d}일차", 22, col, "bold")
        when = date_label(start_date, d) if start_date else ""
        page.text(M + 1.05, y + 0.27, when, 14, INK2, "bold")
        page.text(W - M, y + 0.27, f"{len(places)}곳 · 이동 합계 약 {sum(r.get('leg_min') or 0 for r in places)}분", 11.5, MUTED, ha="right")
        page.box(M, y + 0.5, usable, 0.045, col, r=0.02, z=1)
        y += 0.5 + 0.12
        top = y
        for r in rows:
            if r["type"] == "place":
                if r.get("leg_min") is not None:
                    m = r.get("leg_min") or 0
                    first = r is places[0]
                    how = "길찾기" if r.get("leg_src") == "길찾기" else "추정"
                    page.text(BODY_X, y + MOVE_H / 2, ("출발해서 " if first else "") + (f"약 {m}분 이동 ({how})" if m else "바로 근처"), 10.5, MUTED)
                    y += MOVE_H
                _draw_place(page, r, cards.get(r["place_id"], {}), col, y, (photos or {}).get(r["place_id"]))
                y += CARD_H + 0.08
            elif r["type"] == "extra":
                _draw_extra(page, r, y)
                y += EXTRA_H
            else:
                _draw_missing(page, r, y, p.get("extras_food_blocked", False))
                y += EXTRA_H
        page.ax.plot([TL_X, TL_X], [top + 0.1, y - 0.3], color=col, lw=3, alpha=0.35, zorder=0.8, solid_capstyle="round")

    # 아래: 가기 전에 확인 · 알약 뜻 · 출처
    y += GAP
    page.card(M, y, usable, note_h, r=0.2)
    page.text(M + 0.3, y + 0.3, "가기 전에 확인하세요", 14, INK, "bold")
    yy = y + 0.56
    for block in note_lines:
        page.ax.add_patch(Circle((M + 0.36, yy + NOTE_LH / 2), 0.035, color=SEA, zorder=5))
        for line in block:
            page.text(M + 0.5, yy + NOTE_LH / 2, line, NOTE_PT, INK)
            yy += NOTE_LH
        yy += 0.12
    y += note_h + GAP
    px = M
    for label, mark in (("조사 때 있음", "✓"), ("조사 때 없음", "✗"), ("확인 필요", "?")):
        px += page.pill(px, y, label, mark) + 0.12
    y += 0.3 + 0.2
    for line in foot:
        page.text(M, y + FOOT_LH / 2, line, FOOT_PT, MUTED)
        y += FOOT_LH

    page.reset()
    buf = io.BytesIO()
    page.fig.savefig(buf, format="png", dpi=150, facecolor=SKY[1])
    plt.close(page.fig)
    return buf.getvalue()
