"""에이전트 응답 → 화면 값. 템플릿은 여기 값만 씀 (계산·판단 안 함)"""
from __future__ import annotations

import html
import itertools
import re
from collections.abc import Callable
from datetime import date, timedelta

from jeju_agent.agents.access import CARD_MAX
from jeju_agent.agents.course import shop_basis, shop_entry
from jeju_agent.contracts import Intent, OrchestratorResponse
from jeju_agent.friendly import search_links, soften
from jeju_agent.report.cards import facility_rows, info_items, source_line
from jeju_agent.text import fee_text
from jeju_agent.ui_helpers import split_answer

from . import jeju_map

VERDICT = {"적합": ("ok", "○"), "주의": ("warn", "△"), "부적합": ("bad", "×")}
FAC_SHORT = {"acc_toilet": "화장실", "acc_parking": "전용 주차", "ramp": "경사로", "elevator": "승강기",
             "wheelchair_rental": "휠체어 대여", "stroller_rental": "유아차 대여", "braille": "점자", "audio_guide": "음성 안내"}
FAC_CLASS = {"✓": "yes", "✗": "no", "?": "unk"}
FAC_WORD = {"✓": "있음", "✗": "없음", "?": "확인 필요"}
WEEK = "월화수목금토일"


CAUTION = re.compile(r"추정치|다를 수|변동|참고 바랍|전화|확인이 필요|바뀌었을")
SENT = re.compile(r"(?<=[.!?])\s+(?=[^\s(])")


def explain_html(text: str, tidy: bool = False, notes: list[str] | None = None) -> str:
    """설명(마크다운 일부) → HTML. tidy=True면 첫 문장 요약 + 핵심 목록 + '확인할 점'으로 나눔 (내용은 안 바꿈)"""

    def inline(t: str) -> str:
        t = html.escape(soften(t))
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"^(주의|한계|참고):\s*", r"<b>\1</b> ", t)
        return re.sub(r"\s*\[(?:근거표|자료표) ([^\]]+)\]", r" <span class='ref'>(자료표 \1)</span>", t)

    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if tidy and lines:
        has_list = any(x.startswith("- ") for x in lines)
        paras = [x for x in lines if not x.startswith("- ")]
        listed = [x[2:] for x in lines if x.startswith("- ")]
        if has_list:      # 기본 문장: 문단 줄은 요약·보조 줄로, 목록은 그대로
            lead, subs, rest = (paras[0] if paras else None), paras[1:], listed
        else:             # LLM 문단: 문장으로 나눠서 첫 문장 요약, 나머지는 항목
            sents = [x for t in paras for x in SENT.split(t) if x.strip()]
            lead, subs, rest = sents[0], [], sents[1:]
        out = [f"<p class='ex-lead'>{inline(lead)}</p>"] if lead else []
        out += [f"<p class='ex-sub'>{inline(x)}</p>" for x in subs]
        keys = [x for x in rest if not CAUTION.search(x)]
        warns = [x for x in rest if CAUTION.search(x)] + list(notes or [])
        if keys:
            out.append("<ul class='ex-keys'>" + "".join(f"<li>{inline(x)}</li>" for x in keys) + "</ul>")
        if warns:
            out.append("<div class='ex-warn'><h3>확인할 점</h3><ul>" + "".join(f"<li>{inline(x)}</li>" for x in warns) + "</ul></div>")
        return "".join(out)

    out, items = [], []
    for t in lines:
        if t.startswith("- "):
            items.append(f"<li>{inline(t[2:])}</li>")
            continue
        if items:
            out.append("<ul>" + "".join(items) + "</ul>")
            items = []
        out.append(f"<p>{inline(t)}</p>")
    if items:
        out.append("<ul>" + "".join(items) + "</ul>")
    return "".join(out)


def verdict(v: str) -> dict:
    cls, mark = VERDICT[v]
    return {"cls": cls, "mark": mark, "word": v}


def facilities(card: dict) -> list[dict]:
    """조건으로 확인한 시설 먼저, 그다음 기본 4가지 중 빠진 것"""
    out = list(card.get("facilities", []))
    have = {f["key"] for f in out}
    out += [f for f in card.get("basic_facilities", []) if f["key"] not in have]
    return [{"label": FAC_SHORT.get(f["key"], soften(f.get("label", f["key"]))), "cls": FAC_CLASS.get(f.get("mark", "?"), "unk"),
             "mark": f.get("mark", "?"), "word": FAC_WORD.get(f.get("mark", "?"), "확인 필요"), "need": bool(f.get("needed"))}
            for f in out]


def day_date(start: str | None, day: int) -> str | None:
    if not start:
        return None
    d = date.fromisoformat(start) + timedelta(days=day - 1)
    return f"{d.month}월 {d.day}일 {WEEK[d.weekday()]}요일"


def _move(p: dict, first: bool) -> str:
    m = p.get("leg_min") or 0
    how = "길찾기" if p.get("leg_src") == "길찾기" else "추정"
    if first:
        return "출발지 바로 근처" if m == 0 else f"출발해서 약 {m}분 ({how})"
    return "바로 근처" if m == 0 else f"약 {m}분 이동 ({how})"


ENTRY_CLASS = {"가능": "yes", "주의": "warn", "정보 없음": "unk"}


def _extra(x: dict) -> dict:
    """식당·숙소 한 칸. 한눈에 볼 것(이름·전화·화장실·출입·거리)은 위에 진하게,
    나머지(출입 메모·고른 이유·전화로 물을 말·조사 시점)는 '더 보기'에 접어 둠
    """
    toilet = (x.get("facilities") or [{"state": "정보 없음", "mark": "?"}])[0]
    entry = next((f for f in x.get("facilities") or [] if f.get("key") == "entry"), None)
    entry_state = shop_entry(x)
    dist = None
    if x.get("distance_km") is not None:
        dist = f"{x['anchor_name']}에서 {'약 ' if x.get('estimated') else ''}{x['distance_km']:g}km"
    lines = [d for d in (dist, f"동선에서 약 {x['meal_min']}분 더 가요" if x.get("meal_min") else None) if d]
    links = search_links(x["name"])
    return {"kind": "lodge" if x["kind"] == "lodging" else "meal", "slot": x["slot"], "name": x["name"],
            "where": ", ".join(lines),
            "toilet": f"휠체어 화장실 {toilet['state']}" + (", 전화로 확인" if toilet["state"] == "정보 없음" else ""),
            "toilet_cls": FAC_CLASS.get(toilet.get("mark", "?"), "unk"),
            "entry": (f"출입 {entry_state}" + (f": {entry['evidence']}" if entry and entry.get("evidence") else "")) if entry else None,
            "entry_chip": f"휠체어 출입 {entry_state.replace('휠체어 진입 ', '').replace('(조사)', '')}",
            "entry_cls": ENTRY_CLASS.get(entry_state, "yes" if "가능" in entry_state else "unk"),
            "entry_note": soften(entry["evidence"]) if entry and entry.get("evidence") else None,
            "reasons": [soften(r) for r in x.get("reasons", [])],
            "calls": x.get("call_script") or [],
            "map": links["kakao"], "naver": links["photos"],
            "more_hint": " · ".join(w for w, ok in (("출입 메모", entry and entry.get("evidence")), ("전화로 물어볼 말", x.get("call_script")),
                                                   ("지도", True)) if ok),
            "phone": x.get("phone"), "tel": re.sub(r"[^0-9+]", "", x.get("phone") or ""),
            "source": shop_basis(x), "verdict": verdict(x["verdict"]) if x["verdict"] != "적합" else None}


def _missing(slot: str, food_blocked: bool) -> dict:
    why = ("장애인 화장실이 있다고 조사된 식당이 근처에 없어서 넣지 않았어요." if food_blocked and slot != "숙소"
           else f"코스 장소 근처에 휠체어로 들어갈 수 있다고 적힌 {'숙소' if slot == '숙소' else '식당'}가 자료에 없어요.")
    return {"kind": "none", "slot": slot, "why": why}


def course(resp: OrchestratorResponse, photo: Callable[[str], dict | None]) -> dict:
    """코스 결과 화면 값. photo(place_id) → {"url", "credit"} (없으면 None)"""
    req = resp.plan.request
    r = resp.results[-1]
    p = r.payload
    itin = [{**x, "num": n} for n, x in enumerate(p["itinerary"], 1)]
    cards = {c["place_id"]: c for c in p.get("cards", [])}
    extras = p.get("extras", [])
    miss = p.get("extras_miss", {})
    days = []
    for d, g in itertools.groupby(itin, key=lambda x: x["day"]):
        g = list(g)
        flow = []
        for k, x in enumerate(g):
            c = cards.get(x["place_id"], {})
            kc = c.get("key_checks") or {}
            flow.append({"type": "place", "num": x["num"], "place_id": x["place_id"], "name": x["name"],
                         "where": " · ".join(v for v in (c.get("emd"), c.get("category")) if v and v != "기타"),
                         "move": _move(x, k == 0), "verdict": verdict(x["verdict"]), "facilities": facilities(c) if c else [],
                         "fee": fee_text(c.get("fee_won")), "call": bool(kc.get("need_call")), "photo": photo(x["place_id"])})
            for e in extras:
                if e["day"] == d and e["slot"] != "숙소" and e.get("after") == x["order"]:
                    flow.append({"type": "extra", **_extra(e)})
            if k == len(g) // 2 - (0 if len(g) % 2 else 1) and "점심" in miss and d in miss["점심"]:
                flow.append({"type": "extra", **_missing("점심", p.get("extras_food_blocked", False))})
        for e in extras:
            if e["day"] == d and e["slot"] == "숙소":
                flow.append({"type": "extra", **_extra(e)})
        days.append({"day": d, "color": jeju_map.DAY_COLORS[(d - 1) % len(jeju_map.DAY_COLORS)], "date": day_date(req.start_date, d),
                     "count": len(g), "move_total": sum(x.get("leg_min") or 0 for x in g), "flow": flow})

    n = len(itin)
    n_days = len(days)
    body, notes = split_answer(resp.answer)
    verdicts = [x["verdict"] for x in itin]
    v = resp.verification
    vb, zoom = jeju_map.focus([(x["lat"], x["lng"]) for x in itin]
                              + [(e["lat"], e["lng"]) for e in extras if e.get("lat") is not None])
    map_svg = jeju_map.base_svg(
        jeju_map.route_layer(itin, extras), viewbox=vb, zoom=zoom,
        title="코스 지도",
        desc=" / ".join(f"{d['day']}일차 " + " → ".join(f["name"] for f in d["flow"] if f["type"] == "place") for d in days))
    return {
        "kind": "course",
        "said": req.params.get("said") or req.question,
        "conditions": [req.region or "제주 전체"] + [soften(w) for w in req.profile.describe()] or [],
        "title": f"{req.region or '제주'}에서 {n}곳을 골라 {n_days}일 코스를 짰어요",
        "facts": [x for x in (
            (f"{day_date(req.start_date, 1)} 출발" if req.start_date else None),
            f"적합 {verdicts.count('적합')}곳" + (f", 주의 {verdicts.count('주의')}곳" if "주의" in verdicts else ""),
            f"한 번 이동은 길어야 약 {max((x.get('leg_min') or 0) for x in itin)}분",
            (f"식당 {sum(e['slot'] != '숙소' for e in extras)}곳, 숙소 {sum(e['slot'] == '숙소' for e in extras)}곳" if extras else None),
        ) if x],
        "trust": {"ok": v.passed if v else None, "checks": f"{sum(c.ok for c in v.checks)}/{len(v.checks)}" if v else None,
                  "datasets": next((len(x.payload.get("selected", [])) for x in resp.results if x.agent == "explore"), None)},
        "map": map_svg,
        "has_meal": any(e["slot"] != "숙소" for e in extras), "has_lodge": any(e["slot"] == "숙소" for e in extras),
        "road": any(x.get("path") for x in itin),
        "bus": any(x.get("stop") for x in itin),
        "days": days,
        "explain": explain_html(body, tidy=True, notes=notes), "notes": [],
        "follow_ups": r.follow_ups,
        "intent": Intent.COURSE.value,
    }


# ── 공통 머리 (질문·조건·분석 과정·검증) ──
def header(resp: OrchestratorResponse) -> dict:
    req = resp.plan.request
    v = resp.verification
    ex = next((x for x in resp.results if x.agent == "explore"), None)
    return {"said": req.params.get("said") or req.question,
            "read_as": req.question if req.params.get("said") else None,
            "understood": soften(resp.plan.understood.replace(" — 이렇게 이해했어요. 맞나요?", "")),
            "steps": [soften(x) for x in resp.progress],
            "trust": {"ok": v.passed if v else None, "checks": f"{sum(c.ok for c in v.checks)}/{len(v.checks)}" if v else None,
                      "datasets": len(ex.payload.get("selected", [])) if ex else None},
            "center": [{"no": x["no"], "name": x["name"], "steps": x["steps"]} for x in (ex.payload.get("center_guide", []) if ex else [])]}


def evidence(resp: OrchestratorResponse, spec_rows: list[list[str]]) -> dict:
    """근거와 검증 (전부 접힘): 쓴 자료 → 근거표 → 검증 → 분석 명세 → 기록"""
    tables = []
    for r in resp.results:
        for t in r.evidence:
            tables.append({"id": t.id, "title": soften(t.title), "label": f"[{t.data_label}]", "columns": t.columns,
                           "rows": t.rows[:60], "more": max(0, len(t.rows) - 60),
                           "source": t.dataset + (f" · {t.note}" if t.note else ""), "explore": r.agent == "explore"})
    ex = next((x for x in resp.results if x.agent == "explore"), None)
    v = resp.verification
    u = resp.llm_usage
    return {"datasets": [soften(f.text) for f in ex.findings] if ex else [], "tables": tables,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in v.checks] if v else [],
            "spec": spec_rows,
            "trace": [[t.agent, t.action, soften(t.detail), t.elapsed_ms] for t in resp.trace + [t for r in resp.results for t in r.trace]],
            "usage": f"설명 생성 {'LLM' if resp.answer_source == 'llm' else '문장틀'} · LLM 호출 {u.calls}회(캐시 {u.cached}) · 예상 비용 ${u.est_cost_usd}"}


def _card(c: dict, photo: Callable[[str], dict | None]) -> dict:
    kc = c.get("key_checks") or {}
    return {"place_id": c["place_id"], "name": c["name"], "verdict": verdict(c["verdict"]),
            "where": " · ".join(v for v in (c.get("emd"), c.get("category")) if v and v != "기타"),
            "fee": fee_text(c.get("fee_won")), "facilities": facilities(c), "call": bool(kc.get("need_call")),
            "barriers": c.get("barriers", [])[:3], "photo": photo(c["place_id"])}


def access(resp: OrchestratorResponse, photo: Callable[[str], dict | None]) -> dict:
    """장소 찾기 / 장소 하나 물어보기 화면 값"""
    req = resp.plan.request
    r = resp.results[-1]
    p = r.payload
    by_id = {c["place_id"]: c for c in p.get("cards", [])}
    cards = [_card(by_id[i], photo) for i in p.get("shown_ids", []) if i in by_id][:CARD_MAX]
    body, notes = split_answer(resp.answer)
    if p.get("place_q"):
        a5 = next((t for t in r.evidence if t.id == "A5"), None)
        fact = None
        if a5 and a5.rows:
            row = a5.rows[0]
            fact = {"what": soften(str(row[1])), "state": str(row[2]), "basis": soften(str(row[3])) if len(row) > 3 else "",
                    "date": str(row[4])[:10] if len(row) > 4 else ""}
        return {"kind": "place", "card": cards[0] if cards else None, "fact": fact,
                "explain": explain_html(body), "notes": [soften(x) for x in notes], "follow_ups": r.follow_ups}
    pts = [{"num": n, "lat": v["lat"], "lng": v["lng"], "name": v["name"], "verdict": v["verdict"]}
           for n, v in enumerate([v for i in p.get("shown_ids", []) for v in p.get("verdicts", []) if v["place_id"] == i][:CARD_MAX], 1)
           if v.get("lat") is not None]
    for n, c in enumerate(cards, 1):
        c["num"] = n
    return {"kind": "access",
            "conditions": [req.region or "제주 전체"] + [soften(w) for w in req.profile.describe()],
            "title": f"조건에 맞는 곳 {p.get('n_ok', 0)}곳, 확인하고 갈 곳 {p.get('n_caution', 0)}곳을 찾았어요",
            "facts": [f"적합 {p.get('n_ok', 0)}곳", f"주의 {p.get('n_caution', 0)}곳", f"뺀 곳 {p.get('n_block', 0)}곳",
                      f"추천 순서대로 {len(cards)}곳을 보여 드려요"],
            "map": _points_map(pts),
            "cards": cards, "explain": explain_html(body, tidy=True, notes=notes), "notes": [], "follow_ups": r.follow_ups}


def _points_map(pts: list[dict]) -> str | None:
    if not pts:
        return None
    vb, zoom = jeju_map.focus([(x["lat"], x["lng"]) for x in pts])
    return jeju_map.base_svg(jeju_map.points_layer(pts), title="장소 지도", viewbox=vb, zoom=zoom,
                             desc=", ".join(f"{x['num']}. {x['name']} {x['verdict']}" for x in pts))


def status(resp: OrchestratorResponse, charts: list[str]) -> dict:
    """현황 분석(기관용): 숫자 몇 개 + 그래프 + 설명"""
    r = resp.results[-1]
    ev = {t.id: t for t in r.evidence}
    facts = []
    s1, s2 = ev.get("S1"), ev.get("S2")
    if s1 is not None and s1.rows:
        n = sum(int(row[1]) for row in s1.rows)
        toilet = sum(int(row[2]) for row in s1.rows)
        facts += [f"관광지 {n}곳", f"휠체어 화장실 확인 {toilet / n:.0%}" if n else ""]
    if s2 is not None and s2.rows:
        worst = max(s2.rows, key=lambda x: float(x[4]))
        facts.append(f"정보가 가장 비어 있는 항목: {soften(str(worst[0]))}({float(worst[4]):.0%})")
    body, notes = split_answer(resp.answer)
    return {"kind": "status", "title": soften(r.summary), "facts": [f for f in facts if f], "charts": charts,
            "explain": explain_html(body), "notes": [soften(x) for x in notes], "follow_ups": r.follow_ups}


def text(resp: OrchestratorResponse) -> dict:
    """되묻기·보고서·답 못하는 질문 같은 글 답"""
    body, notes = split_answer(resp.answer)
    edit = resp.plan.request.params.get("edit") or {}
    choices = []
    if edit.get("kind") == "ask":
        choices = [{"label": c, "q": c} if isinstance(c, str) else {"label": c["label"], "q": c["q"]} for c in edit.get("choices", [])]
    r = resp.results[-1] if resp.results else None
    return {"kind": "text", "explain": explain_html(body), "notes": [soften(x) for x in notes], "choices": choices,
            "follow_ups": r.follow_ups if r else [], "report": resp.plan.request.intent == Intent.REPORT}


def place_detail(resp: OrchestratorResponse, place_id: str, photos: Callable[[str, str], list[dict]]) -> dict | None:
    """장소 자세히 보기: 확인할 점 → 시설 → 장애물 → 현장 안내 → 근처 가게 → 전화로 물을 말 → 출처
    photos(place_id, kind) → [{"url", "caption"}]
    """
    card = next((c for r in resp.results for c in r.payload.get("cards", []) if c["place_id"] == place_id), None)
    if card is None:
        return None
    stop = next((x for r in resp.results for x in r.payload.get("itinerary", []) if x["place_id"] == place_id), None)
    extras = [x for r in resp.results for x in r.payload.get("extras", [])
              if stop and x["day"] == stop["day"] and (x.get("after") == stop["order"])]
    kc = card.get("key_checks")
    mark = {"ok": "✓", "warn": "⚠", "unknown": "?"}
    return {
        "card": _card(card, lambda _: None),
        "stop": {"day": stop["day"], "order": stop["order"], "why": [soften(w) for w in stop.get("why", [])],
                 "guide": {k: soften(v) for k, v in (stop.get("guide") or {}).items()}} if stop else None,
        "checks": {"title": kc["title"], "need_call": kc["need_call"],
                   "rows": [{"mark": mark[i["state"]], "cls": {"ok": "yes", "warn": "no", "unknown": "unk"}[i["state"]],
                              "label": i["label"], "text": i["text"]} for i in kc["items"]]} if kc else None,
        "facilities": [(soften(k), soften(v)) for k, v in facility_rows({**card, "facilities": card.get("facilities", [])
                                                                          + [f for f in card.get("basic_facilities", [])
                                                                             if f["key"] not in {g["key"] for g in card.get("facilities", [])}]})],
        "barriers": card.get("barriers", []), "barrier_spots": card.get("barrier_spots", []),
        "info": [(soften(k), soften(v)) for k, v in info_items(card)],
        "nearby": [{"name": f"{n['category']} {n['name']}", "text": f"{n['distance_km']:g}km, 휠체어 화장실 "
                    f"{'있음' if n['disabled_toilet'] == 'Y' else '없음'}(2021-12-22 조사)", "phone": n.get("phone")}
                   for n in card.get("nearby", [])],
        "extras": [_extra(x) for x in extras],
        "calls": card.get("call_script", []),
        "source": source_line(card, file=False),
        "photos": photos(place_id, "rep") + photos(place_id, "access") + photos(place_id, "route"),
    }
