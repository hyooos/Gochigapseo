"""보고서용 PNG 차트 (반출 기준이라 PNG만). 출처 문구·[가상] 표시를 그림 안에 넣음
색만으로 구별 안 하게 기호(○ △ ×)랑 글자도 같이 씀
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 화면 없는 환경(CI·서버)에서도 돌게
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager, ticker

from ..config import ROOT
from ..provenance import footer

_KOREAN_FONTS = ["Pretendard", "Malgun Gothic", "AppleGothic", "Apple SD Gothic Neo", "NanumGothic", "Noto Sans CJK KR",
                 "Noto Sans KR", "Noto Sans CJK JP"]
_FONT_DIR = ROOT / "ui" / "fonts"

VERDICT_COLORS = {"적합": "#2e9d5b", "주의": "#e39a2d", "부적합": "#d0473b"}
VERDICT_COLORS_CVD = {"적합": "#0072B2", "주의": "#E69F00", "부적합": "#9a3f00"}   # Okabe–Ito 계열
VERDICT_MARK = {"적합": "○", "주의": "△", "부적합": "×"}
DAY_COLORS = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9"]               # 색각 이상에도 구별되는 순서
DAY_NAMES = ["파랑", "주황", "초록", "분홍", "하늘"]                                   # 색만으로 구별하면 안 돼서 이름도 같이 씀
_SURFACE, _INK, _MUTED, _GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9"

# 제주 본섬 대략 윤곽 (참고용, 측량 아님)
JEJU_OUTLINE = [(126.16, 33.29), (126.20, 33.36), (126.27, 33.43), (126.36, 33.47), (126.45, 33.51), (126.53, 33.52),
                (126.62, 33.55), (126.72, 33.56), (126.80, 33.55), (126.88, 33.52), (126.93, 33.46), (126.94, 33.43),
                (126.90, 33.38), (126.84, 33.32), (126.75, 33.28), (126.65, 33.26), (126.56, 33.24), (126.47, 33.23),
                (126.38, 33.22), (126.28, 33.20), (126.22, 33.22), (126.16, 33.29)]


_FONT_READY: str | None = None


def setup_korean_font() -> str | None:
    """저장소 Pretendard(ui/fonts, OFL) 먼저 등록, 없으면 PC에 깔린 한글 글꼴"""
    global _FONT_READY
    if _FONT_READY:
        return _FONT_READY
    for f in sorted(_FONT_DIR.glob("*.otf")):
        try:
            font_manager.fontManager.addfont(str(f))
        except (OSError, RuntimeError):
            pass
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in _KOREAN_FONTS:
        if name in installed:
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            _FONT_READY = name
            return name
    return None  # 한글이 네모로 나오면 ui/fonts에 Pretendard 있는지 확인


def save_png(fig, path: str | Path, labels: set[str] | None = None) -> Path:
    path = Path(path)
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("반출용 시각화는 PNG/JPG만 허용됩니다.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.text(0.01, 0.005, footer(labels), fontsize=6, color="#555555", ha="left", va="bottom", wrap=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _style_axes(ax) -> None:
    ax.set_facecolor(_SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.tick_params(colors=_MUTED, length=0)
    ax.xaxis.grid(True, color=_GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def evidence_chart(table, max_rows: int = 12, column: str | None = None):
    """근거표 하나로 가로 막대. column 주면 그 열, 없으면 비율 열 → 마지막 숫자 열. 숫자 없으면 None"""
    rows = list(table.rows)[:max_rows]
    if not rows:
        return None
    cols = list(table.columns)
    num = [i for i in range(1, len(cols)) if all(_is_num(r[i]) or r[i] is None for r in rows) and any(_is_num(r[i]) for r in rows)]
    if column is not None and column in cols and cols.index(column) in num:
        i = cols.index(column)
    elif num:
        ratio = [i for i in num if all(r[i] is not None for r in rows) and any(isinstance(r[i], float) and r[i] != int(r[i]) for r in rows)]
        i = ratio[0] if ratio else num[-1]
    else:
        return None
    vals = [float(r[i] or 0) for r in rows][::-1]
    if not any(vals):
        return None
    labels = [str(r[0]) for r in rows][::-1]
    fig, ax = plt.subplots(figsize=(7.2, 0.42 * len(rows) + 1.4))
    fig.patch.set_facecolor(_SURFACE)
    _style_axes(ax)
    bars = ax.barh(labels, vals, color="#2a78d6", height=0.6)
    for b, v in zip(bars, vals):
        ax.text(b.get_width(), b.get_y() + b.get_height() / 2, f" {v:g}", va="center", fontsize=8, color=_INK)
    ax.set_xlabel(cols[i], color=_MUTED, fontsize=9)
    ax.set_title(table.title if column is None else f"{table.title} — {cols[i]}", color=_INK, fontsize=11, loc="left")
    if all(float(v).is_integer() for v in vals):
        ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax.tick_params(axis="y", labelsize=9, labelcolor=_INK)
    fig.tight_layout()
    return fig


def verdict_chart(counts: dict[str, int], title: str, cvd: bool = False):
    """적합·주의·부적합 장소 수 막대 (기호·글자도 붙임)"""
    order = ["적합", "주의", "부적합"]
    colors = VERDICT_COLORS_CVD if cvd else VERDICT_COLORS
    vals = [int(counts.get(k, 0)) for k in order]
    if not any(vals):
        return None
    fig, ax = plt.subplots(figsize=(6.4, 2.4))
    fig.patch.set_facecolor(_SURFACE)
    _style_axes(ax)
    labels = [f"{VERDICT_MARK[k]} {k}" for k in order][::-1]
    bars = ax.barh(labels, vals[::-1], color=[colors[k] for k in order][::-1], height=0.55, edgecolor=_SURFACE)
    for b, v in zip(bars, vals[::-1]):
        ax.text(b.get_width(), b.get_y() + b.get_height() / 2, f" {v}곳", va="center", fontsize=9, color=_INK)
    ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax.set_xlabel("장소 수 (곳)", color=_MUTED, fontsize=9)
    ax.set_title(title, color=_INK, fontsize=11, loc="left")
    fig.tight_layout()
    return fig


def course_map(itinerary: list[dict], title: str, others: list[tuple[float, float]] | None = None):
    """코스 지도(PNG용). 개략 윤곽 + 날짜별 색 + 순서 번호 + 이름. 지도 타일 안 써서 인터넷 없이 됨"""
    pts = [p for p in itinerary if p.get("lat") is not None]
    if not pts:
        return None
    fig, ax = plt.subplots(figsize=(8.0, 5.0))
    fig.patch.set_facecolor(_SURFACE)
    ax.set_facecolor("#eef4f8")
    xs, ys = zip(*JEJU_OUTLINE)
    ax.fill(xs, ys, color="#ffffff", ec="#9fb3c4", lw=1.0, zorder=1)
    if others:
        ox, oy = zip(*[(lo, la) for la, lo in others])
        ax.scatter(ox, oy, s=10, color="#b5b4ab", zorder=2, label="다른 후보")
    days = sorted({p["day"] for p in pts})
    for d in days:
        seq = sorted((p for p in pts if p["day"] == d), key=lambda p: p["order"])
        c = DAY_COLORS[(d - 1) % len(DAY_COLORS)]
        ax.plot([p["lng"] for p in seq], [p["lat"] for p in seq], color=c, lw=1.6, zorder=3, alpha=0.8)
        ax.scatter([p["lng"] for p in seq], [p["lat"] for p in seq], s=140, color=c, ec="white", lw=1.2, zorder=4,
                   label=f"{d}일차")
        for p in seq:
            ax.text(p["lng"], p["lat"], str(p["order"]), color="white", fontsize=8, ha="center", va="center", zorder=5,
                    fontweight="bold")
            ax.text(p["lng"] + 0.012, p["lat"] + 0.008, p["name"], fontsize=7.5, color=_INK, zorder=5)
    ax.set_xlim(126.12, 127.0)
    ax.set_ylim(33.17, 33.6)
    ax.set_aspect(1 / np.cos(np.radians(33.4)))
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.legend(loc="lower right", fontsize=8, frameon=False)
    ax.set_title(title, color=_INK, fontsize=11, loc="left")
    fig.text(0.99, 0.02, "윤곽은 참고용 개략도", fontsize=6, color=_MUTED, ha="right")
    fig.tight_layout()
    return fig


def report_charts(resp, cvd: bool = False) -> list[tuple[str, object]]:
    """분석 종류에 맞는 차트 목록 [(파일 이름 일부, figure)]"""
    setup_korean_font()   # 스크립트·테스트에서 불러도 한글 안 깨지게
    out: list[tuple[str, object]] = []
    for r in resp.results:
        ev = {e.id: e for e in r.evidence}
        if r.agent == "access":
            p = r.payload
            fig = verdict_chart({"적합": p.get("n_ok", 0), "주의": p.get("n_caution", 0), "부적합": p.get("n_block", 0)},
                                "조건별 판정 (장소 수)", cvd)
            if fig is not None:
                out.append(("verdicts", fig))
            if "A2" in ev and ev["A2"].rows and ev["A2"].rows[0][1]:
                f2 = evidence_chart(ev["A2"], column="장소 수")
                if f2 is not None:
                    out.append(("reasons", f2))
        elif r.agent == "course":
            fig = course_map(r.payload.get("itinerary", []), f"{len({p['day'] for p in r.payload.get('itinerary', [])})}일 코스")
            if fig is not None:
                out.append(("course_map", fig))
            if "C2" in ev:
                f2 = evidence_chart(ev["C2"], column="이동 합계(분)")
                if f2 is not None:
                    out.append(("daily_move", f2))
        elif r.agent == "status":
            if "S1" in ev:
                f1 = evidence_chart(ev["S1"], column="확인 비율")
                if f1 is not None:
                    out.append(("toilet_ratio", f1))
            if "S2" in ev:
                f2 = evidence_chart(ev["S2"], column="정보 없음 비율")
                if f2 is not None:
                    out.append(("info_gap", f2))
    return out
