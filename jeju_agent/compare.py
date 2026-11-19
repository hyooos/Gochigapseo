"""조건 바꿔서 다시 돌렸을 때 전/후 비교
판정은 새로 안 만들고, 두 결과에 이미 있는 판정을 세기만 함
"""
from __future__ import annotations

from typing import Any

from .text import date_label

VERDICTS = ("적합", "주의", "부적합")


def _access(resp) -> Any | None:
    """장소 찾기(access) 결과. 코스면 코스가 불러 쓴 것"""
    return next((r for r in reversed(resp.results) if r.agent == "access" and r.payload.get("verdicts")), None)


def conditions(resp) -> list[str]:
    req = resp.plan.request
    out = [req.region or "제주 전체"]
    if req.intent.value == "course":
        out.append(f"{req.days or 1}일 코스")
        if req.start_date:
            out.append(f"{date_label(req.start_date)} 출발")
    else:
        out.append("갈 만한 곳 찾기")
    return out + (req.profile.describe() or ["이동 조건 없음"])


def snapshot(resp) -> dict | None:
    acc = _access(resp)
    if acc is None:
        return None
    verdicts = {v["place_id"]: (v["name"], v["verdict"]) for v in acc.payload["verdicts"]}
    main = resp.results[-1]
    if main.agent == "course" and main.payload.get("itinerary"):
        kind = "course"
        picked = [(p["place_id"], p["name"]) for p in main.payload["itinerary"]]
    else:
        kind = "access"
        picked = [(c["place_id"], c["name"]) for c in acc.payload.get("cards", [])]
    return {"kind": kind, "verdicts": verdicts, "picked": picked, "conditions": conditions(resp)}


def compare(before, after) -> dict | None:
    """두 응답 비교. 하나라도 판정이 없으면 None (되묻기·현황 등)"""
    a, b = snapshot(before), snapshot(after)
    if a is None or b is None:
        return None
    # 적합·주의·부적합 개수 (전, 후)
    count = {v: (sum(1 for _, x in a["verdicts"].values() if x == v), sum(1 for _, x in b["verdicts"].values() if x == v))
             for v in VERDICTS}
    # 둘 다 있는 장소 중 판정 바뀐 곳
    changed = [(b["verdicts"][pid][0], a["verdicts"][pid][1], b["verdicts"][pid][1])
               for pid in a["verdicts"] if pid in b["verdicts"] and a["verdicts"][pid][1] != b["verdicts"][pid][1]]
    order = {v: i for i, v in enumerate(VERDICTS)}
    changed.sort(key=lambda x: (order[x[2]] - order[x[1]], x[0]))   # 좋아진 곳(주의→적합) 먼저
    ids_a, ids_b = [p for p, _ in a["picked"]], [p for p, _ in b["picked"]]
    names = dict(a["picked"] + b["picked"])
    return {
        "kind": b["kind"], "same_kind": a["kind"] == b["kind"],
        "count": count,
        "changed": changed,
        "added": [names[p] for p in ids_b if p not in ids_a],
        "removed": [names[p] for p in ids_a if p not in ids_b],
        "cond_removed": [c for c in a["conditions"] if c not in b["conditions"]],
        "cond_added": [c for c in b["conditions"] if c not in a["conditions"]],
    }


def _course(resp) -> dict | None:
    main = resp.results[-1] if resp.results else None
    if main is None or main.agent != "course" or not main.payload.get("itinerary"):
        return None
    days: dict[int, list[tuple[str, str]]] = {}
    for x in sorted(main.payload["itinerary"], key=lambda x: (x["day"], x["order"])):
        days.setdefault(x["day"], []).append((x["place_id"], x["name"]))
    extras = {(e["day"], e["slot"]): e["name"] for e in main.payload.get("extras", [])}
    return {"days": days, "extras": extras}


def changes(before, after) -> dict | None:
    """'이번에 바뀐 점' (결과 맨 위). 두 결과 payload만 비교
    rows: 화면 줄 [{"kind", "day", "text"}], new_ids: 새로 들어온 장소
    """
    if before is None or after is None:
        return None
    rows: list[dict] = []
    ca, cb = conditions(before), conditions(after)
    gone_c, new_c = [c for c in ca if c not in cb], [c for c in cb if c not in ca]
    if gone_c or new_c:
        if len(gone_c) == len(new_c):   # 같은 자리 조건이 바뀐 것 (2일 코스 → 3일 코스)
            text = ", ".join(f"{x} → {y}" for x, y in zip(gone_c, new_c))
        else:
            text = ", ".join([f"{c} 뺌" for c in gone_c] + [f"{c} 더함" for c in new_c])
        rows.append({"kind": "cond", "day": None, "text": "바꾼 조건: " + text})
    a, b = _course(before), _course(after)
    new_ids: list[str] = []
    changed_days: list[int] = []
    if a is not None and b is not None:
        where_a = {pid: d for d, ps in a["days"].items() for pid, _ in ps}
        where_b = {pid: d for d, ps in b["days"].items() for pid, _ in ps}
        names = {pid: n for ds in (a["days"], b["days"]) for ps in ds.values() for pid, n in ps}
        for d in sorted(set(a["days"]) | set(b["days"])):
            pa, pb = [p for p, _ in a["days"].get(d, [])], [p for p, _ in b["days"].get(d, [])]
            if pa == pb:
                continue
            changed_days.append(d)
            if not pa:
                rows.append({"kind": "add", "day": d, "text": f"{d}일차가 새로 생겼어요: " + " → ".join(names[p] for p in pb)})
                new_ids += [p for p in pb if p not in where_a]
                continue
            if not pb:
                rows.append({"kind": "remove", "day": d, "text": f"{d}일차가 없어졌어요(" + ", ".join(names[p] for p in pa) + ")"})
                continue
            gone = [p for p in pa if p not in pb]
            came = [p for p in pb if p not in pa]
            head = f"{d}일차 {len(pa)}곳 → {len(pb)}곳" if len(pa) != len(pb) else f"{d}일차"
            bits = []
            for p in gone:
                bits.append(f"{names[p]} " + (f"{where_b[p]}일차로 옮김" if p in where_b else "빠짐"))
            for p in came:
                bits.append(f"{names[p]} " + (f"{where_a[p]}일차에서 옮겨 옴" if p in where_a else "새로 들어옴"))
                if p not in where_a:
                    new_ids.append(p)
            if not bits:
                bits.append("순서만 바꿈: " + " → ".join(names[p] for p in pb))
            kind = "order" if not gone and not came else "add" if not gone else "remove" if not came else "move"
            rows.append({"kind": kind, "day": d, "text": f"{head}: " + ", ".join(bits)})
        for k in sorted(set(a["extras"]) | set(b["extras"])):
            x, y = a["extras"].get(k), b["extras"].get(k)
            if x != y:
                d, slot = k
                text = f"{d}일차 {slot}: " + (f"{x} → {y}" if x and y else f"{y} 더함" if y else f"{x} 뺌")
                rows.append({"kind": "swap", "day": d, "text": text})
        same = [d for d in sorted(b["days"]) if d not in changed_days]
        if same and rows:
            rows.append({"kind": "same", "day": None, "text": "그대로인 날: " + ", ".join(f"{d}일차" for d in same)})
    else:
        cmp = compare(before, after)
        if cmp is not None:
            for v in VERDICTS:
                x, y = cmp["count"][v]
                if x != y:
                    rows.append({"kind": "add" if y > x else "remove", "day": None, "text": f"{v} {x}곳 → {y}곳"})
            if cmp["added"]:
                rows.append({"kind": "add", "day": None, "text": "새로 보인 곳: " + ", ".join(cmp["added"][:6])})
            if cmp["removed"]:
                rows.append({"kind": "remove", "day": None, "text": "빠진 곳: " + ", ".join(cmp["removed"][:6])})
            for n, x, y in cmp["changed"][:4]:
                rows.append({"kind": "move", "day": None, "text": f"{n}: {x} → {y}"})
    if not rows:
        rows.append({"kind": "same", "day": None, "text": "장소와 순서는 그대로예요. 설명과 계산만 새로 했어요."})
    return {"rows": rows, "new_ids": new_ids, "changed_days": changed_days}
