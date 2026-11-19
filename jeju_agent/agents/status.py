"""현황 분석 에이전트 (기관·정책용): 지역별 무장애 시설 비율, 정보 공백, 보완 우선 지역
LLM은 묶는 단위랑 강조할 항목만 고름

비율은 '확인된 곳' 기준이라 정보 없음 많으면 낮게 나옴 → 정보 공백도 같이 보여 줌
"""
from __future__ import annotations

import pandas as pd

from ..config import access_rules
from ..contracts import AgentResult, AgentWarning, AnalysisRequest, EvidenceTable, Finding
from ..data.center import nearest_stops
from ..data.regions import CITIES
from ..text import josa
from .access import FAC_KO, stale
from .base import AgentContext, AgentTool, BaseAgent, default_observe, filter_places, uses

GROUPS = {"region": "시", "emd": "읍면동", "category": "분류"}
FOCUS = {"all": None, "toilet": "acc_toilet", "parking": "acc_parking", "rental": "wheelchair_rental", "slope": "steep"}
ITEMS = ("acc_toilet", "acc_parking", "wheelchair_rental", "stroller_rental", "ramp", "elevator", "braille", "audio_guide")
MIN_GROUP = 3


GROUP_WORDS = (("category", ("분류별", "종류별", "유형별", "분류로", "종류로")), ("emd", ("읍면동별", "동별", "읍면별")),
               ("region", ("시별", "제주시와 서귀포시", "시 단위")))


def default_group(req: AnalysisRequest) -> str:
    """질문에 '분류별·읍면동별·시별' 있으면 그거, 없으면 지역 크기 보고 고름"""
    q = req.question.replace(" ", "")
    for key, words in GROUP_WORDS:
        if any(w.replace(" ", "") in q for w in words):
            return key
    if req.region in CITIES:
        return "emd"
    if req.region:
        return "category"
    return "region"


def summarize_table(places: pd.DataFrame, group_by: str, steep_deg: float) -> pd.DataFrame:
    df = places.copy()
    df[group_by] = df[group_by].fillna("미상")
    for f in ("acc_toilet", "acc_parking", "wheelchair_rental"):
        df[f] = pd.to_numeric(df[f], errors="coerce")
    df["steep"] = pd.to_numeric(df["max_slope_deg"], errors="coerce") > steep_deg
    df["barrier"] = (df["n_stairs"] + df["n_inaccessible"]) > 0
    g = df.groupby(group_by)
    out = pd.DataFrame({
        "n": g.size(),
        "toilet": g["acc_toilet"].apply(lambda s: int((s == 1).sum())),
        "parking": g["acc_parking"].apply(lambda s: int((s == 1).sum())),
        "rental": g["wheelchair_rental"].apply(lambda s: int((s == 1).sum())),
        "steep": g["steep"].sum().astype(int),
        "barrier": g["barrier"].sum().astype(int),
    }).reset_index().rename(columns={group_by: "group"})
    out["toilet_ratio"] = (out["toilet"] / out["n"]).round(2)
    out["steep_ratio"] = (out["steep"] / out["n"]).round(2)
    return out


class StatusAgent(BaseAgent):
    name = "status"
    title = "무장애 관광 현황"
    description = ("무장애 관광지의 시설 확인 현황·정보 공백·자료 노후도를 지역별로 집계해 정책·사업계획서 근거를 만든다. "
                   "묶는 단위(시·읍면동·분류)와 강조할 항목(화장실·주차·대여·경사)을 정한다.")
    required = ("summarize",)

    def _run(self, req: AnalysisRequest, ctx: AgentContext) -> AgentResult:
        opts = req.params.get("opts", {})
        return self.summarize(req, ctx, group_by=opts.get("group_by"), focus=opts.get("focus", "all"))

    def summarize(self, req: AnalysisRequest, ctx: AgentContext, group_by: str | None = None, focus: str = "all") -> AgentResult:
        rules = access_rules()
        steep_deg = rules["slope"]["wheelchair_manual"]["caution"]
        places = filter_places(ctx.repo.places(), req)
        where = req.region or "제주 전체"
        if places.empty:
            return AgentResult(agent=self.name, status="insufficient", confidence="low", summary=f"{where}에는 무장애 관광지 자료가 없습니다.",
                               warnings=[AgentWarning(code="NO_PLACES", message=f"{where}에 해당하는 관광지 자료가 없습니다.", severity="block")])
        group_by = group_by or default_group(req)
        ds = ctx.repo.get("places")
        t = summarize_table(places, group_by, steep_deg)
        key = FOCUS.get(focus)
        if key == "steep":
            t = t.sort_values(["steep_ratio", "n"], ascending=[False, False])
        elif key:
            col = {"acc_toilet": "toilet", "acc_parking": "parking", "wheelchair_rental": "rental"}[key]
            t = t.assign(_r=t[col] / t["n"]).sort_values(["_r", "n"], ascending=[True, False]).drop(columns="_r")
        else:
            t = t.sort_values(["n", "group"], ascending=[False, True])
        gname = GROUPS[group_by]
        s1 = EvidenceTable(id="S1", title=f"{where} {gname}별 무장애 관광지 현황", dataset=ds.source, data_label=ds.label,  # type: ignore[arg-type]
                           columns=[gname, "관광지 수", "장애인 화장실 확인", "확인 비율", "장애인 주차 확인", "휠체어 대여 확인",
                                    f"경사 {steep_deg:g}° 초과 기록", "계단·접근불가 있음"],
                           rows=[[r.group, int(r.n), int(r.toilet), float(r.toilet_ratio), int(r.parking), int(r.rental), int(r.steep),
                                  int(r.barrier)] for r in t.itertuples()],
                           note="'확인'은 조사 자료에 '있음'으로 기록된 곳의 수입니다. 정보 없음은 [근거표 S2] 참고. "
                                "현황은 조사 자료 기준이라 비짓제주 태그로 채우지 않습니다(장소 찾기의 숫자와 다를 수 있음).")
        s2_rows = []
        for f in ITEMS:
            col = pd.to_numeric(places[f], errors="coerce")
            unk = int(col.isna().sum())
            s2_rows.append([FAC_KO[f], int((col == 1).sum()), int((col == 0).sum()), unk, round(unk / len(places), 2)])
        s2 = EvidenceTable(id="S2", title="항목별 정보 공백", dataset=ds.source, data_label=ds.label,  # type: ignore[arg-type]
                           columns=["항목", "있음 확인", "없음 확인", "정보 없음", "정보 없음 비율"], rows=s2_rows)
        years = places["data_date"].fillna("").astype(str).str[:4].replace("", "모름")
        if "date_basis" in places:   # 등록일로 채운 곳은 연도 옆에 표시 (조사 연도랑 섞이면 안 됨)
            years = years.where(places["date_basis"].fillna("") != "등록일", years + " 이전(등록일)")
        yc = years.value_counts().sort_index()
        s3 = EvidenceTable(id="S3", title="자료 기준 연도", dataset=ds.source, data_label=ds.label,  # type: ignore[arg-type]
                           columns=["기준 연도", "관광지 수"], rows=[[y, int(n)] for y, n in yc.items()])
        evidence = [s1, s2, s3]
        n = len(places)
        self.step("aggregate", f"관광지 {n}곳을 {gname}별 {len(t)}개 묶음으로 집계")
        tot_t = int((pd.to_numeric(places["acc_toilet"], errors="coerce") == 1).sum())
        findings = [Finding(text=f"{where} 무장애 관광지 {n}곳 중 장애인 화장실이 확인된 곳은 {tot_t}곳입니다.",
                            evidence_ids=["S1"], numbers={"n": n, "toilet": tot_t})]
        # 보완 우선: 3곳 이상 묶음 중 화장실 비율이 전체보다 낮거나 급경사 비율이 높은 곳 (최대 2곳)
        overall_t = round(tot_t / n, 2)
        overall_s = round(float((pd.to_numeric(places["max_slope_deg"], errors="coerce") > steep_deg).sum()) / n, 2)
        big = t[(t["n"] >= MIN_GROUP) & ((t["toilet_ratio"] < overall_t) | (t["steep_ratio"] > overall_s))]
        priority: list[str] = []
        if len(t) > 1 and not big.empty:
            cand = big.sort_values(["toilet_ratio", "steep_ratio", "n"], ascending=[True, False, False]).head(2)
            priority = cand["group"].tolist()
            findings.append(Finding(
                text=f"관광지가 {MIN_GROUP}곳 이상이면서 화장실 확인 비율이 전체({overall_t})보다 낮거나 급경사 비율이 전체({overall_s})보다 높은 곳은 "
                     + ", ".join(f"{r.group}(화장실 확인 비율 {float(r.toilet_ratio)}, 급경사 기록 {int(r.steep)}곳)" for r in cand.itertuples())
                     + "입니다. 보완을 먼저 검토할 곳입니다.", evidence_ids=["S1"]))
        elif len(t) > 1:
            findings.append(Finding(text=f"관광지가 {MIN_GROUP}곳 이상인 {gname} 사이에 화장실 확인 비율이 전체({overall_t})보다 낮은 곳은 없습니다.",
                                    evidence_ids=["S1"]))
        worst = max(s2_rows, key=lambda r: r[3])
        findings.append(Finding(text=f"정보 공백이 가장 큰 항목은 {josa(worst[0], '으로')}, {n}곳 중 {worst[3]}곳이 자료에 언급이 없습니다.",
                                evidence_ids=["S2"], numbers={"unknown": worst[3], "n": n}))
        n_stale = int(sum(stale(d, rules["stale_years"]) for d in places["data_date"]))
        findings.append(Finding(text=f"자료 기준일이 {rules['stale_years']}년 넘게 지났거나 모르는 곳이 {n_stale}곳입니다. 현행화 조사가 필요합니다.",
                                evidence_ids=["S3"], numbers={"stale": n_stale}))
        warnings = []
        if n_stale:
            warnings.append(AgentWarning(code="STALE_DATA", message=f"{n}곳 중 {n_stale}곳의 자료가 {rules['stale_years']}년 넘게 지났거나 기준일을 모릅니다."))
        small = int((t["n"] < MIN_GROUP).sum())
        if small:
            warnings.append(AgentWarning(code="SMALL_GROUPS", severity="info",
                                         message=f"관광지가 {MIN_GROUP}곳 미만인 {gname} {small}곳은 비율이 크게 흔들립니다. 비교에서 조심하세요."))
        if uses(req, "c3_03") and ctx.repo.has("bus_stops"):
            st = nearest_stops(places, ctx.repo.get("bus_stops").df)
            near = rules["bus"]["near_m"]["wheelchair_manual"]
            m = places[["place_id", group_by]].merge(st, on="place_id")
            m[group_by] = m[group_by].fillna("미상")
            g = m.groupby(group_by).agg(n=("place_id", "size"), near=("stop_m", lambda s: int((s <= near).sum()))).reset_index()
            bs = ctx.repo.get("bus_stops")
            evidence.append(EvidenceTable(id="S4", title=f"버스 정류소 {near}m 안 관광지", dataset=bs.source, data_label=bs.label,  # type: ignore[arg-type]
                                          columns=[gname, "관광지 수(좌표 있음)", f"{near}m 안 정류소 있음", "비율"],
                                          rows=[[r[group_by], int(r.n), int(r.near), round(r.near / r.n, 2)] for _, r in g.iterrows()],
                                          note="직선거리 기준. 저상버스 운행 여부는 자료에 없습니다 [미확인]."))
            tn, tnear = int(g["n"].sum()), int(g["near"].sum())
            findings.append(Finding(text=f"좌표가 있는 {tn}곳 중 {near}m 안에 버스 정류소가 있는 곳은 {tnear}곳입니다.",
                                    evidence_ids=["S4"], numbers={"n": tn, "near": tnear}))
        return AgentResult(
            agent=self.name, status="ok", confidence="medium" if warnings else "high",
            summary=f"{where} 무장애 관광지 {n}곳의 시설 확인 현황을 {gname}별로 정리했습니다.",
            findings=findings, evidence=evidence, warnings=warnings,
            limitations=["무장애 조사 자료(2019~2021년 등록)에 언급된 시설만 셉니다. '정보 없음'이 많아 실제보다 낮게 나올 수 있습니다.",
                         "관광지 목록은 데이터허브에 무장애 조사 자료가 있는 곳만입니다(제주 전체 관광지가 아닙니다)."],
            follow_ups=["읍면동별로 다시 봐줘", "화장실 기준으로 다시 봐줘", "보고서로 정리해줘"],
            payload={"group_by": group_by, "focus": focus, "priority_groups": priority,
                     "groups": t.to_dict("records")},
            data_labels=list(dict.fromkeys(e.data_label for e in evidence)))  # type: ignore[list-item]

    def tools(self, req: AnalysisRequest, ctx: AgentContext) -> list[AgentTool]:
        state: dict = {}

        def run(args: dict) -> AgentResult:
            r = self.summarize(req, ctx, group_by=args.get("group_by"), focus=args.get("focus", "all"))
            if not args:
                state["base"] = True
            return r

        def check(args: dict) -> str | None:
            if args and "base" not in state:
                return "조건을 바꾸기 전에 기본 조건(인자 없이)으로 먼저 실행해 결과를 봐야 합니다."
            if args.get("group_by") == "region" and req.region:
                return f"질문이 {req.region}로 정해져 있어 시 단위로 묶으면 1줄뿐입니다."
            return None

        return [AgentTool(
            name="summarize",
            description="지역별 무장애 시설 현황을 집계한다. 인자 없이 부르면 기본(지역 미지정=시별, 시 지정=읍면동별, 읍면동 지정=분류별).",
            params={"group_by": {"type": "string", "enum": list(GROUPS), "description": "묶는 단위"},
                    "focus": {"type": "string", "enum": list(FOCUS), "description": "표를 정렬·강조할 항목"}},
            run=run, check=check, observe=lambda r: {**default_observe(r), "priority_groups": r.payload.get("priority_groups")})]
