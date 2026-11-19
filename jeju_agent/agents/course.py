"""코스 설계 에이전트: 접근성 진단 결과로 날짜별 코스 짬

- 후보는 적합 → 주의 순. 부적합은 안 넣음
- 속도: 수동 휠체어·고령·'자주 쉬어야 함'은 여유(하루 2곳), 나머지는 보통(하루 3곳)
- 출발점에서 '점수 - 이동시간×0.8' 큰 곳부터, 이동 상한·하루 시간 안에서만
- 비 오는 날 많은 달(센터 5-02)이면 하루 1곳은 실내
- LLM은 속도·실내 수·이동 상한·화장실 장소만 정함. 안전 조건이랑 이용자가 정한 건 못 바꿈
- 점검(payload.issues)에서 고칠 게 있으면 한 번 더 짬
- 이동 시간은 카카오 길찾기(키 있을 때), 버스는 추정치
- 점심은 그날 가운데 장소 뒤, 숙소는 하루 끝. 숙소 좌표 있으면 다음 날 거기서 출발
"""
from __future__ import annotations

import math
from itertools import permutations

import numpy as np
import pandas as pd

from ..config import access_rules
from ..contracts import (
    INTEREST_CATEGORIES,
    INTEREST_KO,
    KIND_KO,
    AgentResult,
    AgentWarning,
    AnalysisRequest,
    EvidenceTable,
    Finding,
    TravelerProfile,
)
from ..data import kto as kto_data
from ..data.center import nearest_stops, place_weather, weather_month_stats
from ..data.regions import region_center
from ..text import josa
from ..tools.geo import LiveXY, haversine_km, road_min, road_path, travel_min
from .access import (
    NEARBY_SOURCE,
    kto_items,
    nearby_rows,
    needed_facilities,
    place_items,
)
from .base import AgentContext, AgentTool, BaseAgent, default_observe, uses

PACES = ("여유", "보통")
LEG_OPTIONS = (30, 45, 60)
MAX_PLAN_RUNS = 2    # 처음 + 점검 뒤 한 번 더
MIN_PER_DAY = 2      # 식당 때문에 줄여도 하루 이만큼은 남김 (하루 1곳 코스 안 되게)
ROAD_TRIES = 5      # 길찾기: 실제 시간이 상한 넘는 후보를 이만큼까지 건너뜀 (호출 수 제한)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def forced_relaxed(p: TravelerProfile, rules: dict) -> bool:
    return p.mobility in rules["course"]["relaxed_profiles"] or "rest_often" in p.needs


def schedule(cands: pd.DataFrame, days: int, per_day: int, visit_min: int, day_minutes: int, max_leg: int,
             indoor_per_day: int, transport: str, rules: dict, start: tuple[float, float],
             hard_cap: int | None = None, home: bool = False,
             day_caps: dict[int, int] | None = None, road=None,
             fixed: dict[int, list[str]] | None = None, day_end=None,
             reorder: bool = True, toilet_per_day: int = 0) -> tuple[list[list[dict]], list[int]]:
    """출발점에서 '점수 - 이동시간×벌점' 큰 곳부터 고름. 반환: (날짜별 장소, 실내 못 채운 날)

    - hard_cap(이용자가 정한 상한)은 절대 안 넘음. 하루 첫 이동만 상한 1.5배까지
    - home=True면 매일 start에서, 아니면 전날 마지막 장소(또는 day_end가 준 숙소)에서 출발
    - day_caps={일차: 곳 수}, fixed={일차: [place_id]}: 수정 대화로 정한 장소 수·안 고치는 날
    - road(a, b): 길찾기 실제 시간 (None이면 추정치), reorder: 이동 제일 짧은 순서로
    - indoor_per_day·toilet_per_day: 모자라면 그런 장소부터 고름
    """
    cr = rules["course"]
    pool = cands.reset_index(drop=True)
    used: set[int] = set()
    fixed = {d: [i for pid in ids for i in pool.index[pool["place_id"] == pid][:1]] for d, ids in (fixed or {}).items()}
    for idxs in fixed.values():   # 고정한 날 장소는 다른 날이 못 가져가게 미리 씀 처리
        used.update(idxs)
    plan: list[list[dict]] = []
    indoor_short: list[int] = []
    day_start = start
    def stop(row, leg: tuple[int, str]) -> dict:
        return {"place_id": row["place_id"], "name": row["name"], "verdict": row["verdict"], "score": int(row["score"]),
                "lat": float(row["lat"]), "lng": float(row["lng"]), "indoor": _num(row.get("indoor")),
                "acc_toilet": _num(row.get("acc_toilet")), "fee_won": _num(row.get("fee_won")),
                "leg_min": leg[0], "leg_src": leg[1], "reasons": row.get("reasons") or [], "category": row.get("category")}

    def est(a: tuple[float, float], b: tuple[float, float]) -> int:
        return int(travel_min(haversine_km(a[0], a[1], np.array([b[0]]), np.array([b[1]])), transport, cr)[0])

    def reordered(start_xy: tuple[float, float], day: list[dict], spent_limit: int) -> list[dict]:
        """순서 다듬기: 직선 이동 합이 제일 짧은 순서로 (1분 이상 줄 때만)"""
        if len(day) < 2:
            return day
        xy = [(p["lat"], p["lng"]) for p in day]

        def total(order) -> int:
            cur, s = start_xy, 0
            for i in order:
                s += est(cur, xy[i])
                cur = xy[i]
            return s
        now = tuple(range(len(day)))
        best = min(permutations(now), key=total)
        if total(best) >= total(now) - 1:
            return day
        cur, out, spent = start_xy, [], 0
        for k, i in enumerate(best):
            real = road(cur, xy[i]) if road else None
            leg = (int(real), "길찾기") if real is not None else (est(cur, xy[i]), "추정")
            limit = max_leg * 1.5 if k == 0 else max_leg
            if hard_cap is not None:
                limit = min(limit, hard_cap)
            if leg[0] > limit:
                return day
            spent += leg[0] + visit_min
            out.append({**day[i], "leg_min": leg[0], "leg_src": leg[1]})
            cur = xy[i]
        return out if spent <= spent_limit else day

    def keep(d: int, start_xy: tuple[float, float]) -> list[dict] | None:
        """고정한 날 그대로 다시 놓음. 이동이 hard_cap 넘으면 None (다시 짬)"""
        cur, out = start_xy, []
        for idx in fixed.get(d, []):
            row = pool.iloc[idx]
            xy = (float(row["lat"]), float(row["lng"]))
            real = road(cur, xy) if road else None
            leg = (int(real), "길찾기") if real is not None else (
                int(travel_min(haversine_km(cur[0], cur[1], np.array([xy[0]]), np.array([xy[1]])), transport, cr)[0]), "추정")
            if hard_cap is not None and leg[0] > hard_cap:
                return None
            out.append(stop(row, leg))
            cur = xy
        return out

    for d in range(1, days + 1):
        if fixed.get(d):
            kept = keep(d, day_start)
            if kept is not None:
                plan.append(kept)
                nxt = day_end(d, kept) if day_end and kept else None
                day_start = start if home else (nxt or (kept[-1]["lat"], kept[-1]["lng"]))
                continue
            used.difference_update(fixed[d])   # 고정 풀었으니 그날 장소도 다시 고를 수 있음
        cur, spent, day = day_start, 0, []
        want = (day_caps or {}).get(d, per_day)
        while len(day) < want:
            left = [i for i in range(len(pool)) if i not in used]
            if not left:
                break
            sub = pool.iloc[left]
            km = haversine_km(cur[0], cur[1], sub["lat"].astype(float).to_numpy(), sub["lng"].astype(float).to_numpy())
            mins = travel_min(km, transport, cr)
            need_indoor = indoor_per_day - sum(1 for d in day if d["indoor"] == 1)
            slots = want - len(day)
            limit = max_leg * 1.5 if not day else max_leg
            if hard_cap is not None:
                limit = min(limit, hard_cap)
            ok = (mins <= limit) & (spent + mins + visit_min <= day_minutes)
            if need_indoor > 0 and slots <= need_indoor:
                ok_in = ok & (sub["indoor"].to_numpy() == 1)
                if ok_in.any():
                    ok = ok_in
                elif d not in indoor_short:
                    indoor_short.append(d)
            need_toilet = toilet_per_day - sum(1 for d in day if d["acc_toilet"] == 1)
            if need_toilet > 0 and slots <= need_toilet and "acc_toilet" in sub:
                ok_t = ok & (pd.to_numeric(sub["acc_toilet"], errors="coerce").to_numpy() == 1)
                if ok_t.any():
                    ok = ok_t
            if not ok.any():
                break
            value = np.where(ok, sub["score"].to_numpy(float) - cr["leg_penalty_per_min"] * mins, -np.inf)
            leg = None
            for _ in range(ROAD_TRIES):
                j = int(np.argmax(value))
                if value[j] == -np.inf:
                    break
                real = road(cur, (float(sub["lat"].iloc[j]), float(sub["lng"].iloc[j]))) if road else None
                if real is None:
                    leg = (int(mins[j]), "추정")
                    break
                if real <= limit and spent + real + visit_min <= day_minutes:
                    leg = (real, "길찾기")
                    break
                value[j] = -np.inf     # 실제 길로는 상한 넘음 → 다음 후보
            if leg is None:
                break
            idx = left[j]
            row = pool.iloc[idx]
            used.add(idx)
            spent += leg[0] + visit_min
            day.append(stop(row, leg))
            cur = (float(row["lat"]), float(row["lng"]))
        if reorder and len(day) >= 2:
            day = reordered(day_start, day, day_minutes)
            cur = (day[-1]["lat"], day[-1]["lng"])
        plan.append(day)
        nxt = day_end(d, day) if day_end and day else None
        day_start = start if home else (nxt or cur)
    return plan, indoor_short


def edit_diff(edit: dict, prev: list[dict], plan: list[list[dict]], label: str, dataset: str, companions: int,
              per_day: int, kept: list[int] | None = None, day_minutes: int | None = None) -> tuple[EvidenceTable, Finding]:
    """수정 대화: 직전 코스랑 새 코스 날짜별 비교표(C6) + 무엇을 바꿨는지 첫 문장
    바뀐 날은 'N일차'로 적음 ('바뀐 날은 2일'이 2일차로 안 읽히게)
    """
    rows, changed, changed_days = [], 0, []
    days = max([len(plan)] + [p["day"] for p in prev])
    for d in range(1, days + 1):
        before = [p["name"] for p in prev if p["day"] == d]
        after = [p["name"] for p in (plan[d - 1] if d <= len(plan) else [])]
        out = [n for n in before if n not in after]
        add = [n for n in after if n not in before]
        what = " / ".join(x for x in ((f"뺌: {', '.join(out)}" if out else ""), (f"더함: {', '.join(add)}" if add else "")) if x)
        if before != after:
            changed += 1
            changed_days.append(d)
        rows.append([f"{d}일차", " → ".join(before) or "-", " → ".join(after) or "-", what or ("순서만 바뀜" if before != after else "그대로")])
    t = EvidenceTable(id="C6", title="직전 코스와 바뀐 점", dataset=dataset, data_label=label,  # type: ignore[arg-type]
                      columns=["일차", "고치기 전", "고친 뒤", "바뀐 것"], rows=rows,
                      note=("고친 날 말고 다른 날은 장소를 그대로 두고 이동 시간만 다시 쟀습니다." if kept else
                            "한 날을 바꾸면 다음 날 출발점(전날 마지막 장소)도 바뀌어 뒤 일정이 함께 바뀔 수 있습니다."))
    k = edit["kind"]
    if k == "exclude":
        text = (f"말씀하신 대로 {josa(edit['name'], '을')} 빼고 다시 짰습니다." if edit.get("in_course") else
                f"{josa(edit['name'], '은')} 직전 코스에 없던 곳이라 그대로 두고, 이번 대화에서는 넣지 않겠습니다.")
        nums: dict[str, float] = {}
    elif k == "include":
        back = any(p["place_id"] == edit["place_id"] for day in plan for p in day)
        text = (f"말씀하신 대로 {josa(edit['name'], '을')} 다시 넣었습니다." if back else
                f"{josa(edit['name'], '을')} 뺀 목록에서 지웠지만, 이동 시간·하루 장소 수 안에서 이번 코스에는 들어가지 않았습니다.")
        nums = {}
    elif k == "lighter":
        b = sum(1 for p in prev if p["day"] == edit["day"])
        a = len(plan[edit["day"] - 1]) if edit["day"] <= len(plan) else 0
        text = (f"{edit['day']}일차를 {b}곳에서 {a}곳으로 줄였습니다." if a < b else
                f"{edit['day']}일차는 이미 {b}곳이라 더 줄이지 않았습니다. 더 쉬려면 일수를 늘리거나 장소를 빼 주세요.")
        nums = {"day": edit["day"], "before": b, "after": a}
    elif k == "day_count":
        b = edit.get("before", sum(1 for p in prev if p["day"] == edit["day"]))
        a = len(plan[edit["day"] - 1]) if edit["day"] <= len(plan) else 0
        if a == edit["n"]:
            text = (f"{edit['day']}일차를 {b}곳에서 {a}곳으로 바꿨습니다." if a != b else f"{edit['day']}일차는 이미 {a}곳입니다.")
        else:
            text = (f"{edit['day']}일차를 {edit['n']}곳으로 하려 했지만, 하루 시간({day_minutes}분)과 이동 시간 상한 안에 "
                    f"{a}곳만 들어갔습니다. 더 넣으려면 그날 장소를 빼거나 일수를 늘려 주세요.")
        nums = {"day": edit["day"], "before": b, "after": a, "want": edit["n"], "day_minutes": day_minutes or 0}
    elif k == "day_swap":
        a = len(plan[edit["day"] - 1]) if edit["day"] <= len(plan) else 0
        gone = ", ".join(edit.get("names") or [])
        text = (f"{edit['day']}일차를 다른 곳으로 바꿨습니다" + (f"(뺀 곳: {gone})." if gone else ".") if a else
                f"{edit['day']}일차의 장소를 뺐지만, 조건 안에서 대신 갈 곳을 찾지 못했습니다. 지역을 넓히거나 이동 시간 상한을 늘려 보세요.")
        nums = {"day": edit["day"], "after": a}
    elif k == "lighter_all":
        text = f"모든 날을 '여유' 속도(하루 최대 {per_day}곳)로 바꿔 다시 짰습니다."
        nums = {"per_day": per_day}
    else:   # join
        text = (f"{edit['who']}까지 함께 가는 조건(일행 {companions}명)으로 다시 짰습니다. "
                "함께 가시는 분께 필요한 조건(예: 오래 걷기 힘듦, 휠체어 이용)이 있으면 말씀해 주세요. 그 조건까지 맞춰 다시 짜겠습니다.")
        if companions - edit["n"] < 2 <= companions:
            text += " 동행인이 생겨, 조사 자료에 '보호자·동행인 동반 시 추천'으로 적힌 곳도 갈 만한 곳으로 봤습니다."
        nums = {"companions": companions}
    if changed:
        text += f" 바뀐 날: {', '.join(f'{d}일차' for d in changed_days)}."
        if kept:
            text += " 다른 날은 그대로 두었습니다."
    else:
        text += " 일정은 그대로입니다."
    return t, Finding(text=text, evidence_ids=["C6"], numbers={**nums, "changed": changed,
                                                                **{f"changed_day_{d}": d for d in changed_days}})


def edit_examples(plan: list[list[dict]], placed: list[dict]) -> list[str]:
    """코스 아래 '이어서 물어보기' 예시 버튼"""
    out = []
    busy = next((d for d, day in enumerate(plan, 1) if len(day) > 1 and d > 1), None)
    if busy:
        out.append(f"{busy}일차 빡빡해")
    elif plan and len(plan[0]) > 1:
        out.append("너무 빡빡해")
    if len(placed) > 1:
        out.append(f"{placed[-1]['name']} 빼줘")
    if placed:
        out.append(f"{placed[0]['name']} 화장실 있어?")
    return out


def why_lines(p: dict, order: int, card: dict, profile: TravelerProfile, indoor_per_day: int, risky: bool,
              caution_why: str) -> list[str]:
    """장소마다 왜 골랐는지. 문장은 코드가 만들고 LLM은 설명만 함"""
    need = [f["label"] for f in card.get("facilities", []) if f.get("needed") and f.get("state") == "있음"]
    if p["verdict"] == "주의":
        out = [f"{caution_why} 넣은 '주의' 장소" + (f" — 확인할 점: {p['reasons'][0]}" if p["reasons"] else "")]
    else:
        out = [f"필요한 시설 확인({'·'.join(need)})" if need else f"조건에 맞음(접근성 점수 {p['score']}점)"]
    if p.get("indoor") == 1 and indoor_per_day:
        out.append("실내 장소(" + ("비 오는 날 대비" if risky else "실내 선호") + ")")
    hit = [INTEREST_KO[i] for i in profile.interests if p.get("category") in INTEREST_CATEGORIES[i]]
    if hit:
        out.append(f"관심사({'·'.join(hit)})에 맞음")
    out.append(f"{'출발점' if order == 1 else '앞 장소'}에서 약 {p['leg_min']}분({p.get('leg_src', '추정')})")
    return out


def route_guide(card: dict, pts: pd.DataFrame | None) -> dict[str, str]:
    """경로 설명(접근로·주차·화장실 위치). 정리본 글 → 조사 지점 기록 → 없으면 '정보 없음'"""
    g = pts[pts["place_id"] == card.get("place_id")] if pts is not None and "place_id" in pts else pd.DataFrame()

    def spots(flag: str, word: str) -> str:
        if g.empty:
            return ""
        hit = g[(pd.to_numeric(g.get(flag), errors="coerce") == 1) | g["point_name"].fillna("").astype(str).str.contains(word)]
        out: list[str] = []
        for n, d in zip(hit["point_name"], hit["detail"]):
            n = "" if pd.isna(n) else str(n).strip()
            d = "" if pd.isna(d) else str(d).replace("특이사항없음", "").strip(" /")
            t = (d if word in d or not n else f"{n}({d})") if d else n    # '출발 / 도착'처럼 글만으론 모르는 건 지점 이름 붙임
            if t and t not in out:
                out.append(t)
        return " / ".join(out[:2])

    path = card.get("route") or (" → ".join(list(dict.fromkeys(str(x).strip() for x in g["point_name"].dropna()))[:6])
                                 if not g.empty else "")
    toilet = card.get("toilet_location") or spots("toilet", "화장실")
    if not toilet and card.get("alt_toilet"):
        toilet = f"주변 화장실: {card['alt_toilet']} (장애인용인지 방문 전 확인)"
    return {"route": path or "정보 없음", "parking": card.get("parking_location") or spots("parking", "주차") or "정보 없음",
            "toilet": toilet or "정보 없음"}


def extra_pool(ctx: AgentContext, profile: TravelerProfile, rules: dict,
               with_kto: bool = True) -> tuple[list[dict], dict[str, tuple[float, float]]]:
    """코스 식당·숙소 후보: 무장애 코스 주변정보 + (with_kto) 관광공사 식당. 같은 가게는 하나만
    가게 좌표는 없어서 코스 짤 때 LiveXY로 실시간으로 붙임 (카카오 응답 저장 금지)
    """
    nd = ctx.repo.nearby_df()
    places = ctx.repo.get("places").df.set_index("place_id")
    cards, xy = [], {}
    for pid in nd["place_id"].unique():
        if pid not in places.index:
            continue
        row = places.loc[pid]
        if pd.isna(row.get("lat")) or pd.isna(row.get("lng")):
            continue
        xy[pid] = (float(row["lat"]), float(row["lng"]))
        cards.append({"place_id": pid, "name": row["name"], "verdict": "적합", "reasons": [], "facilities": [], "category": None,
                      "source": NEARBY_SOURCE, "data_date": None, "stale": False, "call_script": [],
                      "nearby": nearby_rows(nd, pid, ["restaurant", "lodging"])})   # 개수 제한 없음 (식당이 카페에 밀리면 안 됨)
    items = [i for i in place_items(cards, xy, profile, rules) if i["kind"] != "attraction"]
    names = {c["place_id"]: c["name"] for c in cards}
    addr = {f"{c['place_id']}-N{n.get('seq', 0)}": n.get("address") for c in cards for n in c["nearby"]}
    for i in items:
        i["survey_anchor"] = names.get(i["anchor_id"])
        i["address"] = addr.get(i["item_id"]) or None   # 실시간 좌표 찾기용 (화면엔 안 씀)
    if with_kto:
        items = merge_kto(items, kto_items(ctx.repo.kto_df(), profile, rules), profile)
    return items, xy


SAME_SHOP_KM = 0.3   # 이름 같고 이 거리 안이면 같은 가게


def _same_shop(a: dict, b: dict) -> bool:
    x, y = kto_data.norm_name(a["name"]), kto_data.norm_name(b["name"])
    if not (x == y or (min(len(x), len(y)) >= 3 and (x in y or y in x))):
        return False
    ax, bx = _xy(a), _xy(b)
    return ax is None or bx is None or _hav(ax, bx) <= SAME_SHOP_KM


def merge_kto(items: list[dict], kto: list[dict], profile: TravelerProfile) -> list[dict]:
    """주변정보 + 관광공사 식당 합침. 같은 가게(이름 같고 300m 안)는 관광공사 쪽으로 (더 최근이고 출입 문장 있음)
    전화는 주변정보 것 씀. 관광공사 화장실이 빈칸인데 주변정보가 '없음'이면 '없음'으로 둠
    """
    need_toilet = "acc_toilet" in needed_facilities(profile)
    out_kto = []
    dropped: set[int] = set()
    for k in kto:
        same = [n for n, i in enumerate(items) if i["kind"] == "restaurant" and _same_shop(i, k)]
        if same:
            dropped.update(same)
            old = items[same[0]]
            k = {**k, "phone": k.get("phone") or old.get("phone"), "also_in": old["source"]}
            t_old, t_new = old["facilities"][0], k["facilities"][0]
            if t_new["state"] == "정보 없음" and t_old["state"] == "없음":
                k["facilities"] = [{**t_new, "state": "없음", "mark": "✗", "evidence": "2021-12-22 데이터허브 조사"}] + k["facilities"][1:]
                if need_toilet:
                    k = {**k, "verdict": "부적합", "reasons": k["reasons"] + ["장애인 화장실이 없다고 조사됨(2021)"]}
        out_kto.append(k)
    return [i for n, i in enumerate(items) if n not in dropped] + out_kto


def shop_rank(i: dict, need_toilet: bool) -> tuple[int, int]:
    """식당·숙소 고르는 순서(작을수록 먼저): 화장실 필요하면 '있음' 먼저 → '주의' 아닌 곳 → 거리"""
    toilet = (i.get("facilities") or [{}])[0].get("state")
    return (0 if not need_toilet or toilet == "있음" else 1, 1 if i["verdict"] == "주의" else 0)


def shop_basis(x: dict) -> str:
    """식당·숙소 출처 짧은 말: '주변정보 2021 조사' / '관광공사 2026-11 수정'"""
    if x.get("source") == NEARBY_SOURCE:
        return "데이터허브 주변정보(2021 조사)"
    return f"한국관광공사({x['data_date'][:7]} 수정)" if x.get("data_date") else "한국관광공사"


def shop_entry(x: dict) -> str:
    """출입 칸: 관광공사는 문장 보고 나눔(가능·주의·정보 없음), 주변정보는 '휠체어 진입 가능(조사)'"""
    e = next((f for f in x.get("facilities") or [] if f.get("key") == "entry"), None)
    if e is None:
        return "휠체어 진입 가능(조사)" if x.get("source") == NEARBY_SOURCE else "정보 없음"
    return e["state"]


def _hav(a: tuple[float, float], b: tuple[float, float]) -> float:
    return float(haversine_km(a[0], a[1], b[0], b[1]))


def _xy(i: dict) -> tuple[float, float] | None:
    return (float(i["lat"]), float(i["lng"])) if i.get("lat") is not None and i.get("lng") is not None else None


def extra_km(i: dict, s: dict, anchor_xy: dict[str, tuple[float, float]]) -> tuple[float, bool] | None:
    """코스 장소 s → 가게 i 거리(km), 추정 여부
    가게 좌표 있으면 직선거리, 없으면 기준 코스 거쳐서 추정. 둘 다 안 되면 None
    """
    sxy = _xy(s)
    if (ixy := _xy(i)) is not None and sxy is not None:
        return _hav(sxy, ixy), False
    if i.get("anchor_id") == s["place_id"]:
        return float(i.get("distance_km") or 0), True
    if i.get("anchor_id") in anchor_xy and sxy is not None:
        return _hav(sxy, anchor_xy[i["anchor_id"]]) + float(i.get("distance_km") or 0), True
    return None


def nearest_lodging(items: list[dict], last: dict, used: set[tuple], max_km: float,
                    live: LiveXY | None = None, anchor_xy: dict[str, tuple[float, float]] | None = None) -> dict | None:
    """그날 마지막 장소에서 제일 가까운 숙소(좌표 있는 곳). 다음 날 출발점으로 씀
    live 있으면 추정 거리 가까운 숙소만 먼저 실시간으로 좌표 찾음
    """
    if live is not None:
        live.fill(items, lambda i: i["kind"] == "lodging" and i["verdict"] != "부적합" and (i["name"], i.get("phone")) not in used
                  and (e := extra_km(i, last, anchor_xy or {})) is not None and e[0] <= max_km + 2, anchor_xy or {})
    best, best_km = None, None
    for i in items:
        if i["kind"] != "lodging" or i["verdict"] == "부적합" or (i["name"], i.get("phone")) in used or _xy(i) is None:
            continue
        km = _hav((last["lat"], last["lng"]), _xy(i))  # type: ignore[arg-type]
        if km <= max_km and (best_km is None or km < best_km):
            best, best_km = i, km
    return {**best, "distance_km": round(best_km, 1), "estimated": False} if best else None


def place_extras(plan: list[list[dict]], items: list[dict], home: bool,
                 want: list[str] | None = None, anchor_xy: dict[str, tuple[float, float]] | None = None,
                 max_km: float = 6.0, choice: list[str] | None = None,
                 lodging_fixed: dict[int, dict] | None = None,
                 need_toilet: bool = False,
                 toilet_slack_km: float | None = None,
                 live: LiveXY | None = None) -> tuple[list[dict], dict[str, list[int]]]:
    """식당·숙소를 코스에 넣음. 반환: (넣은 것, {"점심": 못 찾은 날, …})

    - 점심: 그날 가운데 장소 뒤 (덜 돌아가는 곳), 저녁: 말했거나 골랐을 때만, 숙소: 하루 끝 (마지막 날 빼고)
    - 부적합은 안 넣고, 화장실 필요하면 '있음'인 곳 먼저 (toilet_slack_km 넘게 멀면 가까운 쪽)
    - detour_km: 그 식당 때문에 더 가는 거리 → 코스가 하루 시간에 넣음
    - choice: 화면에서 고른 것, want: 말로 부탁한 것 (choice 상관없이 넣음)
    """
    want = want or []
    picked = ["lunch", "lodging"] if choice is None else choice
    lunch_on, dinner_on = "lunch" in picked or "lunch" in want, "dinner" in picked or "dinner" in want
    lodging_on = "lodging" in picked or "lodging" in want
    anchor_xy = anchor_xy or {}
    lodging_fixed = lodging_fixed or {}
    ok = [i for i in items if i["verdict"] != "부적합"]
    used: set[tuple] = {(x["name"], x.get("phone")) for x in lodging_fixed.values()}
    out: list[dict] = []
    miss: dict[str, list[int]] = {"점심": [], "저녁": [], "숙소": []}

    def pick(kind: str, stops: list[dict], day: list[dict], lunch: bool = False) -> tuple[dict | None, dict | None]:
        for s in stops:
            k = day.index(s)
            nxt = day[k + 1] if lunch and k + 1 < len(day) else None
            if live is not None:
                live.fill(ok, lambda i: i["kind"] == kind and (i["name"], i.get("phone")) not in used
                          and (e := extra_km(i, s, anchor_xy)) is not None and e[0] <= max_km + 2, anchor_xy)
            cands = []   # (순위, 비용, 가게, 거리, 추정 여부, 우회 거리)
            for i in ok:
                if i["kind"] != kind or (i["name"], i.get("phone")) in used:
                    continue
                got = extra_km(i, s, anchor_xy)
                if got is None or got[0] > max_km:
                    continue
                km, est = got
                if not lunch:
                    detour = km
                elif nxt is not None and not est and (ixy := _xy(i)) is not None and _xy(nxt) is not None:
                    detour = max(0.0, km + _hav(ixy, (nxt["lat"], nxt["lng"])) - _hav((s["lat"], s["lng"]), (nxt["lat"], nxt["lng"])))
                else:
                    detour = 2 * km
                cands.append((shop_rank(i, need_toilet), detour if lunch else km, i, km, est, detour))
            if cands:
                best = min(cands, key=lambda c: (c[0], c[1]))
                if need_toilet and toilet_slack_km is not None and best[0][0] == 0:
                    # 화장실 있는 식당이 너무 멀면(상한 넘게 더 돌면) 가까운 쪽
                    rest = [c for c in cands if c[0][0] == 1]
                    alt = min(rest, key=lambda c: (c[0], c[1])) if rest else None
                    if alt is not None and best[1] - alt[1] > toilet_slack_km:
                        best = alt
                _, _, i, km, est, detour = best
                used.add((i["name"], i.get("phone")))
                return {**i, "distance_km": round(km, 1), "estimated": est, "detour_km": round(detour, 1)}, s
        return None, None

    for d, day in enumerate(plan, 1):
        if not day:
            continue
        mid = (len(day) + 1) // 2 - 1
        if lunch_on:
            i, s = pick("restaurant", [day[mid]] + day[:mid] + day[mid + 1:], day, lunch=True)
            if i and s:
                out.append({**i, "day": d, "after": day.index(s) + 1, "slot": "점심", "anchor_name": s["name"]})
            else:
                miss["점심"].append(d)
        if dinner_on:
            i, s = pick("restaurant", day[::-1], day)
            if i and s:
                out.append({**i, "day": d, "after": len(day), "slot": "저녁", "anchor_name": s["name"]})
            else:
                miss["저녁"].append(d)
        if lodging_on and not home and (d < len(plan) or "lodging" in want):
            if d in lodging_fixed:
                i, s = {**lodging_fixed[d], "detour_km": 0.0}, day[-1]
            else:
                i, s = pick("lodging", day[::-1], day)
                if i:
                    i["detour_km"] = 0.0   # 하루 끝에 가는 곳이라 하루 시간에 안 넣음
            if i and s:
                out.append({**i, "day": d, "after": len(day), "slot": "숙소", "anchor_name": s["name"]})
            else:
                miss["숙소"].append(d)
    return out, miss


def course_issues(*, toilet_missing: list[int], toilet_per_day: int, short: list[int], max_leg: int, cap: int | None,
                  risky: bool, indoor_per_day: int, indoor_short: list[int], meal_miss: list[int]) -> list[dict]:
    """코스 점검. 문제마다 {code, days, text, fix}. fix = 다시 짤 때 바꿀 조건, 고칠 게 없으면 None"""
    out: list[dict] = []
    if toilet_missing:
        out.append({"code": "toilet_day", "days": toilet_missing, "text": "장애인 화장실 확인 장소가 없는 날",
                    "fix": None if toilet_per_day else {"toilet_per_day": 1}})
    if short:
        longer = [m for m in LEG_OPTIONS if m > max_leg and (cap is None or m <= cap)]
        out.append({"code": "short_day", "days": short, "text": "장소가 모자란 날",
                    "fix": {"max_leg_min": longer[0]} if longer else None})
    if risky and not indoor_per_day:
        out.append({"code": "rain_no_indoor", "days": [], "text": "비·강풍 위험이 높은데 실내 장소를 넣지 않음",
                    "fix": {"indoor_per_day": 1}})
    elif indoor_short:
        out.append({"code": "indoor_short", "days": indoor_short, "text": "실내 장소를 다 못 채운 날", "fix": None})
    if meal_miss:
        out.append({"code": "meal_miss", "days": meal_miss, "text": "근처 식당을 못 넣은 날", "fix": None})
    return out


def fix_args(payload: dict) -> dict:
    """고칠 수 있는 문제들 조건 합침"""
    out: dict = {}
    for i in payload.get("issues") or []:
        out.update(i.get("fix") or {})
    return out


def issue_days(payload: dict) -> int:
    """문제 크기(문제 있는 날 수 합). 작을수록 좋은 코스"""
    return sum(len(i["days"]) or 1 for i in payload.get("issues") or [])


class CourseAgent(BaseAgent):
    name = "course"
    title = "여행 코스 설계"
    description = ("접근성 진단 에이전트가 고른 장소로 날짜별 코스를 짠다. 이용자 조건에 맞는 속도(여유·보통), 여행 달의 날씨 위험에 따른 "
                   "실내 장소 수, 장소 사이 이동 시간 상한을 정하고, 화장실·입장료·버스 정류소를 점검한다.")
    required = ("plan",)

    def _run(self, req: AnalysisRequest, ctx: AgentContext) -> AgentResult:
        """규칙 판단: 기본 조건으로 짜고, 고칠 게 있으면 한 번 더 짜서 문제 적은 쪽 씀"""
        opts = req.params.get("opts", {})
        r = self.plan(req, ctx, pace=opts.get("pace"), indoor_per_day=opts.get("indoor_per_day"),
                      max_leg_min=opts.get("max_leg_min"), toilet_per_day=opts.get("toilet_per_day"))
        fix = fix_args(r.payload)
        if opts or not fix or r.status not in ("ok", "partial"):
            return r
        self.step("rule:recheck", "점검: " + ", ".join(i["text"] for i in r.payload["issues"] if i["fix"])
                  + " → " + ", ".join(f"{k}={v}" for k, v in fix.items()) + "로 한 번 다시")
        r2 = self.plan(req, ctx, pace=None, indoor_per_day=fix.get("indoor_per_day", r.payload.get("indoor_per_day")),
                       max_leg_min=fix.get("max_leg_min"), toilet_per_day=fix.get("toilet_per_day"))
        better = r2.status in ("ok", "partial") and issue_days(r2.payload) < issue_days(r.payload)
        self.step("rule:recheck_pick", "다시 짠 코스가 점검 문제가 적어 다시 짠 코스를 씀" if better else
                  "다시 짜도 점검 문제가 줄지 않아 처음 코스를 씀")
        return r2 if better else r

    # ── 날씨 ──
    def weather(self, req: AnalysisRequest, ctx: AgentContext, places: pd.DataFrame) -> tuple[pd.DataFrame | None, str]:
        """(관광지별 비 온 날 비율, 상태). 상태: ok | no_month | no_data | no_month_data"""
        if req.month is None:
            return None, "no_month"
        if not (uses(req, "c5_01") and uses(req, "c5_02")):
            return None, "not_selected"
        if not (ctx.repo.has("weather_daily") and ctx.repo.has("weather_stations")):
            return None, "no_data"
        stats = weather_month_stats(ctx.repo.get("weather_daily").df, req.month, access_rules()["weather"])
        if stats.empty:
            return None, "no_month_data"
        pw = place_weather(places, ctx.repo.get("weather_stations").df, stats)
        self.step("weather", f"{req.month}월 관측소 {len(stats)}곳 집계")
        return pw, "ok"

    # ── 계산 ──
    def plan(self, req: AnalysisRequest, ctx: AgentContext, pace: str | None = None, indoor_per_day: int | None = None,
             max_leg_min: int | None = None, toilet_per_day: int | None = None) -> AgentResult:
        rules = access_rules()
        cr, wr = rules["course"], rules["weather"]
        acc = ctx.ask("access", req.model_copy(update={"params": {k: v for k, v in req.params.items() if k != "opts"}}))
        if acc.status not in ("ok", "partial") or not acc.payload.get("verdicts"):
            return AgentResult(agent=self.name, status="insufficient", confidence="low", summary=acc.summary,
                               warnings=acc.warnings + [AgentWarning(code="NO_CANDIDATES", message="코스에 넣을 장소가 없습니다.", severity="block")])
        v = pd.DataFrame(acc.payload["verdicts"])
        v = v[v["verdict"] != "부적합"].dropna(subset=["lat", "lng"])
        edits = req.params.get("edits") or {}
        want = list(req.params.get("want_extras") or [])   # 이용자가 말한 식당·숙소 (저녁·마지막 날 숙소)
        excluded = list(edits.get("exclude", []))
        if excluded:   # 수정 대화에서 뺀 장소는 다시 안 넣음
            v = v[~v["place_id"].isin(excluded)]
            self.step("edit:exclude", f"이용자가 뺀 장소 {len(excluded)}곳 제외")
        day_caps = {int(k): int(n) for k, n in (edits.get("day_caps") or {}).items()}
        days = min(req.days or 1, cr["max_days"])
        fixed = {int(d): ids for d, ids in (req.params.get("keep_days") or {}).items() if int(d) <= days}
        if fixed:
            self.step("edit:keep_days", f"고치지 않는 날 {len(fixed)}일은 그대로")
        relaxed = forced_relaxed(req.profile, rules)
        pace = "여유" if relaxed or edits.get("pace") == "여유" else (pace or "보통")
        pc = cr["pace"][pace]
        per_day, visit_min, day_minutes = pc["per_day"], pc["visit_min"], pc["day_minutes"]
        need = days * per_day
        n_ok = int((v["verdict"] == "적합").sum())
        use_caution = n_ok < need
        cands = v if use_caution else v[v["verdict"] == "적합"]
        kept_ids = [pid for ids in fixed.values() for pid in ids]
        if kept_ids:   # 그대로 둘 날의 '주의' 장소도 후보에 있어야 다시 놓을 수 있음
            cands = pd.concat([cands, v[v["place_id"].isin(kept_ids) & ~v["place_id"].isin(cands["place_id"])]])

        pw, wstate = self.weather(req, ctx, v)     # 주의 장소를 나중에 넣어도 맞게 후보 전체로 계산
        rain = wind = hot = None
        if pw is not None and not pw.empty:
            rain, wind, hot = (round(float(pw[c].mean()), 2) for c in ("rain_ratio", "wind_ratio", "hot_ratio"))
        risky = rain is not None and (rain >= wr["rain_ratio_high"] or wind >= wr["wind_ratio_high"])
        if indoor_per_day is None:
            indoor_per_day = 1 if (risky or "indoor_pref" in req.profile.needs) else 0
            if indoor_per_day:
                self.step("rule:indoor", "날씨 위험·실내 선호 → 하루 1곳 실내")
        cap = req.profile.max_ride_min                  # 이용자가 정한 '한 번에 이동할 수 있는 시간' (안 넘음)
        max_leg = max_leg_min or cr["default_max_leg_min"]
        if cap is not None:
            max_leg = min(max_leg, cap)
        transport = req.profile.transport
        key = ctx.settings.kakao_rest_key
        road = (lambda a, b: road_min(a, b, key)) if key and transport != "bus" else None   # 버스는 항상 추정치
        airport =((cr["start"]["lat"], cr["start"]["lng"]), cr["start"]["name"])
        origin_xy = region_center(req.origin) if req.origin else None
        if req.origin and origin_xy:
            start, start_name = origin_xy, f"{req.origin}(출발지)"
        elif req.origin == cr["start"]["name"]:
            start, start_name = airport
        elif region_center(req.region):
            start, start_name = region_center(req.region), f"{req.region} 중심"  # type: ignore[assignment]
        else:
            start, start_name = airport
        if relaxed:
            self.step("rule:pace", "여유가 필요한 조건 → 여유")
        home = bool(req.origin and origin_xy)    # origin = 집·숙소 → 매일 거기서 출발
        # 식당·숙소 후보: 판정은 장소 찾기가 했고 코스는 자리만 정함
        # 가게 좌표 있으면 숙소를 짜는 중에 골라서 다음 날 거기서 출발, 식당은 덜 돌아가는 곳
        # 카카오 좌표는 이번 코스에서만 쓰고 저장 안 함
        pick = req.params.get("extras_pick")   # 화면에서 고른 식당·숙소 (None = 점심·숙소)
        extras_on = not (pick is not None and not pick and not want) and uses(req, "bf_nearby")
        items, anchor_xy = extra_pool(ctx, req.profile, rules, with_kto=uses(req, "kto_bf")) if extras_on else ([], {})
        live = LiveXY(key) if extras_on and key else None
        # 화장실 점검(C2)이랑 같은 기준: 화장실 필요 or 휠체어 → 식당도 화장실 있는 곳 먼저
        need_toilet = "acc_toilet" in req.profile.needs or req.profile.mobility.startswith("wheelchair")
        max_km = float((cr.get("extras_max_km") or {}).get(transport, 6))
        lodging_on = extras_on and not home and ("lodging" in (["lunch", "lodging"] if pick is None else pick) or "lodging" in want)
        lodging_fixed: dict[int, dict] = {}

        def lodge(d: int, day: list[dict]) -> tuple[float, float] | None:
            """그날 숙소 고르고 다음 날 출발점으로 돌려줌. 없으면 None (전날 마지막 장소에서 출발)"""
            if not lodging_on or (d == days and "lodging" not in want):
                return None
            got = nearest_lodging(items, day[-1], {(x["name"], x.get("phone")) for x in lodging_fixed.values()}, max_km,
                                  live, anchor_xy)
            if got is None:
                return None
            lodging_fixed[d] = got
            return (float(got["lat"]), float(got["lng"])) if d < days else None

        def run(caps: dict[int, int]) -> tuple[list[list[dict]], list[int]]:
            lodging_fixed.clear()
            return schedule(cands, days, per_day, visit_min, day_minutes, max_leg, indoor_per_day, transport, rules, start,
                            cap, home, caps, road, fixed, day_end=lodge, toilet_per_day=toilet_per_day or 0)

        plan, indoor_short = run(day_caps)
        short = [i + 1 for i, d in enumerate(plan) if len(d) < day_caps.get(i + 1, per_day)]
        if short and max_leg_min is None and max_leg < max(LEG_OPTIONS) and (cap is None or cap >= max(LEG_OPTIONS)):
            # 규칙: 장소 모자란 날 있으면 이동 상한 한 번 늘려서 다시
            self.step("rule:extend_leg", f"장소가 모자란 날 {len(short)}일 → 이동 상한 {max(LEG_OPTIONS)}분")
            max_leg = max(LEG_OPTIONS)
            plan, indoor_short = run(day_caps)
            short = [i + 1 for i, d in enumerate(plan) if len(d) < day_caps.get(i + 1, per_day)]
        caution_why = f"적합 장소가 {n_ok}곳이라"
        if short and not use_caution:
            # 규칙: 적합 장소 모자라면 '주의'(동행·방문 전 확인)까지 넣어서 다시. 부적합은 끝까지 뺌
            self.step("rule:include_caution", f"장소가 모자란 날 {len(short)}일 → 주의 장소 포함")
            use_caution, cands, caution_why = True, v, "이동 시간 안의 적합 장소가 모자라"
            plan, indoor_short = run(day_caps)
            short = [i + 1 for i, d in enumerate(plan) if len(d) < day_caps.get(i + 1, per_day)]
        if indoor_per_day and indoor_short and not use_caution:
            # 규칙: 실내 '적합'이 모자라면(박물관·미술관은 대부분 '주의') 실내 '주의'만 더해서 다시
            # 실외 칸은 그대로 적합에서 고름
            indoor_c = v[(v["verdict"] == "주의") & (pd.to_numeric(v["indoor"], errors="coerce") == 1)
                         & ~v["place_id"].isin(cands["place_id"])] if "indoor" in v else v.iloc[0:0]
            if not indoor_c.empty:
                self.step("rule:include_caution_indoor", f"실내 장소가 모자란 날 {len(indoor_short)}일 → 실내 '주의' 장소 {len(indoor_c)}곳을 후보에 더함")
                use_caution, cands, caution_why = True, pd.concat([cands, indoor_c]), "실내 '적합' 장소가 모자라"
                plan, indoor_short = run(day_caps)
                short = [i + 1 for i, d in enumerate(plan) if len(d) < day_caps.get(i + 1, per_day)]
        extras: list[dict] = []
        miss: dict[str, list[int]] = {"점심": [], "저녁": [], "숙소": []}
        food_blocked = False   # 식당 후보가 전부 화장실 조건 때문에 빠졌는지 (화면에 이유 보여 줌)
        speed = cr["speed_kmh"].get(transport, cr["speed_kmh"]["car"])
        # 화장실 있는 식당 먼저, 근데 이 시간 넘게 더 돌면 가까운 쪽 (분 → km)
        toilet_slack_km = float(cr.get("toilet_first_max_extra_min", 15)) * speed / 60 / cr["road_factor"]

        def meal_min(x: dict) -> int:   # 식당 때문에 더 가는 이동(분). 식사 시간은 안 넣음 (장소 수가 너무 줄어서)
            return 0 if x["slot"] == "숙소" else int(round(float(x.get("detour_km") or 0) * cr["road_factor"] / speed * 60))

        if extras_on:
            extras, miss = place_extras(plan, items, home, want, anchor_xy, max_km, choice=pick, lodging_fixed=dict(lodging_fixed),
                                        need_toilet=need_toilet, toilet_slack_km=toilet_slack_km, live=live)
            # 식당까지 이동을 하루 시간에 넣음. 넘치는 날만 1곳 줄여서 한 번 더
            # 하루 2곳 밑으로는 안 줄이고, 수정 대화로 정한 날·고정한 날은 안 건드림
            trim = {}
            for d, day in enumerate(plan, 1):
                meal = sum(meal_min(x) for x in extras if x["day"] == d)
                if (meal and len(day) > MIN_PER_DAY and d not in day_caps and d not in (fixed or {})
                        and sum(p["leg_min"] for p in day) + visit_min * len(day) + meal > day_minutes):
                    trim[d] = len(day) - 1
            if trim:
                self.step("rule:meal_time", f"식당까지 이동을 넣으니 하루 시간을 넘는 날 {len(trim)}일 → 그날 1곳 줄임"
                                            f"(하루 {MIN_PER_DAY}곳 아래로는 줄이지 않음)")
                plan, indoor_short = run({**day_caps, **trim})
                extras, miss = place_extras(plan, items, home, want, anchor_xy, max_km, choice=pick, lodging_fixed=dict(lodging_fixed),
                                        need_toilet=need_toilet, toilet_slack_km=toilet_slack_km, live=live)
                short = [i + 1 for i, d in enumerate(plan) if len(d) < {**day_caps, **trim}.get(i + 1, per_day)]
            for x in extras:
                x["meal_min"] = meal_min(x)
            if live is not None and live.calls:
                self.step("tool:kakao_address", f"가게 주소 {live.calls}곳을 카카오 주소 검색으로 실시간 확인(좌표 {live.found}곳, 저장하지 않음)"
                                                + (" — 오류로 중단, 나머지는 추정 거리" if live.down else ""))
        placed = [p for d in plan for p in d]
        if road is not None:   # 지도 경로선: 길찾기 구간은 같은 응답(캐시)의 도로 좌표. 하루 첫 이동은 안 그림
            for day in plan:
                for a, b in zip(day, day[1:]):
                    if b["leg_src"] == "길찾기":
                        b["path"] = road_path((a["lat"], a["lng"]), (b["lat"], b["lng"]), key)
        self.step("schedule", f"{days}일 · 하루 최대 {per_day}곳 · 이동 상한 {max_leg}분 → {len(placed)}곳 배치"
                  + (f" (장소가 모자란 날 {len(short)}일)" if short else ""))
        if not placed:
            return AgentResult(agent=self.name, status="insufficient", confidence="low",
                               summary="조건에 맞는 장소 중 이동 시간 안에 갈 수 있는 곳이 없어 코스를 만들지 못했습니다.",
                               warnings=[AgentWarning(code="NO_ROUTE", message="지역을 넓히거나 이동 시간 상한을 늘려 보세요.", severity="block")],
                               follow_ups=["제주 전체에서 코스 짜줘"])

        label = ctx.repo.get("places").label
        dataset = ctx.repo.get("places").source
        cards_by = {c["place_id"]: c for c in acc.payload.get("cards", [])}
        pts = ctx.repo.get("place_points").df if ctx.repo.has("place_points") else None
        c1 = []
        for di, day in enumerate(plan, 1):
            for k, p in enumerate(day, 1):
                p["why"] = why_lines(p, k, cards_by.get(p["place_id"], {}), req.profile, indoor_per_day, risky, caution_why)
                p["guide"] = route_guide(cards_by.get(p["place_id"], {"place_id": p["place_id"]}), pts)
                c1.append([f"{di}일차", k, p["name"], p["verdict"], p["leg_min"], p["leg_src"],
                           {1.0: "실내", 0.0: "실외"}.get(p["indoor"], "모름"),
                           {1.0: "있음", 0.0: "없음"}.get(p["acc_toilet"], "정보 없음"), " · ".join(p["why"])])
        srcs = {p["leg_src"] for p in placed}
        road_txt = ("카카오 길찾기(자동차) 기준" if srcs == {"길찾기"} else
                    "카카오 길찾기(자동차) 기준, '추정' 구간은 직선거리 추정" if "길찾기" in srcs else "직선거리 기반 추정")
        evidence = [EvidenceTable(id="C1", title=f"{days}일 코스 ({pace})", dataset=dataset, data_label=label,
                                  columns=["일차", "순서", "장소", "판정", "이동(분)", "이동 기준", "실내", "장애인 화장실", "선정 근거"], rows=c1,
                                  note=("이동 시간은 카카오 길찾기(자동차)의 실제 도로 시간입니다. '추정' 구간은 " if "길찾기" in srcs else "이동 시간은 ")
                                       + f"직선거리×{cr['road_factor']}÷평균 속도로 낸 추정치입니다"
                                       f"({'버스 대기 ' + str(cr['bus_wait_min']) + '분 포함' if transport == 'bus' else '자가용·택시 기준'}). "
                                       + (f"매일 {start_name}에서 출발한다고 가정했습니다." if home else
                                          f"1일차는 {start_name}에서 출발하고, 다음 날은 "
                                          + ("코스에 넣은 숙소(좌표가 있는 곳)에서, 숙소가 없는 날 다음 날은 " if lodging_fixed else "")
                                          + "전날 마지막 장소 근처에서 출발한다고 가정했습니다. 하루 안 방문 순서는 이동이 가장 짧은 순서로 다듬었습니다."))]
        c2, fee_total, fee_unknown, toilet_missing = [], 0, 0, []
        for di, day in enumerate(plan, 1):
            fees = [p["fee_won"] for p in day if p["fee_won"] is not None]
            fee_unknown += sum(1 for p in day if p["fee_won"] is None)
            fee_total += int(sum(fees))
            has_toilet = sum(1 for p in day if p["acc_toilet"] == 1)
            if day and need_toilet and not has_toilet:
                toilet_missing.append(di)
            c2.append([f"{di}일차", len(day), sum(p["leg_min"] for p in day), visit_min * len(day), has_toilet, int(sum(fees)),
                       sum(x.get("meal_min", 0) for x in extras if x["day"] == di)])
        evidence.append(EvidenceTable(id="C2", title="하루 요약", dataset=dataset, data_label=label,
                                      columns=["일차", "장소 수", "이동 합계(분)", "관람 합계(분)", "장애인 화장실 확인 장소", "입장료 합계(원, 1인)",
                                               "식당까지 더 가는 이동(분)"],
                                      rows=c2, note="입장료는 자료에 있는 곳만 더했습니다. 식당까지 더 가는 이동은 하루 시간에 넣어 계산했습니다(식사 시간 제외)."))
        findings = [Finding(text=f"{days}일 코스에 {len(placed)}곳을 넣었습니다('{pace}' 속도, 하루 최대 {per_day}곳·관람 {visit_min}분).",
                            evidence_ids=["C1", "C2"], numbers={"days": days, "placed": len(placed), "per_day": per_day})]
        legs = [r[2] for r in c2 if r[1]]
        if legs:
            span = f"{min(legs)}분" if min(legs) == max(legs) else f"{min(legs)}~{max(legs)}분"
            findings.append(Finding(text=f"하루 이동 시간 합계는 {span}입니다({road_txt}).",
                                    evidence_ids=["C2"], numbers={"min": min(legs), "max": max(legs)}))
        if req.profile.interests:
            liked = {c for i in req.profile.interests for c in INTEREST_CATEGORIES[i]}
            hit = sum(1 for p in placed if p.get("category") in liked)
            liked_ko = "·".join(INTEREST_KO[i] for i in req.profile.interests)   # want를 덮으면 안 됨
            text = (f"관심사({liked_ko})에 맞는 곳을 {hit}곳 넣었습니다." if hit else
                    f"관심사({liked_ko})에 맞는 곳은 조건(이동 시간·접근성) 안에서 찾지 못해 넣지 못했습니다.")
            findings.append(Finding(text=text, evidence_ids=["C1"], numbers={"hit": hit}))
        if cap is not None:
            longest = max(p["leg_min"] for p in placed)
            findings.append(Finding(text=f"한 번에 이동하는 시간은 말씀하신 {cap}분을 넘지 않게 짰습니다(가장 긴 이동 {longest}분, {road_txt}).",
                                    evidence_ids=["C1"], numbers={"cap": cap, "longest": longest}))
        warnings: list[AgentWarning] = [w for w in acc.warnings if w.code in ("STALE_DATA",)]
        if use_caution:
            k = sum(1 for p in placed if p["verdict"] == "주의")
            if k:
                findings.append(Finding(text=f"{caution_why} '주의' 장소 {k}곳이 들어갔습니다. 선정 근거의 '확인할 점'을 방문 전에 확인하세요.",
                                        evidence_ids=["C1"], numbers={"ok": n_ok, "k": k}))
                warnings.append(AgentWarning(code="CAUTION_INCLUDED", severity="info", message=f"'주의' 장소 {k}곳이 포함됐습니다."))
        if wstate == "ok":
            assert pw is not None
            used = pw[pw["place_id"].isin([p["place_id"] for p in placed])]
            g = used.groupby(["observatory_code", "observatory_name"], as_index=False).agg(
                n_days=("n_days", "first"), rain_ratio=("rain_ratio", "first"), wind_ratio=("wind_ratio", "first"),
                hot_ratio=("hot_ratio", "first"))
            wl = ctx.repo.get("weather_daily").label
            evidence.append(EvidenceTable(id="C3", title=f"{req.month}월 날씨 위험 (코스 장소와 가장 가까운 관측소)",
                                          dataset=ctx.repo.get("weather_daily").source, data_label=wl,  # type: ignore[arg-type]
                                          columns=["관측소", "관측 일수", "비 온 날 비율", "강풍일 비율", "폭염일 비율"],
                                          rows=[[r.observatory_name, int(r.n_days), float(r.rain_ratio), float(r.wind_ratio), float(r.hot_ratio)]
                                                for r in g.itertuples()],
                                          note=f"비 온 날: 일강수 {wr['rain_mm']:g}mm 이상, 강풍일: 최대풍속 {wr['wind_ms']:g}m/s 이상, "
                                               f"폭염일: 최고기온 {wr['hot_c']:g}℃ 이상. 과거 같은 달 기록의 비율이며 예보가 아닙니다."))
            text = f"{req.month}월 과거 기록에서 비 온 날 비율은 평균 {rain:.0%}, 강풍일 비율은 {wind:.0%}입니다"
            text += ((f". 그래서 하루 {indoor_per_day}곳씩 실내 장소를 넣으려 했습니다." if indoor_short else
                      f". 그래서 하루 {indoor_per_day}곳은 실내 장소로 넣었습니다.") if indoor_per_day and risky else ".")
            findings.append(Finding(text=text, evidence_ids=["C3"], numbers={"rain": rain or 0, "wind": wind or 0}))
            if hot is not None and hot >= wr["hot_ratio_high"]:
                warnings.append(AgentWarning(code="HOT_MONTH", message=f"{req.month}월은 폭염일 비율이 {hot:.0%}입니다. 한낮 실외 일정은 피하세요."))
            if int(g["n_days"].min()) < wr["min_days"]:
                warnings.append(AgentWarning(code="LOW_SAMPLE", message="날씨 자료 일수가 적어 비율이 흔들릴 수 있습니다."))
        elif wstate == "no_month":
            warnings.append(AgentWarning(code="NO_MONTH", severity="info", message="여행 달을 알려 주면 그 달의 날씨 위험(비·강풍)도 반영합니다."))
        elif wstate == "not_selected":
            warnings.append(AgentWarning(code="WEATHER_SKIPPED", severity="info", message="데이터 탐색에서 날씨 자료를 쓰지 않기로 해 날씨 위험은 반영하지 않았습니다."))
        else:
            warnings.append(AgentWarning(code="NO_WEATHER", severity="info",
                                         message="센터 날씨 자료(5-01 관측소, 5-02 일일 날씨)가 없어 날씨 위험은 반영하지 않았습니다. 왼쪽 메뉴 '센터 자료 넣기'에서 넣을 수 있습니다."))
        if indoor_per_day and not risky and "indoor_pref" in req.profile.needs:
            findings.append(Finding(text=(f"실내를 원하셔서 하루 {indoor_per_day}곳씩 실내 장소를 넣으려 했습니다." if indoor_short else
                                          f"실내를 원하셔서 하루 {indoor_per_day}곳은 실내 장소로 넣었습니다."), evidence_ids=["C1"],
                                    numbers={"indoor": indoor_per_day}))
        if indoor_per_day and indoor_short:
            days_txt = ", ".join(f"{d}일차" for d in indoor_short)
            findings.append(Finding(text=f"{days_txt}에는 조건에 맞는 실내 장소가 더 없어 실외 장소로 채웠습니다. 비가 오면 일정을 바꿀 준비가 필요합니다.",
                                    evidence_ids=["C1"]))
            warnings.append(AgentWarning(code="INDOOR_SHORT", severity="info", message=f"{days_txt}: 실내 장소 부족"))
        if toilet_missing:
            days_txt = ", ".join(f"{d}일차" for d in toilet_missing)
            findings.append(Finding(text=f"{days_txt}에는 장애인 화장실이 확인된 장소가 없습니다. 근처 공공화장실을 미리 확인하세요.",
                                    evidence_ids=["C2"]))
            warnings.append(AgentWarning(code="NO_TOILET_DAY", severity="info", message=f"{days_txt}: 장애인 화장실 확인 장소 없음"))
        if short:
            days_txt = ", ".join(f"{d}일차" for d in short)
            why = "버스는 이동 시간이 길어 " if transport == "bus" else ""
            findings.append(Finding(text=f"{why}{days_txt}에는 이동 시간 {max_leg}분 안에 갈 수 있는 조건에 맞는 장소가 모자라 {per_day}곳보다 적게 넣었습니다. "
                                         + ("택시·교통약자 콜택시를 함께 쓰거나 " if transport == "bus" else "") + "일정을 줄이는 것을 검토하세요.",
                                    evidence_ids=["C2"], numbers={"max_leg": max_leg, "per_day": per_day}))
            warnings.append(AgentWarning(code="SHORT_DAY", severity="info", message=f"{days_txt}는 조건에 맞는 장소가 모자라 {per_day}곳보다 적게 넣었습니다."))
        n_fee = len(placed) - fee_unknown
        unk_txt = f" 입장료 정보가 없는 곳은 {fee_unknown}곳입니다." if fee_unknown else ""
        if not n_fee:
            fee_txt = "코스 장소의 입장료는 자료에 없어 방문 전 확인이 필요합니다."
        elif fee_total == 0:
            fee_txt = f"확인된 {n_fee}곳은 입장료가 없습니다(조사 당시)." + unk_txt
        else:
            fee_txt = f"확인된 입장료 합계는 1인 {fee_total:,}원"
            if req.profile.companions > 1:
                fee_txt += f"(일행 {req.profile.companions}명이면 {fee_total * req.profile.companions:,}원)"
            fee_txt += f"이고, 입장료 정보가 없는 곳은 {fee_unknown}곳입니다." if fee_unknown else "입니다."
        findings.append(Finding(text=fee_txt, evidence_ids=["C2"], numbers={"fee": fee_total, "unknown": fee_unknown}))
        comp = [p["name"] for p in placed if any("보호자·동행인" in t for t in p["reasons"])]
        if comp:
            findings.append(Finding(text=f"코스 장소 중 {len(comp)}곳({', '.join(comp)})은 조사 자료에 '보호자·동행인 동반 시 추천'으로 "
                                         "적혀 있습니다. 동행인과 함께 가세요.", evidence_ids=["C1"], numbers={"companion": len(comp)}))
        # 식당·숙소: 판정은 장소 찾기가 했고 코스는 자리만 정함
        if pick is not None and not pick and not want:
            self.step("extras", "식당·숙소는 넣지 않기로 함(화면 선택)")
        elif extras_on:
            self.step("extras", f"점심 식당 {sum(x['slot'] == '점심' for x in extras)}곳 · 저녁 식당 {sum(x['slot'] == '저녁' for x in extras)}곳"
                      f" · 숙소 {sum(x['slot'] == '숙소' for x in extras)}곳"
                      + (" (주변정보가 없는 날 있음)" if any(miss.values()) else ""))
            if extras:
                srcs = list(dict.fromkeys([NEARBY_SOURCE if x.get("source", NEARBY_SOURCE) == NEARBY_SOURCE else kto_data.SOURCE for x in extras]))
                kto_used = kto_data.SOURCE in srcs
                evidence.append(EvidenceTable(
                    id="C5", title="코스에 넣은 식당·숙소", dataset=" · ".join(srcs), data_label=label,
                    columns=["일차", "자리", "이름", "분류", "기준 장소", "거리(km)", "거리 기준", "식당까지 더 가는 이동(분)",
                             "장애인 화장실", "출입", "전화", "출처"],
                    rows=[[f"{x['day']}일차", {"점심": f"{x['after']}번째 장소 뒤 점심", "저녁": "하루 끝 저녁"}.get(x["slot"], "하루 끝 숙소"),
                           x["name"], KIND_KO[x["kind"]], x["anchor_name"], x["distance_km"],
                           "추정(기준 코스 경유)" if x.get("estimated") else "직선(가게 좌표)", x.get("meal_min", 0),
                           x["facilities"][0]["state"] if x["facilities"] else "정보 없음", shop_entry(x),
                           x["phone"] or "정보 없음", shop_basis(x)] for x in extras],
                    note=("주변정보 가게는 휠체어 진입 가능으로 조사된 곳(2021-12-22 조사)이라 문을 닫았거나 바뀌었을 수 있습니다. "
                          + ("관광공사 식당은 장애인 화장실이 '있음'이거나 휠체어로 들어갈 수 있다고 적힌 곳만 넣었고(빈칸은 '없음'이 아니라 "
                             "'정보 없음' — 방문 전 확인), 화장실이 필요한 조건이면 화장실 있는 식당을 먼저 골랐습니다"
                             f"(가까운 식당보다 {cr.get('toilet_first_max_extra_min', 15)}분 넘게 더 가야 하면 가까운 식당). "
                             "출처의 날짜는 관광공사 자료의 수정일입니다. " if kto_used else "")
                          + "전화로 영업 여부를 확인하세요. "
                          f"코스 장소에서 약 {max_km:g}km 안의 곳만 넣었습니다. 가게 좌표(관광공사 좌표, 주변정보는 카카오 주소 검색으로 실시간 확인 — 저장하지 않음)가 있으면 "
                          "직선거리이고, 점심은 그 장소 → 식당 → 다음 장소로 덜 돌아가는 곳을 골랐습니다. 좌표가 없는 곳은 기준 무장애 코스를 거친 추정 거리입니다. "
                          "식당까지 더 가는 이동 시간은 하루 일정 시간에 넣어 계산했고(식사 시간 제외), 하루 시간을 넘는 날은 그날 1곳을 줄였습니다"
                          f"(하루 {MIN_PER_DAY}곳 아래로는 줄이지 않음). 숙소가 정해진 날 다음 날은 그 숙소에서 출발합니다.")))
                n_l, n_d, n_s = (sum(x["slot"] == k for x in extras) for k in ("점심", "저녁", "숙소"))
                parts = ([f"점심 식당 {n_l}곳"] if n_l else []) + ([f"저녁 식당 {n_d}곳"] if n_d else []) + ([f"숙소 {n_s}곳"] if n_s else [])
                findings.append(Finding(text=", ".join(parts) + "을 코스에 넣었습니다"
                                             "(조사 뒤 바뀌었을 수 있어 전화로 영업 여부 확인).", evidence_ids=["C5"],
                                        numbers={"lunch": n_l, "dinner": n_d, "lodging": n_s}))
            food = [i for i in items if i["kind"] == "restaurant"]
            food_blocked = bool(food) and all(i["verdict"] == "부적합" for i in food)
            for slot, ds in miss.items():
                if ds and not (food_blocked and slot != "숙소"):   # 화장실 조건 때문에 뺀 거면 '자료에 없음'이라고 하면 안 됨
                    findings.append(Finding(text=f"{', '.join(f'{d}일차' for d in ds)}에는 코스 장소 주변 "
                                                 f"{'숙소' if slot == '숙소' else slot + ' 식당'} 정보가 자료에 없어요.", evidence_ids=["C1"]))
            if (miss["점심"] or miss["저녁"]) and food_blocked:
                findings.append(Finding(text=f"장애인 화장실이 필요한 조건인데, 휠체어 진입 가능으로 조사된 식당 {len(food)}곳은 모두 "
                                             "장애인 화장실이 없다고 조사돼 코스에 넣지 않았습니다.", evidence_ids=["C1"],
                                        numbers={"food": len(food)}))
            elif want and any(miss[k] for k in ("점심", "저녁", "숙소")):
                anchors = int(ctx.repo.nearby_df()["place_id"].nunique())
                kto_n = int((ctx.repo.kto_df()["kind"] == "restaurant").sum()) if uses(req, "kto_bf") else 0
                where = (f"무장애 코스 {anchors}곳 주변(2021년 조사)과 관광공사 무장애 식당 {kto_n}곳뿐이라"
                         if kto_n else f"무장애 코스 {anchors}곳 주변에만 있어(2021년 조사)")
                findings.append(Finding(text=f"휠체어로 들어갈 수 있다고 적힌 식당·숙소는 {where}, "
                                             f"코스 장소에서 약 {max_km:g}km 안에 없는 날은 넣지 못했습니다. 장소를 누르면 카카오 지도로 주변을 찾아볼 수 있어요.",
                                        evidence_ids=["C1"], numbers={"anchors": anchors, "year": 2021, "max_km": max_km}))

        stops: dict[str, dict] = {}
        if transport == "bus" and uses(req, "c3_03") and ctx.repo.has("bus_stops"):
            st = nearest_stops(pd.DataFrame(placed), ctx.repo.get("bus_stops").df)
            stops = {r.place_id: {"name": str(r.stop_nm), "m": int(r.stop_m), "lat": float(r.stop_lat), "lng": float(r.stop_lng)}
                     for r in st.itertuples()}   # 정류소 표시 (버스는 노선 경로 없어서 직선 점선)
            near = rules["bus"]["near_m"].get(req.profile.mobility, rules["bus"]["near_m"]["default"])
            names = {p["place_id"]: p["name"] for p in placed}
            evidence.append(EvidenceTable(id="C4", title="코스 장소에서 가장 가까운 버스 정류소", dataset=ctx.repo.get("bus_stops").source,
                                          data_label=ctx.repo.get("bus_stops").label,  # type: ignore[arg-type]
                                          columns=["장소", "가까운 정류소", "직선거리(m)"],
                                          rows=[[names[r.place_id], r.stop_nm, int(r.stop_m)] for r in st.itertuples()],
                                          note="직선거리입니다. 저상버스 운행 여부는 자료에 없습니다 [미확인]."))
            far = int((st["stop_m"] > near).sum())
            text = (f"코스 장소 {len(st)}곳 중 {far}곳은 가장 가까운 정류소가 {near}m보다 멉니다." if far
                    else f"코스 장소 {len(st)}곳 모두 {near}m 안에 버스 정류소가 있습니다(직선거리).")
            findings.append(Finding(text=text, evidence_ids=["C4"], numbers={"far": far, "n": len(st), "near": near}))
            if far:
                warnings.append(AgentWarning(code="BUS_FAR", severity="info", message=f"{far}곳은 버스 정류소가 멉니다. 택시·교통약자 콜택시를 함께 고려하세요."))
        elif transport == "bus":
            warnings.append(AgentWarning(code="NO_BUS_DATA", severity="info", message="센터 버스 정류소 자료(3-03)가 없어 정류소 거리는 점검하지 않았습니다."))

        itinerary = [{"day": di, "order": k, **{x: p[x] for x in ("place_id", "name", "lat", "lng", "verdict", "leg_min", "leg_src",
                                                                  "why", "guide")},
                      **({"path": p["path"]} if p.get("path") else {}),
                      **({"stop": stops[p["place_id"]]} if p["place_id"] in stops else {})}
                     for di, day in enumerate(plan, 1) for k, p in enumerate(day, 1)]
        edit = req.params.get("edit")
        if edit and edit.get("kind") in ("exclude", "include", "lighter", "lighter_all", "join", "day_count", "day_swap"):
            t6, f6 = edit_diff(edit, req.params.get("prev_itinerary") or [], plan, label, dataset, req.profile.companions, per_day,
                               kept=sorted(fixed), day_minutes=day_minutes)
            evidence.append(t6)
            findings.insert(0, f6)   # 뭐가 바뀌었는지 제일 먼저 보여 줌
        issues = course_issues(toilet_missing=toilet_missing, toilet_per_day=toilet_per_day or 0, short=short, max_leg=max_leg,
                               cap=cap, risky=risky, indoor_per_day=indoor_per_day, indoor_short=indoor_short,
                               meal_miss=[] if food_blocked else sorted(set(miss["점심"] + miss["저녁"])))
        if issues:
            self.step("check", "점검: " + ", ".join(f"{i['text']} {len(i['days'])}일" for i in issues))
        confidence = "low" if short or not placed else ("medium" if any(w.severity != "info" for w in warnings) else "high")
        return AgentResult(
            agent=self.name, status="ok" if not short else "partial", confidence=confidence,  # type: ignore[arg-type]
            summary=f"{days}일 코스: " + " / ".join(f"{di}일차 " + " → ".join(p["name"] for p in day) for di, day in enumerate(plan, 1) if day),
            findings=findings, evidence=evidence, warnings=warnings,
            limitations=[("이동 시간은 카카오 길찾기(자동차) 기준이라 실제 교통 상황과 다를 수 있습니다." if srcs == {"길찾기"} else
                          "이동 시간은 직선거리로 낸 추정치가 섞여 있습니다('추정' 표시). 실제 길·교통 상황과 다릅니다."),
                         "무장애 조사 자료(2019~2021년 등록) 기준이라 지금 시설과 다를 수 있습니다. 방문 전 전화로 확인하세요.",
                         "운영시간·입장료는 일부 장소만 조사 당시 기록이 있습니다. 휴무일·예약 필요 여부는 자료에 없습니다."],
            follow_ups=edit_examples(plan, placed) + ([f"{days + 1}일로 늘려줘"] if days < cr["max_days"] else [])
                       + ["보고서로 정리해줘"],
            payload={"itinerary": itinerary, "place_ids": [p["place_id"] for p in placed], "pace": pace,
                     "indoor_per_day": indoor_per_day, "max_leg_min": max_leg, "short_days": short, "rain_ratio": rain,
                     "wind_ratio": wind, "weather_risky": risky, "weather": wstate, "relaxed": relaxed, "start": start_name,
                     "ride_cap": cap, "indoor_short_days": indoor_short, "excluded_ids": excluded, "day_caps": day_caps,
                     "extras": extras, "toilet_per_day": toilet_per_day or 0, "toilet_missing_days": toilet_missing,
                     "issues": issues,
                     "extras_miss": {k: v for k, v in miss.items() if v}, "extras_food_blocked": food_blocked,
                     "cards": [c for pid in [p["place_id"] for p in placed]
                               for c in acc.payload.get("cards", []) if c["place_id"] == pid]},
            data_labels=list(dict.fromkeys([label] + [e.data_label for e in evidence])))  # type: ignore[list-item]

    # ── LLM 판단 도구 ──
    def tools(self, req: AnalysisRequest, ctx: AgentContext) -> list[AgentTool]:
        """속도·실내·이동 상한·화장실은 LLM이 정함. 안전 조건이랑 이용자가 정한 건 못 바꿈"""
        rules = access_rules()
        runs: list[dict] = []

        def run(args: dict) -> AgentResult:
            r = self.plan(req, ctx, pace=args.get("pace"), indoor_per_day=args.get("indoor_per_day"),
                          max_leg_min=args.get("max_leg_min"), toilet_per_day=args.get("toilet_per_day"))
            runs.append(r.payload)
            return r

        def check(args: dict) -> str | None:
            if len(runs) >= MAX_PLAN_RUNS:
                return f"코스는 {MAX_PLAN_RUNS}번까지만 짤 수 있습니다(점검 뒤 한 번 다시). 지금까지 짠 결과 중 하나를 finish로 고르세요."
            if args.get("pace") == "보통" and forced_relaxed(req.profile, rules):
                return "이용자 조건(수동 휠체어·고령·자주 쉬어야 함)상 '여유' 속도만 쓸 수 있습니다."
            if args.get("indoor_per_day") == 0 and "indoor_pref" in req.profile.needs:
                return "이용자가 실내를 원한다고 했습니다. 실내 장소를 0곳으로 할 수 없습니다."
            cap = req.profile.max_ride_min
            if args.get("max_leg_min") and cap is not None and args["max_leg_min"] > cap:
                return f"이용자가 한 번에 이동할 수 있는 시간은 {cap}분입니다. 그보다 긴 상한은 쓸 수 없습니다."
            return None

        def observe(r: AgentResult) -> dict:
            return {**default_observe(r), **{k: r.payload.get(k) for k in (
                "pace", "indoor_per_day", "max_leg_min", "toilet_per_day", "short_days", "rain_ratio", "wind_ratio",
                "weather_risky", "weather", "issues")}}

        return [AgentTool(
            name="plan",
            description="코스를 짠다. 조건을 비우면 기본값(조건에 맞는 속도, 날씨 위험·실내 선호면 하루 1곳 실내, 이동 45분 상한, 화장실 우선 없음). "
                        "결과의 issues는 코드가 점검한 문제와 고칠 조건(fix)이다.",
            params={"pace": {"type": "string", "enum": list(PACES), "description": "여유=하루 2곳·관람 90분, 보통=하루 3곳·60분"},
                    "indoor_per_day": {"type": "integer", "minimum": 0, "maximum": 2, "description": "하루에 넣을 실내 장소 수"},
                    "max_leg_min": {"type": "integer", "enum": list(LEG_OPTIONS), "description": "장소 사이 이동 시간 상한(분)"},
                    "toilet_per_day": {"type": "integer", "minimum": 0, "maximum": 1,
                                       "description": "1이면 날마다 장애인 화장실이 확인된 장소를 한 곳 이상 먼저 넣는다"}},
            run=run, check=check, observe=observe)]

    def before_finish(self, run_id: str, runs: dict[str, AgentResult]) -> str | None:
        """고칠 문제가 있는데 아직 다시 안 짜 봤으면 안 끝냄"""
        fix = fix_args(runs[run_id].payload)
        if fix and len(runs) < MAX_PLAN_RUNS:
            return ("점검에서 고칠 수 있는 문제가 있습니다(" + ", ".join(i["text"] for i in runs[run_id].payload["issues"] if i["fix"])
                    + "). issues의 fix 조건(" + ", ".join(f"{k}={v}" for k, v in fix.items()) + ")으로 한 번 다시 짜 본 뒤 고르세요.")
        return None
