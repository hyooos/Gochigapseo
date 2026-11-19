"""접근성 진단 에이전트: 이용자 조건으로 관광지 적합·주의·부적합 판정 (기준은 config/access_rules.yaml)
LLM은 '주의' 포함 여부랑 지역 밖 확장만 고름. 경사·시설 기준은 못 바꿈

'정보 없음'은 '없음'이 아님. '적합'도 안전 보장 아님
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd

from ..config import access_rules, catalog
from ..contracts import (
    INTEREST_CATEGORIES,
    KIND_KO,
    NEARBY_KIND,
    AgentResult,
    AgentWarning,
    AnalysisRequest,
    EvidenceTable,
    Finding,
    Intent,
    PlaceItem,
    TravelerProfile,
)
from ..data import DataNotReady
from ..data import kto as kto_data
from ..data.regions import region_center
from ..data.visitjeju import fill_blank_facilities, is_tag_evidence
from ..text import josa
from ..tools.geo import haversine_km
from .base import (
    AgentContext,
    AgentTool,
    BaseAgent,
    default_observe,
    filter_places,
    uses,
)

VERDICT_ORDER = {"적합": 0, "주의": 1, "부적합": 2}
SHARE_FACILITIES = ("acc_toilet", "acc_parking", "ramp", "elevator")   # 가족 공유 일정표에 항상 보이는 기본 시설
NEARBY_SOURCE = "제주데이터허브 무장애여행코스 주변정보 (2021-12-22 조사)"
NEARBY_CALL = ["지금도 영업하시나요?", "휠체어로 들어갈 수 있나요?"]
NEARBY_SHOW = 8               # 기준 장소 없는 근처 질문에서 보여 줄 가게 수 (화장실 있는 곳 먼저, 가까운 순)
NEARBY_FALLBACK_KM = 3.0      # 주변정보 없는 관광지면 이 거리 안의 무장애 코스 주변으로 대신 안내
NEARBY_FALLBACK_N = 2
FAC_KO = {"acc_toilet": "장애인 화장실", "acc_parking": "장애인 주차", "wheelchair_rental": "휠체어 대여",
          "stroller_rental": "유아차 대여", "ramp": "경사로", "elevator": "엘리베이터", "braille": "점자 안내",
          "audio_guide": "음성 안내"}
BARRIER_KO = {"stairs": "계단", "threshold": "턱·단차", "inaccessible": "접근불가", "rough": "노면 불량"}
BARRIERS_ORDER = ("stairs", "inaccessible", "threshold", "rough")
# 이동 방법·감각 조건별로 있으면 도움 되는 시설 (점수 가산)
HELPFUL = {
    "wheelchair_manual": ["acc_toilet", "acc_parking", "wheelchair_rental", "ramp", "elevator"],
    "wheelchair_power": ["acc_toilet", "acc_parking", "ramp", "elevator"],
    "walker": ["acc_toilet", "wheelchair_rental", "elevator"],
    "senior": ["acc_toilet", "wheelchair_rental", "elevator"],
    "stroller": ["stroller_rental", "elevator", "ramp"],
    "none": [],
}
MAX_ROWS = 15
EXTEND_KM = (0, 10, 20)
# 코스 조사자가 적은 '추천 대상' (코스 50곳만, 관광지는 빈칸 = 모름). 값: Y / Y(동반) / N
# Y는 거의 전부라 근거로 안 쓰고, N이랑 Y(동반)만 판정에 씀
SURVEY_COL = {"wheelchair_manual": "rec_wheelchair_manual", "wheelchair_power": "rec_wheelchair_electric",
              "walker": "rec_physical_walking", "senior": "rec_senior", "stroller": "rec_infant"}
SURVEY_KO = {"rec_wheelchair_manual": "수동 휠체어", "rec_wheelchair_electric": "전동 휠체어", "rec_physical_walking": "보행 가능 지체장애",
             "rec_senior": "시니어", "rec_infant": "영유아·유아차", "rec_visual": "시각장애", "rec_hearing": "청각장애",
             "rec_intellectual": "발달·지적장애"}


def survey_cols(p: TravelerProfile) -> list[str]:
    return ([SURVEY_COL[p.mobility]] if p.mobility in SURVEY_COL else []) + \
        (["rec_visual"] if p.vision != "none" else []) + (["rec_hearing"] if p.hearing != "none" else []) + \
        (["rec_intellectual"] if p.cognition != "none" else [])


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _isnum(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def needed_facilities(p: TravelerProfile) -> list[str]:
    """이용자가 꼭 필요하다고 한 시설. 버스면 주차는 안 봄"""
    out = []
    if "acc_toilet" in p.needs:
        out.append("acc_toilet")
    if "acc_parking" in p.needs and p.transport != "bus":
        out.append("acc_parking")
    return out


def checked_facilities(p: TravelerProfile) -> list[str]:
    """확인할 시설 = 꼭 필요한 것 + 도움 되는 것. '정보 없음 비율'의 분모"""
    helpful = HELPFUL[p.mobility] + (["braille", "audio_guide"] if p.vision != "none" else [])
    return list(dict.fromkeys(needed_facilities(p) + helpful))


def assess(places: pd.DataFrame, profile: TravelerProfile, rules: dict | None = None,
           points: pd.DataFrame | None = None) -> pd.DataFrame:
    """장소마다 판정·점수·이유 열 더한 새 표
    필요 시설 '없음 확인' → 부적합, '정보 없음' → 주의. 점수는 확인된 사실로만
    """
    rules = rules or access_rules()
    mob = profile.mobility
    slope, bar, sc = rules["slope"][mob], rules["barriers"][mob], rules["score"]
    need = needed_facilities(profile)
    checked = checked_facilities(profile)
    liked = {c for i in profile.interests for c in INTEREST_CATEGORIES[i]}
    sb = rules["slope_block"]
    block_pts: dict[str, int] = {}
    steep: dict[str, int] | None = None     # caution 기준 넘는 경사 기록 수 (조사 지점 있을 때만)
    if points is not None and not points.empty and "place_id" in points:
        p = points.assign(_b=(pd.to_numeric(points.get("stairs"), errors="coerce").fillna(0) > 0)
                          | (pd.to_numeric(points.get("inaccessible"), errors="coerce").fillna(0) > 0))
        block_pts = p.groupby("place_id")["_b"].sum().astype(int).to_dict()
        if "slope_deg" in p:
            steep = (pd.to_numeric(p["slope_deg"], errors="coerce") > slope["caution"]).groupby(p["place_id"]).sum().astype(int).to_dict()

    rows = []
    for _, r in places.iterrows():
        block, caution, unknown, good, notes = [], [], [], [], []
        ms = r.get("max_slope_deg")
        over = 0.0
        if _isnum(ms):
            n_steep = 1 if steep is None else max(1, steep.get(r["place_id"], 0))
            if ms > slope["caution"] and (steep is None or n_steep >= sb["count"] or ms >= slope["caution"] + sb["margin"]):
                where = f" {n_steep}곳" if n_steep > 1 else ""
                block.append(("slope_block", f"경사 {ms:g}° 기록{where}(기준 {slope['caution']:g}° 초과)"))
            elif ms > slope["caution"]:
                caution.append(("slope_steep", f"경사 {ms:g}° 기록 1곳(기준 {slope['caution']:g}° 초과, 동반자 도움 권장)"))
            elif ms > slope["ok"]:
                caution.append(("slope_caution", f"경사 {ms:g}° 기록(동반자 도움 권장)"))
            over = max(0.0, ms - slope["ok"])
        n_pts = int(r.get("n_points") or 0)
        bad_pts = block_pts.get(r["place_id"], min(n_pts, int(r.get("n_stairs", 0)) + int(r.get("n_inaccessible", 0))))
        if n_pts and bad_pts / n_pts >= bar["block_share"]:
            block.append(("barrier_share", f"계단·접근불가 지점이 많음({bad_pts}/{n_pts}곳)"))
        barrier_pts = 0
        for b in bar["caution"]:
            n = int(r.get(f"n_{b}", 0) or 0)
            if n > 0:
                caution.append((f"{b}", f"{BARRIER_KO[b]} 지점 {n}곳"))
                barrier_pts += n
        for f in need:
            v = r.get(f)
            if _isnum(v) and v == 0:
                block.append((f"{f}_none", f"{FAC_KO[f]} 없음 확인"))
            elif not _isnum(v):
                unknown.append((f"{f}_unknown", f"{FAC_KO[f]} 정보 없음(방문 전 확인)"))
        for f in checked:
            if _isnum(r.get(f)) and r.get(f) == 1:
                good.append(f"{FAC_KO[f]} 있음")
        unknown_n = sum(1 for f in checked if not _isnum(r.get(f)))
        interest_bonus = 0
        if liked and r.get("category") in liked:
            interest_bonus = sc["interest_bonus"]
            good.append("관심사 맞음")
        for col in survey_cols(profile):
            v = _text(r.get(col))
            if v == "N":
                block.append(("survey_no", f"조사자 비추천({SURVEY_KO[col]})"))
            elif v == "Y(동반)" and profile.companions < 2:
                # 점수는 그대로, '주의'로만. 조사가 코스에만 있어서 감점하면 조사 안 된 관광지보다 밀림
                unknown.append(("companion", f"보호자·동행인 동반 시 추천({SURVEY_KO[col]}, 조사자 의견)"))
        if not (_isnum(r.get("lat")) and _isnum(r.get("lng"))):
            notes.append("좌표 없음(지도·코스 제외)")
        indoor_bonus = sc["indoor_bonus"] if ("indoor_pref" in profile.needs and r.get("indoor") == 1) else 0
        n_good_fac = sum(1 for g in good if g != "관심사 맞음")
        score = (sc["base"] - sc["per_caution_reason"] * len(caution)
                 - min(sc["barrier_cap"], sc["per_barrier_point"] * barrier_pts) - sc["per_slope_degree_over"] * over
                 + sc["facility_bonus"] * n_good_fac + indoor_bonus + interest_bonus)
        verdict = "부적합" if block else "주의" if (caution or unknown) else "적합"
        rows.append({"place_id": r["place_id"], "verdict": verdict, "score": int(max(0, min(100, round(score)))),
                     "reasons": block + caution + unknown, "good": good, "notes": notes,
                     "unknown_n": unknown_n, "checked_n": len(checked)})
    out = places.reset_index(drop=True).copy()
    extra = pd.DataFrame(rows, columns=["place_id", "verdict", "score", "reasons", "good", "notes", "unknown_n", "checked_n"])
    out = out.merge(extra, on="place_id", how="left")
    out["_order"] = out["verdict"].map(VERDICT_ORDER)
    # 판정 → 점수 → 정보 더 많이 확인된 곳 → 이름 순
    return (out.sort_values(["_order", "score", "unknown_n", "name"], ascending=[True, False, True, True])
            .drop(columns="_order").reset_index(drop=True))


def coverage(a: pd.DataFrame) -> pd.DataFrame:
    """읍면동별 조건 충족 수 + 정보 없음 비율 (접근성 리포트 중심 표)"""
    d = a.assign(group=a["emd"].fillna(a["region"]).fillna("미상"))
    g = d.groupby("group")
    out = pd.DataFrame({
        "n": g.size(),
        "ok": g["verdict"].apply(lambda s: int((s == "적합").sum())),
        "caution": g["verdict"].apply(lambda s: int((s == "주의").sum())),
        "block": g["verdict"].apply(lambda s: int((s == "부적합").sum())),
        "unk": g["unknown_n"].sum(), "chk": g["checked_n"].sum(),
    }).reset_index()
    out["gap"] = (out["unk"] / out["chk"].where(out["chk"] > 0)).round(2)
    return out.sort_values(["ok", "n", "group"], ascending=[False, False, True]).reset_index(drop=True)


def gap_text(row) -> str:
    """'정보 없음 2/5' 칸. 확인할 시설 없으면 '-'"""
    return f"{int(row['unknown_n'])}/{int(row['checked_n'])}" if row.get("checked_n") else "-"


FAC_STATE = {1: ("있음", "✓"), 0: ("없음", "✗")}
CARD_MAX = 8
MOBILITY_CALL = {"wheelchair_manual": "휠체어를 쓰는 사람", "wheelchair_power": "전동휠체어를 쓰는 사람",
                 "walker": "보행 보조기를 쓰는 사람", "senior": "오래 걷기 힘든 어르신", "stroller": "유아차를 끄는 가족",
                 "none": "몸이 불편한 사람"}


def _basic_fac(row, f: str) -> dict:
    """기본 시설 한 칸(상태·기호·원문 근거). 조건 상관없이 같은 모양"""
    v = row.get(f)
    state, mark = FAC_STATE.get(int(v), ("정보 없음", "?")) if _isnum(v) else ("정보 없음", "?")
    ev = row.get(f"ev_{f}")
    return {"key": f, "label": FAC_KO[f], "state": state, "mark": mark,
            "evidence": ev if isinstance(ev, str) and ev else None, "needed": False}


KEY_LABEL = {"slope": "경사", "stairs": "계단", "inaccessible": "접근 불가"}


def key_checks(row, profile: TravelerProfile, rules: dict) -> dict | None:
    """이동 조건별 '확인할 점' (판정은 안 바꿈). state: ok / warn / unknown"""
    conf = (rules.get("key_checks") or {}).get(profile.mobility)
    if not conf:
        return None
    n_pts = int(row.get("n_points") or 0) if _isnum(row.get("n_points")) else 0
    items = []
    for k in conf["items"]:
        if k == "slope":
            ok = rules["slope"][profile.mobility]["ok"]
            if _isnum(row.get("max_slope_deg")):
                d = float(row["max_slope_deg"])
                items.append({"key": k, "label": KEY_LABEL[k], "state": "ok" if d <= ok else "warn",
                              "text": f"최대 {d:g}°(기준 {ok:g}° 이하)"})
            else:
                items.append({"key": k, "label": KEY_LABEL[k], "state": "unknown", "text": "기록 없음"})
        else:
            n = int(row.get(f"n_{k}") or 0) if _isnum(row.get(f"n_{k}")) else 0
            if not n_pts:
                items.append({"key": k, "label": KEY_LABEL[k], "state": "unknown", "text": "조사 지점 없음"})
            else:
                items.append({"key": k, "label": KEY_LABEL[k], "state": "warn" if n else "ok",
                              "text": f"{n}곳 기록" if n else f"기록된 곳 없음(조사 지점 {n_pts}곳)"})
    return {"title": conf["title"], "items": items, "need_call": any(i["state"] == "unknown" for i in items),
            "ask": conf.get("ask")}


def place_card(row, profile: TravelerProfile, rules: dict, points: pd.DataFrame | None, dataset: str,
               nearby: list[dict] | None = None) -> dict:
    """장소 카드 1장: 시설(✓·✗·?)과 근거, 장애물, 출처·기준일, 전화 확인 문장"""
    facilities = []
    for f in checked_facilities(profile):
        v = row.get(f)
        state, mark = FAC_STATE.get(int(v), ("정보 없음", "?")) if _isnum(v) else ("정보 없음", "?")
        ev = row.get(f"ev_{f}")
        facilities.append({"key": f, "label": FAC_KO[f], "state": state, "mark": mark,
                           "evidence": ev if isinstance(ev, str) and ev else None, "needed": f in needed_facilities(profile)})
    barriers = [f"{BARRIER_KO[b]} {int(row.get(f'n_{b}') or 0)}곳" for b in BARRIERS_ORDER if int(row.get(f"n_{b}") or 0) > 0]
    if _isnum(row.get("max_slope_deg")):
        barriers.append(f"경사 최대 {float(row['max_slope_deg']):g}°")
    spots: list[str] = []
    if points is not None and not points.empty and "place_id" in points:
        p = points[points["place_id"] == row["place_id"]]
        hit = p[(pd.to_numeric(p.get("stairs"), errors="coerce").fillna(0) > 0)
                | (pd.to_numeric(p.get("inaccessible"), errors="coerce").fillna(0) > 0)]
        spots = [str(x) for x in hit["point_name"].dropna().head(3) if str(x).strip()]
    q = rules["call_questions"]
    script = [f"안녕하세요. {josa(MOBILITY_CALL[profile.mobility], '이')} 방문하려고 하는데 몇 가지 여쭤봐도 될까요?"]
    kc = key_checks(row, profile, rules)
    if kc and kc["need_call"]:   # 중요한 정보가 자료에 없으면 그 질문을 맨 앞에
        for it in kc["items"]:
            if it["state"] == "unknown":
                script.append(q[kc["ask"]] if it["key"] == "slope" else q[f"{it['key']}_unknown"])
    for x in facilities:
        if x["state"] == "정보 없음" and x["key"] in q:
            script.append(q[x["key"]])
    if spots:
        script.append(q["stairs_spot"].format(spots=", ".join(spots)))
    elif int(row.get("n_stairs") or 0) or int(row.get("n_inaccessible") or 0):
        script.append(q["stairs"])
    if _isnum(row.get("max_slope_deg")) and float(row["max_slope_deg"]) > rules["slope"][profile.mobility]["ok"]:
        script.append(q["slope"])
    if not _isnum(row.get("fee_won")):
        script.append(q["fee"])
    script.append(q["closing"])
    return {
        "place_id": row["place_id"], "name": row["name"], "verdict": row["verdict"], "score": int(row["score"]),
        "emd": row.get("emd") if isinstance(row.get("emd"), str) else row.get("region"),
        "category": row.get("category"), "facilities": facilities, "barriers": barriers, "barrier_spots": spots,
        "reasons": [t for _, t in row["reasons"]], "gap": gap_text(row),
        "source": dataset, "source_file": row.get("source_file") if isinstance(row.get("source_file"), str) else None,
        "data_date": row.get("data_date") if isinstance(row.get("data_date"), str) else None,
        "date_basis": _text(row.get("date_basis")),
        "stale": stale(row.get("data_date"), rules["stale_years"]), "call_script": script,
        "key_checks": kc,
        # 정리본 안내(빈칸 = 자료 없음). 문장은 report/cards.info_lines가 만듦
        **{k: _text(row.get(k)) for k in ("route", "parking_location", "toilet_location", "alt_toilet", "fee_note", "hours_note")},
        "fee_won": int(row["fee_won"]) if _isnum(row.get("fee_won")) else None,
        "survey": [f"{SURVEY_KO[c]}: {'보호자·동행인 동반 시 추천' if _text(row.get(c)) == 'Y(동반)' else '조사자 비추천'}"
                   for c in survey_cols(profile) if _text(row.get(c)) in ("Y(동반)", "N")],
        "nearby": nearby or [],
        # 가족 공유용: 조건 상관없이 기본 시설 4개 (조건이 드러나면 안 됨)
        "basic_facilities": [_basic_fac(row, f) for f in SHARE_FACILITIES],
    }


def place_items(cards: list[dict], xy: dict[str, tuple], profile: TravelerProfile, rules: dict) -> list[dict]:
    """장소 카드 → PlaceItem 목록. 관광지 + 주변 식당·카페·숙소
    주변 장소는 기본 적합, 화장실 필요한데 'N'이면 부적합. 좌표는 없음 (카카오 응답 저장 금지)
    """
    need_toilet = "acc_toilet" in needed_facilities(profile)
    out: list[dict] = []
    for c in cards:
        lat, lng = xy.get(c["place_id"], (None, None))
        out.append(PlaceItem(
            item_id=c["place_id"], name=c["name"], kind="attraction", category=_text(c.get("category")) or None,
            verdict=c["verdict"], reasons=c["reasons"], facilities=c["facilities"],
            lat=lat if _isnum(lat) else None, lng=lng if _isnum(lng) else None,
            source=c["source"], data_date=c["data_date"], stale=c["stale"], call_script=c["call_script"]).model_dump())
        for n in c.get("nearby", []):
            state = {"Y": "있음", "N": "없음"}.get(n.get("disabled_toilet"), "정보 없음")
            block = need_toilet and state == "없음"
            out.append(PlaceItem(
                item_id=f"{c['place_id']}-N{n.get('seq', 0)}", name=n["name"],
                kind=NEARBY_KIND.get(n["category"], "restaurant"),  # type: ignore[arg-type]
                category=n["category"], verdict="부적합" if block else "적합",
                reasons=["휠체어 진입 가능으로 조사된 곳"] + (["장애인 화장실이 없다고 조사됨"] if block else []),
                facilities=[{"key": "acc_toilet", "label": FAC_KO["acc_toilet"], "state": state,
                             "mark": {"있음": "✓", "없음": "✗"}.get(state, "?"), "evidence": None, "needed": need_toilet}],
                phone=n.get("phone") or None, anchor_id=c["place_id"], distance_km=float(n["distance_km"]),
                lat=float(n["lat"]) if _isnum(n.get("lat")) else None, lng=float(n["lng"]) if _isnum(n.get("lng")) else None,
                source=NEARBY_SOURCE, data_date=n.get("base_date") or None,
                stale=stale(n.get("base_date"), rules["stale_years"]), call_script=list(NEARBY_CALL)).model_dump())
    return out


def nearby_rows(near: pd.DataFrame, place_id: str, kinds: list[str], max_km: float = 3.0) -> list[dict]:
    """근처 질문용: 물어본 종류 가게를 개수 제한 없이(max_km 안)
    '카페만' 물었는데 카페가 식당에 밀려 빠지면 안 돼서 따로 둠. 번호는 repository.nearby랑 같은 규칙
    """
    n = near[near["place_id"] == place_id]
    n = n.assign(seq=range(1, len(n) + 1))
    n = n[(pd.to_numeric(n["distance_km"], errors="coerce") <= max_km) & n["category"].map(NEARBY_KIND).isin(kinds)]
    return n.assign(_t=(n["disabled_toilet"] != "Y")).sort_values(["_t", "distance_km"]).drop(columns="_t").to_dict("records")



ENTRY_MARK = {"가능": "✓", "주의": "!", "정보 없음": "?"}
KTO_CALL = ["지금도 영업하시나요?", "휠체어로 들어갈 수 있나요?"]


def kto_source(fetched: str | None = None) -> str:
    """관광공사 무장애 식당 출처 문구 (받은 날)"""
    return f"{kto_data.SOURCE}({fetched} 받음)" if fetched else kto_data.SOURCE


def kto_items(df: pd.DataFrame, profile: TravelerProfile, rules: dict) -> list[dict]:
    """관광공사 식당 → PlaceItem 목록. 화장실 '있음'이거나 출입 문장 있는 곳만
    화장실 꼭 필요한데 '없음' → 부적합, 출입 어렵다고 적혔으면 '주의'
    """
    need_toilet = "acc_toilet" in needed_facilities(profile)
    out: list[dict] = []
    for r in df.to_dict("records"):
        if r["toilet"] != "있음" and r["entry"] == "정보 없음":
            continue
        block = need_toilet and r["toilet"] == "없음"
        caution = r["entry"] == "주의" and profile.mobility != "none"
        reasons = ({"가능": ["휠체어로 들어갈 수 있다고 적힌 곳"], "주의": ["출입이 어렵다고 적힌 곳(방문 전 확인)"]}.get(r["entry"], [])
                   + (["장애인 화장실 있음"] if r["toilet"] == "있음" else [])
                   + (["장애인 화장실이 없다고 적힘"] if block else []))
        out.append(PlaceItem(
            item_id=f"K{r['contentid']}", name=r["name"], kind=r["kind"], category=None,
            verdict="부적합" if block else "주의" if caution else "적합", reasons=reasons,
            facilities=[{"key": "acc_toilet", "label": FAC_KO["acc_toilet"], "state": r["toilet"],
                         "mark": {"있음": "✓", "없음": "✗"}.get(r["toilet"], "?"), "evidence": r["toilet_ev"] or None,
                         "needed": need_toilet},
                        {"key": "entry", "label": "출입", "state": r["entry"], "mark": ENTRY_MARK[r["entry"]],
                         "evidence": r["entry_ev"] or None, "needed": profile.mobility != "none"}],
            lat=float(r["lat"]), lng=float(r["lng"]),
            source=kto_source(r.get("fetched")), data_date=r.get("data_date"),
            stale=stale(r.get("data_date"), rules["stale_years"]),
            call_script=list(KTO_CALL) + (["장애인 화장실이 있나요?"] if r["toilet"] != "있음" else [])).model_dump())
    return out

def tag_date() -> str:
    """비짓제주 태그 받은 날 (조사일이 없어서 받은 날을 기준일로 씀)"""
    d = catalog()["datasets"].get("vj_tags", {})
    return f"{d['fetched']} 받음" if d.get("fetched") else "받은 날 미기록"


def tag_source() -> str:
    """비짓제주 태그 출처 문구"""
    return f"비짓제주 관광정보 태그({tag_date()})"


def place_answer(row, fac: str, label: str, dataset: str) -> tuple[dict, EvidenceTable, str]:
    """장소 하나 물어보기: 물어본 시설 상태(있음·없음·정보 없음) + 근거 + 기준일
    정보 없음은 '없다'고 하면 안 됨
    """
    v = row.get(fac)
    state, mark = FAC_STATE.get(int(v), ("정보 없음", "?")) if _isnum(v) else ("정보 없음", "?")
    ev = row.get(f"ev_{fac}")
    ev = ev if isinstance(ev, str) and ev else None
    when = date_cell(row)
    tag = state != "정보 없음" and is_tag_evidence(ev)   # 조사 칸은 빈칸인데 비짓제주 태그로 채운 값
    item = {"key": fac, "label": FAC_KO[fac], "state": state, "mark": mark, "evidence": ev, "needed": True}
    t = EvidenceTable(id="A5", title=f"{row['name']}: {FAC_KO[fac]}", dataset=tag_source() if tag else dataset,
                      data_label=label,  # type: ignore[arg-type]
                      columns=["장소", "항목", "자료 상태", "원문 근거", "자료 기준일"],
                      rows=[[row["name"], FAC_KO[fac], state, ev or "-", tag_date() if tag else when]],
                      note=("조사 자료(자료 기준일 " + when + ")에는 이 칸이 비어 있어 비짓제주 태그에 적힌 값을 썼습니다. "
                            "태그에 없다고 '없음'으로 보지는 않습니다.") if tag else
                           "'정보 없음'은 조사 자료에 언급이 없다는 뜻이며, 시설이 없다는 뜻이 아닙니다.")
    if state == "정보 없음":
        text = (f"{row['name']}의 {FAC_KO[fac]} 여부는 조사 자료에 언급이 없습니다. 없다는 뜻은 아니니 방문 전 전화로 확인하세요."
                f" (자료 기준일 {when})")
    elif tag:
        text = (f"{row['name']}의 {FAC_KO[fac]}: 무장애 조사 자료에는 언급이 없고, 비짓제주 관광정보 태그에 "
                f"{josa(FAC_KO[fac], '이')} 적혀 있습니다({tag_date()}). 지금은 바뀌었을 수 있으니 방문 전 확인하세요.")
    else:
        text = f"{row['name']}의 {FAC_KO[fac]}: 조사 당시 '{state}'으로 기록돼 있습니다 (자료 기준일 {when})."
        if ev:
            text += f" 원문 근거: \"{ev}\""
        text += " 지금은 바뀌었을 수 있으니 방문 전 확인하세요."
    return item, t, text


def evidence_text(row) -> str:
    """근거표 '근거' 칸: 문제 이유 먼저, 그다음 도움 되는 시설 (최대 3개)"""
    items = [t for _, t in row["reasons"]] + list(row["good"])
    return " · ".join(items[:3]) or "기준을 넘는 기록 없음"


def date_cell(row) -> str:
    """근거표 '자료 기준일' 칸: 2020-12-22 / 2021-02-09 이전(등록일) / 모름
    등록일은 데이터허브에 올라간 날이라(2019 자료도 2021-02-09 일괄 등록) '이전'으로 씀
    """
    d = row.get("data_date")
    if not isinstance(d, str) or not d:
        return "모름"
    return f"{d} 이전(등록일)" if _text(row.get("date_basis")) == "등록일" else d


def stale(data_date, years: int, today: date | None = None) -> bool:
    if not isinstance(data_date, str) or not data_date:
        return True
    try:
        y, m, d = (int(x) for x in data_date[:10].split("-"))
    except ValueError:
        return True
    today = today or date.today()
    return (today - date(y, m, d)).days > 365.25 * years


class AccessAgent(BaseAgent):
    name = "access"
    title = "접근성 진단"
    description = ("이용자 조건(이동 방법·감각·필요 시설)에 맞춰 무장애 관광지를 적합·주의·부적합으로 판정하고 근거를 붙인다. "
                   "적합 장소가 적으면 '주의' 장소(동반자 도움·방문 전 확인 필요)를 함께 보여 줄지, 지역 밖 가까운 곳까지 볼지 정한다.")
    required = ("screen",)

    def _run(self, req: AnalysisRequest, ctx: AgentContext) -> AgentResult:
        if req.intent == Intent.NEARBY:
            return self.nearby_search(req, ctx)
        opts = req.params.get("opts", {})
        return self.screen(req, ctx, include_caution=opts.get("include_caution"), extend_km=int(opts.get("extend_km", 0)))

    def _llm_on(self, req: AnalysisRequest, ctx: AgentContext) -> bool:
        # 근처 식당·숙소는 고를 조건이 없어서 규칙으로만 돌림
        return req.intent != Intent.NEARBY and super()._llm_on(req, ctx)

    # ── 근처 식당·카페·숙소 (nearby) ──
    def nearby_search(self, req: AnalysisRequest, ctx: AgentContext) -> AgentResult:
        """기준 장소 근처에서 휠체어 진입 가능으로 조사된 식당·카페·숙소 (부적합은 뺌)"""
        rules = access_rules()
        kinds = req.params.get("nearby_kinds") or ["restaurant", "cafe", "lodging"]
        kind_txt = "·".join(KIND_KO[k] for k in kinds)
        has_near = set(ctx.repo.nearby_df()["place_id"])
        places = ctx.repo.places()
        asked = list(req.place_ids)
        note = None
        if asked:
            ids = [p for p in asked if p in has_near]
            if not ids:   # 일반 관광지: 반경 안 가까운 무장애 코스 주변으로 대신 안내
                me = places[places["place_id"] == asked[0]]
                cand = places[places["place_id"].isin(has_near)].dropna(subset=["lat", "lng"])
                ids = []
                if not me.empty and pd.notna(me.iloc[0]["lat"]) and not cand.empty:
                    km = haversine_km(float(me.iloc[0]["lat"]), float(me.iloc[0]["lng"]),
                                      cand["lat"].astype(float), cand["lng"].astype(float))
                    near_c = cand.assign(km=km)[km <= NEARBY_FALLBACK_KM].nsmallest(NEARBY_FALLBACK_N, "km")
                    ids = near_c["place_id"].tolist()
                name = req.params.get("anchor_name") or (me.iloc[0]["name"] if not me.empty else asked[0])
                note = (f"{name} 주변 가게는 조사 자료에 없어, {NEARBY_FALLBACK_KM:g}km 안의 가까운 무장애 코스 주변을 대신 보여 드립니다."
                        if ids else f"{name} 주변 가게는 조사 자료에 없고, {NEARBY_FALLBACK_KM:g}km 안에 주변정보가 있는 무장애 코스도 없습니다.")
            base = self.screen(req.model_copy(update={"place_ids": ids or asked, "region": None}), ctx, include_caution=True)
        else:
            base = self.screen(req, ctx, include_caution=True)
        verdicts = base.payload.get("verdicts", [])
        if asked:
            anchors = [v for v in verdicts if v["place_id"] in has_near]
        else:   # 질문 지역에서 조건 맞는 코스 전부. 가게는 아래에서 NEARBY_SHOW곳으로 줄임
            anchors = [v for v in verdicts if v["place_id"] in has_near and v["verdict"] != "부적합"]
        dataset = base.evidence[0].dataset if base.evidence else NEARBY_SOURCE
        by_card = {c["place_id"]: c for c in base.payload.get("cards", [])}
        cards = []
        for v in anchors:   # 기준 장소 판정이랑 상관없이 가게는 따로 봄 (가게 진입 여부는 가게 조사값)
            c = by_card.get(v["place_id"]) or {
                "place_id": v["place_id"], "name": v["name"], "category": v.get("category"), "verdict": v["verdict"],
                "reasons": v.get("reasons") or [], "facilities": [], "source": dataset, "data_date": v.get("data_date"),
                "stale": stale(v.get("data_date"), rules["stale_years"]), "call_script": []}
            cards.append({**c, "nearby": nearby_rows(ctx.repo.nearby_df(), v["place_id"], kinds)})
        items = place_items(cards, {v["place_id"]: (v.get("lat"), v.get("lng")) for v in anchors}, req.profile, rules)
        found = [i for i in items if i["kind"] != "attraction" and i["kind"] in kinds]
        ok = [i for i in found if i["verdict"] != "부적합"]
        n_block = len(found) - len(ok)
        if not asked:   # 지역 전체: 화장실 있는 곳 → 가까운 순으로 NEARBY_SHOW곳
            ok = sorted(ok, key=lambda i: (not (i["facilities"] and i["facilities"][0]["state"] == "있음"),
                                           i["distance_km"] if i["distance_km"] is not None else 99))[:NEARBY_SHOW]
            keep = {i["anchor_id"] for i in ok}
            anchors = [v for v in anchors if v["place_id"] in keep]
            cards = [c for c in cards if c["place_id"] in keep]
            items = [i for i in items if i["kind"] != "attraction" or i["item_id"] in keep]
        label = (base.data_labels or ["실데이터"])[0]
        anchor_name = {v["place_id"]: v["name"] for v in anchors}
        where = (f"{req.params.get('anchor_name') or anchor_name.get(asked[0], asked[0])} 근처" if asked and not note else
                 "가까운 무장애 코스 근처" if asked else f"{req.region or '제주 전체'} 무장애 코스 근처")
        payload = {**base.payload, "shown_ids": [v["place_id"] for v in anchors], "cards": cards,
                   "items": [i for i in items if i["kind"] == "attraction"] + ok, "nearby_kinds": kinds}
        common = dict(agent=self.name, payload=payload, data_labels=[label],  # type: ignore[list-item]
                      limitations=["무장애여행코스 주변만 조사된 자료라, 다른 관광지 주변 가게는 자료에 없습니다.",
                                   "맛·가격·평점·영업시간은 자료에 없습니다. '휠체어 진입 가능'은 조사 당시 기준입니다."])
        if not ok:
            why = note or (f"장애인 화장실이 없다고 조사된 {n_block}곳만 있어 모두 뺐습니다." if n_block else
                           f"이 지역 무장애 코스 주변에 조사된 {kind_txt}이(가) 없습니다.")
            return AgentResult(status="insufficient", confidence="low", summary=f"{where}에서 조건에 맞는 {kind_txt}을(를) 찾지 못했습니다. {why}",
                               follow_ups=["제주 전체에서 찾아줘", "숙소만 보여줘"], **common)
        n1 = EvidenceTable(id="N1", title=f"근처 {kind_txt} (휠체어 진입 가능으로 조사된 곳)", dataset=NEARBY_SOURCE, data_label=label,  # type: ignore[arg-type]
                           columns=["기준 장소", "종류", "이름", "거리(km)", "장애인 화장실", "전화", "조사일"],
                           rows=[[anchor_name.get(i["anchor_id"], i["anchor_id"]), KIND_KO[i["kind"]], i["name"], i["distance_km"],
                                  i["facilities"][0]["state"] if i["facilities"] else "정보 없음", i["phone"] or "정보 없음",
                                  i["data_date"] or "모름"] for i in ok],
                           note="거리는 코스에서 가게까지의 조사 거리입니다. 영업 여부는 바뀌었을 수 있어 전화로 확인하세요.")
        per = {}
        for i in ok:
            p = per.setdefault(i["anchor_id"], [0, 0, 0])
            p[0] += 1
            p[1] += bool(i["facilities"]) and i["facilities"][0]["state"] == "있음"
            p[2] += i["phone"] is None
        n2 = EvidenceTable(id="N2", title="기준 장소별 근처 가게 수", dataset=NEARBY_SOURCE, data_label=label,  # type: ignore[arg-type]
                           columns=["기준 장소", "고른 곳", "장애인 화장실 있음", "전화번호 없음"],
                           rows=[[anchor_name.get(k, k), *v] for k, v in per.items()])
        n_toilet = sum(v[1] for v in per.values())
        n_nophone = sum(v[2] for v in per.values())
        findings = []
        if note:
            findings.append(Finding(text=note, evidence_ids=["N2"]))
        findings.append(Finding(text=f"{where}에서 휠체어로 들어갈 수 있다고 조사된 {kind_txt} {len(ok)}곳을 찾았습니다.",
                                evidence_ids=["N1"], numbers={"n": len(ok)}))
        findings.append(Finding(text=f"그중 장애인 화장실이 있다고 조사된 곳은 {n_toilet}곳입니다.", evidence_ids=["N1"],
                                numbers={"toilet": n_toilet}))
        if n_block:
            findings.append(Finding(text=f"장애인 화장실이 필요하다는 조건에 맞지 않는 {n_block}곳은 뺐습니다.", evidence_ids=["N1"],
                                    numbers={"block": n_block}))
        if n_nophone:
            findings.append(Finding(text=f"{n_nophone}곳은 전화번호가 자료에 없어 방문 전 확인이 어렵습니다.", evidence_ids=["N2"],
                                    numbers={"no_phone": n_nophone}))
        return AgentResult(
            status="ok", confidence="medium", summary=f"{where}에서 휠체어 진입 가능으로 조사된 {kind_txt} {len(ok)}곳을 찾았습니다.",
            findings=findings, evidence=[n1, n2],
            warnings=[AgentWarning(code="STALE_DATA", message="주변 가게는 2021-12-22 조사 자료라 폐업·이전했을 수 있습니다. 가기 전에 전화로 영업 여부를 확인하세요.")],
            follow_ups=["이 근처로 1일 코스 짜줘", "숙소만 보여줘", "카페만 보여줘"], **common)

    # ── 계산 ──
    def screen(self, req: AnalysisRequest, ctx: AgentContext, include_caution: bool | None = None,
               extend_km: int = 0) -> AgentResult:
        rules = access_rules()
        places = ctx.repo.places()
        if uses(req, "vj_tags"):   # 탐색이 비짓제주 자료 골랐을 때만 빈칸을 태그로 채움 (있음만)
            places = fill_blank_facilities(places, ctx.repo.visitjeju_df())
        try:
            points = ctx.repo.get("place_points").df
        except DataNotReady:
            points = None
        pool = filter_places(places, req).assign(outside_km=None)
        if extend_km and req.region and region_center(req.region):
            la, lo = region_center(req.region)  # type: ignore[misc]
            rest = places[~places["place_id"].isin(pool["place_id"])].dropna(subset=["lat", "lng"])
            km = haversine_km(la, lo, rest["lat"].astype(float), rest["lng"].astype(float))
            near = rest[km <= extend_km].assign(outside_km=km[km <= extend_km].round(0))
            pool = pd.concat([pool, near], ignore_index=True)
            self.step("extend", f"지역 밖 {extend_km}km 안 {len(near)}곳 추가")
        if pool.empty:
            where = req.region or "선택한 장소"
            return AgentResult(agent=self.name, status="insufficient", confidence="low",
                               summary=f"{where}에는 무장애 관광지 자료가 없습니다.",
                               warnings=[AgentWarning(code="NO_PLACES", message=f"{where}에 해당하는 관광지 자료가 없습니다.", severity="block")],
                               follow_ups=["제주 전체에서 찾아줘"])
        # 태그로 채운 칸은 이번에 판정한 장소(pool)만 셈
        tag_cells = {c: int(pool[f"ev_{c}"].map(is_tag_evidence).sum()) for c in FAC_KO if f"ev_{c}" in pool}
        filled = sum(tag_cells.values())
        if filled:
            n_pl = int(pool[[f"ev_{c}" for c in tag_cells]].apply(lambda col: col.map(is_tag_evidence)).any(axis=1).sum())
            self.step("fill:visitjeju", f"이번 장소 {len(pool)}곳 중 {n_pl}곳, 조사 빈칸 {filled}칸을 비짓제주 태그로 보완(있음만)")
        a = assess(pool, req.profile, rules, points)
        n_ok, n_c, n_b = (int((a["verdict"] == v).sum()) for v in ("적합", "주의", "부적합"))
        self.step("assess", f"{len(a)}곳 판정: 적합 {n_ok}·주의 {n_c}·부적합 {n_b}")
        auto = include_caution is None
        if auto:
            include_caution = n_ok < rules["min_suitable"]
            if include_caution:
                self.step("rule:include_caution", f"적합 {n_ok}곳 < {rules['min_suitable']} → 주의 포함")
        place_q = req.params.get("place_q")   # 수정 대화: '○○ 화장실 있어?' (장소 하나)
        shown = a if place_q else a[(a["verdict"] == "적합") | ((a["verdict"] == "주의") & bool(include_caution))]
        label = ctx.repo.get("places").label
        dataset = base_dataset = ctx.repo.get("places").source
        if filled:   # 비짓제주 태그 섞였으면 근거표 출처에도 적음
            dataset = f"{dataset} + 비짓제주 관광정보 태그(조사 빈칸 보완, {tag_date()})"
        # 근거표 제목엔 판정에 쓴 조건만 (이동 방법·이동 시간·관심사는 코스 조건이라 뺌)
        cond = ", ".join(x for x in req.profile.describe()
                         if not (x.endswith(" 이동") or x.startswith(("한 번에", "관심")))) or "이동 불편 없음"
        where = req.region or "제주 전체"

        def place_cell(r) -> str:
            loc = r.get("emd") if isinstance(r.get("emd"), str) else (r.get("region") or "")
            if _isnum(r.get("outside_km")):
                loc = f"{loc}(지역 밖 {int(r['outside_km'])}km)"
            return loc

        a1 = EvidenceTable(id="A1", title=f"조건에 맞는 장소 ({cond})", dataset=dataset, data_label=label,
                           columns=["장소", "읍면동", "판정", "점수", "정보 없음", "근거", "자료 기준일"],
                           rows=[[r["name"], place_cell(r), r["verdict"], int(r["score"]), gap_text(r), evidence_text(r),
                                  date_cell(r)] for _, r in shown.head(MAX_ROWS).iterrows()],
                           note="판정은 자료에 기록된 지점 기준입니다. '적합'도 현장 상황이 바뀌었을 수 있어 방문 전 확인이 필요합니다. "
                                "'정보 없음'은 이 조건에서 확인할 시설 중 자료에 언급이 없는 수이며 점수에는 넣지 않았습니다.")
        counts: dict[str, int] = {}
        texts: dict[str, str] = {}
        for _, r in a.iterrows():
            for code, _ in r["reasons"]:
                counts[code] = counts.get(code, 0) + 1
                texts.setdefault(code, _reason_label(code))
        a2 = EvidenceTable(id="A2", title="판정 이유 집계 (장소 수)", dataset=dataset, data_label=label,
                           columns=["이유", "장소 수"],
                           rows=[[texts[c], n] for c, n in sorted(counts.items(), key=lambda kv: -kv[1])] or [["해당 없음", 0]])
        need = needed_facilities(req.profile)
        check = checked_facilities(req.profile)
        evidence = [a1, a2]
        gap_rows = []
        for f in check:
            col = pd.to_numeric(a[f], errors="coerce") if f in a else pd.Series(dtype=float)
            gap_rows.append([FAC_KO[f], int((col == 1).sum()), int((col == 0).sum()), int(col.isna().sum())])
        if gap_rows:
            evidence.append(EvidenceTable(id="A3", title="시설 정보 확인 현황 (장소 수)", dataset=dataset, data_label=label,
                                          columns=["항목", "있음 확인", "없음 확인", "정보 없음"], rows=gap_rows,
                                          note="'정보 없음'은 조사 자료에 언급이 없다는 뜻이며, 시설이 없다는 뜻이 아닙니다."
                                               + (" 조사 칸이 비어 있고 비짓제주 태그에 그 시설이 적힌 곳은 '있음'에 넣었습니다"
                                                  f"(이 표의 항목 중 태그로 채운 칸 {n_tag}개, 태그에 없다고 '없음'으로 보지는 않음)."
                                                  if (n_tag := sum(tag_cells.get(f, 0) for f in check)) else "")))
        cov = coverage(a)
        evidence.append(EvidenceTable(id="A4", title=f"읍면동별 조건 충족 ({cond})", dataset=dataset, data_label=label,
                                      columns=["읍면동", "관광지 수", "적합", "주의", "부적합", "정보 없음 비율"],
                                      rows=[[r.group, int(r.n), int(r.ok), int(r.caution), int(r.block),
                                             None if pd.isna(r.gap) else float(r.gap)] for r in cov.head(12).itertuples()],
                                      note="정보 없음 비율 = 이 조건에서 확인할 시설 중 자료에 언급이 없는 비율(읍면동 안 모든 장소 합계)."))

        findings = []
        top = shown.head(3)
        if not top.empty:   # 어디가 좋은지를 맨 앞에
            findings.append(Finding(text="점수가 높은 곳은 " + ", ".join(f"{r['name']}({r['verdict']}, {int(r['score'])}점)" for _, r in top.iterrows()) + "입니다.",
                                    evidence_ids=["A1"]))
        findings.append(Finding(text=f"전체 {len(a)}곳을 살펴 부적합 {n_b}곳은 뺐습니다(이유: 근거표 A2)." if n_b else
                                f"전체 {len(a)}곳을 살펴봤고, 부적합으로 뺀 곳은 없습니다.",
                                evidence_ids=["A1"], numbers={"total": len(a), "block": n_b}))
        if counts:
            code, n = max(counts.items(), key=lambda kv: kv[1])
            findings.append(Finding(text=f"가장 많은 판정 이유는 '{texts[code]}'({n}곳)입니다.", evidence_ids=["A2"], numbers={"n": n}))
        if len(cov) > 1 and cov["ok"].max() >= 2:
            best = cov.iloc[0]
            findings.append(Finding(text=f"조건에 맞는 적합 장소가 가장 많은 곳은 {best.group}({int(best.ok)}곳)입니다.",
                                    evidence_ids=["A4"], numbers={"ok": int(best.ok)}))
        elif len(cov) > 1 and n_ok:
            m = int((cov["ok"] > 0).sum())
            findings.append(Finding(text=f"적합 장소 {n_ok}곳은 {m}개 읍면동에 흩어져 있어, 한 지역에 모여 있지 않습니다.",
                                    evidence_ids=["A4"], numbers={"ok": n_ok, "m": m}))
        big = cov[(cov["n"] >= 2) & cov["gap"].notna() & (cov["gap"] > 0)]
        if len(cov) > 1 and not big.empty:
            g = big.sort_values(["gap", "n"], ascending=[False, False]).iloc[0]
            findings.append(Finding(text=f"정보 공백이 가장 큰 곳은 {g.group}(정보 없음 비율 {g.gap:.0%})입니다. 방문 전 확인이 특히 필요합니다.",
                                    evidence_ids=["A4"], numbers={"gap": float(g.gap)}))
        if include_caution and n_c:
            why = f"적합한 곳이 {n_ok}곳뿐이라 " if auto else ""
            findings.append(Finding(text=f"{why}'주의' 장소 {n_c}곳도 함께 보여 드립니다. 동반자 도움이나 방문 전 확인이 필요한 곳입니다.",
                                    evidence_ids=["A1"], numbers={"ok": n_ok, "caution": n_c}))
        n_comp = int(sum(any(c == "companion" for c, _ in rs) for rs in a["reasons"]))
        if n_comp:
            findings.append(Finding(text=f"{len(a)}곳 중 {n_comp}곳은 조사 자료에 '보호자·동행인과 함께 가면 추천'으로 적혀 있습니다. "
                                         "동행인과 함께 가는 일정으로 보세요.",
                                    evidence_ids=["A2"], numbers={"total": len(a), "companion": n_comp}))
        warnings = []
        for f in need:
            unk = int(pd.to_numeric(a[f], errors="coerce").isna().sum()) if f in a else len(a)
            if unk:
                findings.append(Finding(text=f"{josa(FAC_KO[f], '은')} {len(a)}곳 중 {unk}곳이 자료에 언급이 없어 방문 전 확인이 필요합니다.",
                                        evidence_ids=["A3"], numbers={"unknown": unk, "total": len(a)}))
            if len(a) and unk / len(a) > 0.5:
                warnings.append(AgentWarning(code="INFO_GAP", message=f"{FAC_KO[f]} 정보가 없는 장소가 절반이 넘습니다({unk}/{len(a)}곳)."))
        n_stale = int(sum(stale(d, rules["stale_years"]) for d in a["data_date"]))
        if n_stale:
            warnings.append(AgentWarning(code="STALE_DATA",
                                         message=f"{len(a)}곳 중 {n_stale}곳은 자료 기준일(조사 기준일자 또는 데이터허브 등록일)이 "
                                                 f"{rules['stale_years']}년 넘게 지났거나 모릅니다. 현장 시설이 바뀌었을 수 있습니다."))
        if len(shown) < rules["min_suitable"]:
            warnings.append(AgentWarning(code="FEW_CANDIDATES", message=f"조건에 맞는 장소가 {len(shown)}곳뿐입니다. 지역을 넓히거나 조건을 바꿔 보세요."))
        no_xy = int(a["lat"].isna().sum())
        if no_xy:
            warnings.append(AgentWarning(code="NO_COORDS", severity="info", message=f"{no_xy}곳은 좌표가 없어 지도·코스에서 빠집니다."))
        confidence = "low" if len(shown) < rules["min_suitable"] else ("medium" if n_stale or warnings else "high")
        payload_rows = a[["place_id", "name", "verdict", "score", "lat", "lng", "indoor", "acc_toilet", "fee_won", "region",
                          "emd", "data_date", "category", "unknown_n", "checked_n"]].copy()
        payload_rows["reasons"] = [[t for _, t in rs] for rs in a["reasons"]]
        # 카드: 적합·주의 전부 (코스가 다시 씀). 화면은 shown_ids 순서로 앞 CARD_MAX장만 펼침
        near_ok = uses(req, "bf_nearby")
        def card_source(r) -> str:   # 태그로 채운 시설이 있을 때만 비짓제주 붙임
            return dataset if any(is_tag_evidence(r.get(f"ev_{c}")) for c in FAC_KO) else base_dataset

        cards = [place_card(r, req.profile, rules, points, card_source(r), ctx.repo.nearby(r["place_id"]) if near_ok else [])
                 for _, r in (a if place_q else a[a["verdict"] != "부적합"]).iterrows()]
        if place_q and not a.empty:
            asked, a5, summary = place_answer(a.iloc[0], place_q, label, dataset)
            for c in cards:
                if place_q not in {f["key"] for f in c["facilities"]}:
                    c["facilities"].insert(0, asked)
            r0 = a.iloc[0]
            why = [t for _, t in r0["reasons"]]
            findings = [Finding(text=summary, evidence_ids=["A5"])]
            if why:
                findings.append(Finding(text=f"이 조건에서 '{r0['verdict']}'로 본 이유: {'; '.join(why[:3])}.", evidence_ids=["A1"]))
            evidence = [a5, a1]
            warnings = [w for w in warnings if w.code == "STALE_DATA"]
            confidence = "medium"
        payload_rows["good"] = a["good"]
        return AgentResult(
            agent=self.name, status="ok" if not shown.empty else "partial", confidence=confidence,  # type: ignore[arg-type]
            summary=(f"{a.iloc[0]['name']}: 이 조건에서 판정은 '{a.iloc[0]['verdict']}'입니다." if place_q and not a.empty else
                     f"{where}에서 고른 조건에 맞는 장소는 적합 {n_ok}곳, 주의 {n_c}곳입니다."),   # 조건은 화면·보고서 위에 이미 있음
            findings=findings, evidence=evidence, warnings=warnings,
            limitations=["무장애 조사 자료(2019~2021년 등록) 기준이라 지금 시설과 다를 수 있습니다. 방문 전 전화로 확인하세요.",
                         "'정보 없음'은 자료에 언급이 없다는 뜻이지 시설이 없다는 뜻이 아닙니다.",
                         "경사·계단은 조사 지점에 기록된 것만 반영합니다. 조사하지 않은 구간은 판단하지 않습니다."]
                        + (["발달·지적장애 이용 추천 여부는 무장애여행코스 조사에만 있습니다. 그 밖의 관광지는 조사되지 않아 판정에 쓰지 않았습니다."]
                           if req.profile.cognition != "none" else []),
            follow_ups=([f"{a.iloc[0]['name']} {w} 있어?" for k, w in (("acc_parking", "주차"), ("acc_toilet", "화장실"),
                                                                      ("elevator", "엘리베이터")) if k != place_q][:2]
                        if place_q and not a.empty else ["이 곳들로 2일 코스 짜줘", "실내 장소 위주로 다시 보여줘", "보고서로 정리해줘"]),
            payload={"verdicts": payload_rows.to_dict("records"), "shown_ids": shown["place_id"].tolist(),
                     "n_ok": n_ok, "n_caution": n_c, "n_block": n_b, "include_caution": bool(include_caution),
                     "extend_km": extend_km, "cards": cards, "place_q": place_q,
                     "items": place_items(cards, {r["place_id"]: (r["lat"], r["lng"]) for _, r in a.iterrows()}, req.profile, rules),
                     "coverage": cov.drop(columns=["unk", "chk"]).to_dict("records")},
            data_labels=[label])  # type: ignore[list-item]

    # ── LLM 판단 도구 ──
    def tools(self, req: AnalysisRequest, ctx: AgentContext) -> list[AgentTool]:
        rules = access_rules()
        state: dict = {}

        def run(args: dict) -> AgentResult:
            r = self.screen(req, ctx, include_caution=args.get("include_caution"), extend_km=int(args.get("extend_km", 0)))
            if not args:
                state["base"] = r.payload if r.status in ("ok", "partial") else {"n_ok": 0, "n_caution": 0}
            return r

        def check(args: dict) -> str | None:
            if not args:
                return None
            base = state.get("base")
            if base is None:
                return "조건을 바꾸기 전에 기본 조건(인자 없이)으로 먼저 실행해 결과를 봐야 합니다."
            if args.get("include_caution") is True and base["n_ok"] >= rules["min_suitable"]:
                return f"적합 장소가 {base['n_ok']}곳으로 충분해 '주의' 장소를 더할 근거가 없습니다."
            ext = args.get("extend_km", 0)
            if ext:
                if not req.region:
                    return "지역을 정하지 않은 질문이라 지역 밖으로 넓힐 수 없습니다(이미 제주 전체)."
                if base["n_ok"] + base["n_caution"] >= rules["min_suitable"]:
                    return f"지역 안 후보(적합+주의 {base['n_ok'] + base['n_caution']}곳)가 충분해 지역 밖으로 넓힐 근거가 없습니다."
            return None

        def observe(r: AgentResult) -> dict:
            return {**default_observe(r), "counts": {k: r.payload.get(k) for k in ("n_ok", "n_caution", "n_block")},
                    "include_caution": r.payload.get("include_caution"), "extend_km": r.payload.get("extend_km")}

        return [AgentTool(
            name="screen",
            description="조건에 맞는 장소를 판정한다. 인자 없이 부르면 기본(적합이 3곳 미만일 때만 주의 포함, 지역 안).",
            params={"include_caution": {"type": "boolean", "description": "'주의' 장소도 보여 줄지"},
                    "extend_km": {"type": "integer", "enum": list(EXTEND_KM), "description": "지역 중심에서 이 거리(km) 안의 지역 밖 장소까지 볼지"}},
            run=run, check=check, observe=observe)]


_REASON_LABELS = {"slope_block": "경사 기준 초과 기록", "slope_steep": "경사 기준 초과 1곳", "slope_caution": "경사 주의 구간 기록", "barrier_share": "계단·접근불가 지점 많음",
                  "stairs": "계단 지점", "threshold": "턱·단차 지점", "inaccessible": "접근불가 지점", "rough": "노면 불량 지점",
                  "survey_no": "조사자 비추천", "companion": "보호자·동행인 동반 시 추천(조사자 의견)"}


def _reason_label(code: str) -> str:
    if code in _REASON_LABELS:
        return _REASON_LABELS[code]
    fac, _, kind = code.rpartition("_")
    return f"{FAC_KO.get(fac, fac)} {'없음 확인' if kind == 'none' else '정보 없음'}"
