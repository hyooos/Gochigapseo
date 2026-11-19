"""장소 카드 → 마크다운. 화면이랑 보고서(builder.py)가 같은 문장 씀
기호만으로 뜻 전하면 안 돼서 항상 낱말(있음·없음·정보 없음)도 같이 씀
"""
from __future__ import annotations

import re

from ..data.visitjeju import is_tag_evidence

VERDICT_SYMBOL = {"적합": "○", "주의": "△", "부적합": "×"}


def card_title(c: dict) -> str:
    """점수·정보 없음 수는 근거표 A1에 있어서 제목엔 판정만. 분류 '기타'는 뺌"""
    where = " · ".join(x for x in (c.get("emd"), c.get("category")) if x and x != "기타")
    return f"{VERDICT_SYMBOL.get(c['verdict'], '')} {c['name']}" + (f" ({where})" if where else "") + f" — {c['verdict']}"


def facility_lines(c: dict) -> list[str]:
    """확인된 시설이랑 꼭 필요한 시설은 한 줄씩, 나머지 '정보 없음'은 한 줄로 묶음"""
    out, unknown = [], []
    for f in c["facilities"]:
        state = f.get("state") or {"✓": "있음", "✗": "없음"}.get(f.get("mark"), "정보 없음")   # 예전 결과(상태 없음)도 그리게
        if state == "정보 없음" and not f.get("needed"):
            unknown.append(f["label"])
            continue
        need = " (꼭 필요)" if f.get("needed") else ""
        if is_tag_evidence(f.get("evidence")):   # 빈칸을 비짓제주 태그로 채운 값이라 조사 원문처럼 인용하면 안 됨
            ev = f"{f['evidence']} (조사 자료에는 언급 없음)"
        elif f.get("evidence"):
            ev = f"원문: “{f['evidence']}”"
        else:
            ev = "자료에 언급 없음 → 방문 전 확인"
        out.append(f"{f.get('mark', '?')} {f['label']} {state}{need} — {ev}")
    if unknown:
        out.append(f"? 정보 없음(자료에 언급 없음 → 방문 전 확인): {', '.join(unknown)}")
    return out


def facility_rows(c: dict) -> list[tuple[str, str]]:
    """시설을 (✓ 이름, 상태 — 근거)로. 꼭 필요하지 않은 '정보 없음'은 한 줄로 (화면 '자세히 보기')"""
    rows, unknown = [], []
    for f in c["facilities"]:
        state = f.get("state") or {"✓": "있음", "✗": "없음"}.get(f.get("mark"), "정보 없음")
        if state == "정보 없음" and not f.get("needed"):
            unknown.append(f["label"])
            continue
        if is_tag_evidence(f.get("evidence")):
            ev = f"{f['evidence']} (조사 자료에는 언급 없음)"
        elif f.get("evidence"):
            ev = f"원문: “{f['evidence']}”"
        else:
            ev = "자료에 언급 없음 → 방문 전 확인"
        rows.append((f"{f.get('mark', '?')} {f['label']}", f"{state}{' (꼭 필요)' if f.get('needed') else ''} — {ev}"))
    if unknown:
        rows.append(("? 정보 없음", f"{', '.join(unknown)} — 자료에 언급 없음 → 방문 전 확인"))
    return rows


KEY_MARK = {"ok": "✓", "warn": "⚠", "unknown": "?"}
NEED_CALL = "방문 전 추가 확인 필요"


def key_check_line(c: dict) -> str | None:
    """이동 조건별 '확인할 점' 한 줄 (판정은 그대로)"""
    kc = c.get("key_checks")
    if not kc:
        return None
    items = " · ".join(f"{i['label']} {KEY_MARK[i['state']]} {i['text']}" for i in kc["items"])
    return f"확인할 점({kc['title']}): {items}" + (f" → {NEED_CALL}(아래 전화로 물어볼 말)" if kc["need_call"] else "")


def info_items(c: dict) -> list[tuple[str, str]]:
    """현장 안내 (항목, 내용). 화면 표랑 보고서 줄(info_lines)이 같이 씀"""
    out = [("조사자 의견", s) for s in c.get("survey", [])]
    if c.get("route"):
        out.append(("추천 동선(조사자 기록)", c["route"]))
    if c.get("toilet_location"):
        out.append(("장애인 화장실 위치", c["toilet_location"]))
    if c.get("alt_toilet"):
        out.append(("주변(대체) 화장실", f"{c['alt_toilet']} — 장애인용인지는 자료로 확인되지 않음, 방문 전 확인"))
    if c.get("parking_location"):
        out.append(("주차 위치", c["parking_location"]))
    if c.get("fee_won") == 0:
        out.append(("입장료", "없음(조사 당시)"))
    elif c.get("fee_won"):
        out.append(("입장료(일반, 조사 당시)", f"{c['fee_won']:,}원" + (f" — 원문: {c['fee_note']}" if c.get("fee_note") else "")))
    elif c.get("fee_note"):
        out.append(("입장료(조사 당시 원문)", c["fee_note"]))
    if c.get("hours_note"):
        out.append(("이용 시간(조사 당시)", c["hours_note"]))
    return out


def info_lines(c: dict) -> list[str]:
    """현장 안내 (정리본 원문 그대로). 값 없으면 줄 안 만듦 (없는 정보 지어내면 안 됨)"""
    out = [f"{k}: {v}" for k, v in info_items(c)]
    if c.get("nearby"):
        items = [f"{n['category']} {n['name']} {n['distance_km']:g}km · 장애인 화장실 {'있음' if n['disabled_toilet'] == 'Y' else '없음'}(조사 당시)"
                 + (f" · ☎ {n['phone']}" if n.get("phone") else "") for n in c["nearby"]]
        out.append("근처 휠체어 진입 가능으로 조사된 곳(2021-12-22 조사, 지금은 다를 수 있어 전화 확인): " + " / ".join(items))
    return out


def source_line(c: dict, file: bool = True) -> str:
    """file=False면 원본 파일 이름 뺌 (화면 카드용)"""
    date = c.get("data_date") or "모름"
    if c.get("data_date") and c.get("date_basis") == "등록일":   # 등록일은 조사일 아님 (2019 자료도 2021-02-09 일괄 등록)
        date += " 이전 조사(데이터허브 등록일)"
    old = " → 현장이 바뀌었을 수 있어 방문 전 확인" if c.get("stale") else ""
    src = f" · 원본 파일 {c['source_file']}" if file and c.get("source_file") else ""
    return f"출처: {c['source']}{src} · 자료 기준일 {date}{old}"


def _shorten(line: str, quote: int = 70, total: int = 120) -> str:
    """보고서용: 긴 원문 인용은 앞 quote자만, 줄 전체는 total자까지. 원문 전체는 화면이랑 원본 파일에 있음"""
    line = re.sub(rf"“([^”]{{{quote}}})[^”]+”", r"“\1…”", line)
    return line if len(line) <= total else line[:total].rstrip() + "…"


def common_questions(cards: list[dict]) -> list[str]:
    """모든 장소에 똑같이 나오는 전화 문의 (보고서에선 한 번만)"""
    if not cards:
        return []
    rest = [set(c.get("call_script", [])) for c in cards[1:]]
    return [q for q in cards[0].get("call_script", []) if all(q in r for r in rest)]


def card_markdown(c: dict, level: int = 4, common: list[str] | None = None, short: bool = False) -> str:
    """장소 카드 1장(보고서). common 주면 그 질문은 빼고 이 장소 것만. short면 긴 원문 줄임"""
    fix = _shorten if short else (lambda x: x)
    lines = [f"{'#' * level} {card_title(c)}", ""]
    if kc := key_check_line(c):
        lines.append(f"- {kc}")
    lines += [f"- {fix(x)}" for x in facility_lines(c)]
    if c.get("barriers"):
        spots = f" — 기록된 곳: {', '.join(c['barrier_spots'])}" if c.get("barrier_spots") else ""
        lines.append(f"- 기록된 장애물: {', '.join(c['barriers'])}{spots}")
    lines += [f"- {fix(x)}" for x in info_lines(c)]
    lines += ["", source_line(c)]
    own = [q for q in c["call_script"] if q not in set(common or [])]
    if own:
        lines += ["", "전화로 물어볼 말:" if common is None else "이 장소에 더 물어볼 말:"]
        lines += [f"{i}. {q}" for i, q in enumerate(own, 1)]
    return "\n".join(lines) + "\n"
