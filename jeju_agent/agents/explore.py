"""데이터 탐색 에이전트: 분석 전에 쓸 자료 고르고 점검
search_catalog → select(이유 남김) → inspect(행 수·빈 값·기준일). 센터 표 없으면 조회 사이트 안내

- LLM은 날씨·버스·주변정보·사진만 켜고 끔. 반출 불가 표는 못 고름
- 분석 에이전트는 고른 자료만 씀 (base.uses). 점검 결과는 숫자만
"""
from __future__ import annotations

import pandas as pd

from ..config import catalog
from ..contracts import AgentResult, AgentWarning, AnalysisRequest, EvidenceTable, Finding, Intent
from .base import AgentContext, AgentTool, BaseAgent, default_observe

OPTIONAL = {"weather": ("c5_01", "c5_02"), "bus": ("c3_03",), "nearby": ("bf_nearby",), "photos": ("bf_photos",)}
INTENT_TOPICS = {Intent.ACCESS: {"접근성", "관광"}, Intent.COURSE: {"접근성", "코스", "관광", "날씨", "대중교통"},
                 Intent.STATUS: {"접근성", "관광", "대중교통"},
                 Intent.NEARBY: {"접근성", "관광", "소비"}}
WORD_TOPICS = {"대중교통": ("버스", "정류소", "정류장", "대중교통"), "혼잡": ("붐비", "붐벼", "혼잡", "북적", "사람 많"),
               "소비": ("식당", "맛집", "카페", "밥 먹"), "날씨": ("날씨", "비 오", "바람", "더위", "태풍")}
BUS_WORDS = WORD_TOPICS["대중교통"]
COL_KO = {"acc_toilet": "장애인 화장실", "acc_parking": "장애인 주차", "ramp": "경사로", "lat": "좌표", "phone": "전화",
          "disabled_toilet": "장애인 화장실 표시", "caption": "사진 설명", "latitude": "좌표", "longitude": "경도",
          "daily_rainfall": "일강수", "maximum_wind_speed": "최대풍속"}
# '센터'라는 말 넣으면 안 됨: 반출 기준 검사가 출처에 '센터' 있는 표를 센터 자료로 봄
CATALOG_SOURCE = "데이터 카탈로그(config/catalog.yaml: 제주데이터허브 무장애 자료·비짓제주 관광정보·데이터명세서 v13) · 점검은 이 PC 자료로 계산"


def has_bus_words(question: str) -> bool:
    return any(w in question for w in BUS_WORDS)


def search_catalog(req: AnalysisRequest) -> list[str]:
    """질문 주제에 맞는 후보 ID (카탈로그 순서). 반출 불가 표도 후보로는 보임 (고르진 않음)"""
    want = set(INTENT_TOPICS.get(req.intent, {"접근성"}))
    want |= {t for t, words in WORD_TOPICS.items() if any(w in req.question for w in words)}
    if req.month is not None:      # 여행 달 말했으면 날씨도 후보로 (쓸지는 select가 정함)
        want.add("날씨")
    return [k for k, d in catalog()["datasets"].items() if want & set(d.get("topics", []))]


def rule_options(req: AnalysisRequest) -> dict[str, bool]:
    """규칙 판단: 선택 자료 켤지"""
    travel = req.intent in (Intent.ACCESS, Intent.COURSE, Intent.NEARBY)
    return {"weather": req.intent == Intent.COURSE and req.month is not None,
            "bus": (req.intent == Intent.COURSE and req.profile.transport == "bus")
            or (req.intent == Intent.STATUS and has_bus_words(req.question)),
            "nearby": travel, "photos": travel}


def option_problem(req: AnalysisRequest, opts: dict[str, bool]) -> str | None:
    """선택 울타리. 막을 이유 or None"""
    if opts.get("weather") and req.month is None:
        return "여행 달이 없어 날씨 비율을 계산할 수 없습니다."
    if opts.get("weather") and req.intent != Intent.COURSE:
        return "날씨는 코스를 짤 때만 씁니다(실내 장소 배치)."
    if req.intent == Intent.STATUS and (opts.get("nearby") or opts.get("photos")):
        return "현황 집계에는 사진·주변정보를 쓰지 않습니다."
    if req.intent in (Intent.ACCESS, Intent.COURSE) and opts.get("photos") is False:
        return "장소 카드·날짜별 동선·일정표는 조사 사진을 보여 주므로 사진 자료를 빼지 않습니다."
    if req.intent == Intent.NEARBY and opts.get("nearby") is False:
        return "근처 식당·카페·숙소 질문은 주변정보 자료 없이 답할 수 없어 빼지 않습니다."
    if req.intent == Intent.NEARBY and opts.get("bus"):
        return "근처 식당·숙소 찾기에는 정류소 거리를 쓰지 않습니다."
    if req.intent == Intent.ACCESS and opts.get("bus"):
        return "장소 찾기에는 정류소 거리를 쓰지 않습니다(버스 코스를 짤 때 씁니다)."
    return None


def select(req: AnalysisRequest, opts: dict[str, bool]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(고른 자료 → 이유, 뺀 자료 → 이유, 후보 목록)"""
    cat = catalog()["datasets"]
    cands = search_catalog(req)
    chosen = {"bf_places": "관광지·코스의 시설·장애물을 판정하는 바탕 자료(필수 데이터)",
              "bf_points": "경사·계단·접근불가가 기록된 지점 — 판정과 장소 카드의 근거"}
    if opts.get("photos"):
        chosen["bf_photos"] = "장소 카드·날짜별 동선에 조사 때 찍은 사진을 보이기 위해"
    if req.intent in (Intent.ACCESS, Intent.COURSE):   # 장소 판정 있는 질문만. 현황은 조사 자료로만 셈
        chosen["vj_tags"] = "조사 자료에 빈칸인 시설을 비짓제주 태그로 보완하고(있음만), 조사 사진이 없는 곳에 대표 사진을 보이기 위해"
    if opts.get("nearby"):
        chosen["bf_nearby"] = "코스 장소 주변 휠체어 진입 가능으로 조사된 식당·카페·숙소 안내"
        if req.intent == Intent.COURSE:   # 근처 질문은 아직 주변정보만 씀
            chosen["kto_bf"] = "코스 점심·저녁 식당 후보를 늘리기 위해(장애인 화장실·출입 문장이 있는 제주 식당)"
    if opts.get("weather"):
        chosen["c5_01"] = "관광지마다 가장 가까운 날씨 관측소를 찾기 위해"
        chosen["c5_02"] = f"{req.month}월의 비 온 날·강풍일·폭염일 비율 → 실내 장소 배치"
    if opts.get("bus"):
        chosen["c3_03"] = "장소에서 가장 가까운 버스 정류소까지 거리"
    skipped = {}
    for k in cands:
        if k in chosen:
            continue
        d = cat[k]
        why = d.get("skip", "이번 질문에는 필요하지 않습니다.")
        if k in ("c5_01", "c5_02") and req.month is not None and req.intent != Intent.COURSE:
            why = "장소 찾기·현황에는 날씨를 쓰지 않습니다(코스를 짤 때 반영)."
        if k == "kto_bf" and req.intent == Intent.NEARBY:
            why = "근처 식당 찾기는 기준 장소 주변 조사 자료(무장애 코스 주변정보)로 답합니다(관광공사 식당은 코스에만 씀)."
        if k == "vj_tags" and req.intent == Intent.NEARBY:
            why = "근처 식당·카페·숙소 찾기에는 관광지 시설 태그를 쓰지 않습니다."
        if k == "c3_03":
            why = {Intent.ACCESS: "장소 찾기에는 정류소 거리를 쓰지 않습니다(버스 코스를 짤 때 반영).",
                   Intent.STATUS: "질문에 대중교통이 없어 정류소 거리를 보지 않습니다."}.get(req.intent, why)
        if d.get("export") == "불가":
            why = why if why.startswith("반출 불가") else f"반출 불가 표 — {why}"
        skipped[k] = why
    return chosen, skipped, cands


def _frame(ctx: AgentContext, table: str) -> tuple[pd.DataFrame | None, str | None]:
    """(표, 표시 라벨). 없으면 (None, None)"""
    repo = ctx.repo
    if table == "nearby":
        df = repo.nearby_df()
        return (df, repo.label) if len(df) else (None, None)
    if table == "photos":
        df = repo.photos_df()
        return (df, repo.label) if len(df) else (None, None)
    if table == "kto":   # 외부 공개 자료 (받아 둔 표). 실데이터
        df = repo.kto_df()
        return (df, "실데이터") if len(df) else (None, None)
    if table == "visitjeju":   # 외부 공개 자료 (받아 둔 표). 실데이터
        df = repo.visitjeju_df()
        return (df, "실데이터") if len(df) else (None, None)
    if not repo.has(table):
        return None, None
    ds = repo.get(table)
    return ds.df, ds.label


def inspect(ctx: AgentContext, key: str) -> dict:
    """자료 하나 점검: 상태, 행 수, 빈 값 비율, 기준일 범위. 값은 안 담음"""
    d = catalog()["datasets"][key]
    df, label = _frame(ctx, d["table"])
    if df is None:
        need = "센터 CSV 필요" if d["source"] == "제주빅데이터센터" else "없음"
        return {"id": key, "status": need, "rows": None, "gaps": {}, "dates": None, "label": None}
    gaps = {}
    for c in d.get("check", []):
        if c in df:
            col = df[c].replace("", pd.NA) if df[c].dtype == object else df[c]
            gaps[c] = round(float(col.isna().mean()), 2) if len(df) else None
    dates = None
    dc = d.get("date_col")
    if dc and dc in df:
        s = df[dc].astype(str).str.replace(r"[^0-9]", "", regex=True).str[:8]
        s = s[s.str.len() == 8]
        if len(s):
            lo, hi = s.min(), s.max()
            dates = f"{lo[:4]}-{lo[4:6]}-{lo[6:]}" + ("" if lo == hi else f" ~ {hi[:4]}-{hi[4:6]}-{hi[6:]}")
    elif d.get("fetched"):
        dates = f"{d['fetched']} 받음"
    extra = {}
    if key == "bf_places" and "date_basis" in df:
        extra["registered"] = int((df["date_basis"].fillna("") == "등록일").sum())
    if key == "bf_points" and "slope_deg" in df:
        extra["slope_records"] = int(pd.to_numeric(df["slope_deg"], errors="coerce").notna().sum())
    return {"id": key, "status": "있음", "rows": int(len(df)), "gaps": gaps, "dates": dates, "label": label, **extra}


def center_guide(req: AnalysisRequest, missing: list[str]) -> list[dict]:
    cat, g = catalog()["datasets"], catalog()["center_guide"]
    out = []
    for k in missing:
        d = cat[k]
        info = g.get(k, {})
        period = info.get("period", "필요한 기간").replace("{month}", str(req.month or "여행"))
        out.append({"id": k, "no": d["code"], "name": d["name"], "period": period, "condition": info.get("condition", "전체"),
                    "steps": [f"센터 조회 사이트에서 표 '{d['code']} {d['name']}' 선택", f"기간: {period}",
                              f"조건: {info.get('condition', '전체')}", "CSV로 내려받기(열 이름은 영어 그대로 둠)",
                              "이 프로그램 왼쪽 메뉴 '센터 자료 넣기'에 넣기"]})
    return out


def _gap_text(r: dict) -> str:
    if not r["gaps"]:
        return "-"
    return ", ".join(f"{COL_KO.get(c, c)} {v:.0%}" for c, v in r["gaps"].items() if v is not None)


class ExploreAgent(BaseAgent):
    name = "explore"
    title = "데이터 탐색"
    description = "카탈로그에서 이번 질문에 쓸 자료를 찾아 고르고(고른 이유·뺀 이유), 행 수·빈 값·기준일을 점검한다."
    required = ("select_datasets",)

    def _run(self, req: AnalysisRequest, ctx: AgentContext) -> AgentResult:
        opts = req.params.get("opts") or {}
        return self.explore(req, ctx, {**rule_options(req), **{k: v for k, v in opts.items() if k in OPTIONAL}})

    def explore(self, req: AnalysisRequest, ctx: AgentContext, opts: dict[str, bool]) -> AgentResult:
        cat = catalog()["datasets"]
        chosen, skipped, cands = select(req, opts)
        self.step("search_catalog", f"후보 {len(cands)}개 (카탈로그 {len(cat)}개 중)")
        self.step("select", f"고름 {len(chosen)}개: {', '.join(chosen)} / 뺌 {len(skipped)}개")
        checks = [inspect(ctx, k) for k in chosen]
        self.step("inspect", ", ".join(f"{c['id']}={c['status']}" for c in checks))
        missing = [c["id"] for c in checks if c["status"] == "센터 CSV 필요"]
        guide = center_guide(req, missing)

        labels = {c["label"] for c in checks if c["label"]}
        label = "가상" if "가상" in labels else "실데이터"
        d1_rows = [[cat[c["id"]]["name"], cat[c["id"]]["source"] + (f" {cat[c['id']]['code']}" if cat[c["id"]].get("code") else ""),
                    f"[{c['label']}]" if c["label"] else "-", chosen[c["id"]], c["status"], c["rows"], c["dates"] or "-", _gap_text(c)]
                   for c in checks]
        d2_rows = [[cat[k]["name"], cat[k]["source"] + (f" {cat[k]['code']}" if cat[k].get("code") else ""), cat[k]["export"], why]
                   for k, why in skipped.items()]
        evidence = [
            EvidenceTable(id="D1", title="이번 분석에 쓴 자료", dataset=CATALOG_SOURCE, data_label=label,  # type: ignore[arg-type]
                          columns=["자료", "출처", "구분", "왜 썼나", "상태", "행 수", "기준일", "빈 값(점검 열)"], rows=d1_rows,
                          note="구분은 자료마다 [실데이터]/[가상]을 따로 적었습니다(표 제목의 표시는 하나라도 가상이면 [가상]). "
                               "빈 값 = 그 열이 비어 있는 행의 비율. 시설 열이 비어 있으면 '정보 없음'으로 다룹니다(없다는 뜻이 아님)."),
            EvidenceTable(id="D2", title="찾았지만 쓰지 않은 자료와 이유", dataset=CATALOG_SOURCE, data_label=label,  # type: ignore[arg-type]
                          columns=["자료", "출처", "반출", "쓰지 않은 이유"], rows=d2_rows,
                          note="반출 불가 표는 원본을 이 프로그램에서 다루지 않습니다(외부 LLM에도 보내지 않음)."),
        ]
        findings = [Finding(text=f"카탈로그에서 후보 {len(cands)}개를 찾아 {len(chosen)}개를 골랐습니다.",
                            evidence_ids=["D1", "D2"], numbers={"cands": len(cands), "chosen": len(chosen)})]
        place = next((c for c in checks if c["id"] == "bf_places" and c["rows"]), None)
        if place and place["gaps"].get("acc_toilet") is not None:
            findings.append(Finding(text=f"관광지 {place['rows']}곳 중 장애인 화장실 열이 빈 곳이 {place['gaps']['acc_toilet']:.0%}라 "
                                         "그곳은 '정보 없음'으로 다룹니다.", evidence_ids=["D1"],
                                    numbers={"rows": place["rows"], "gap": place["gaps"]["acc_toilet"]}))
        if place and place.get("registered"):
            findings.append(Finding(text=f"{place['registered']}곳은 조사 기준일자가 없어 데이터허브 등록일로 기준일을 대신합니다.",
                                    evidence_ids=["D1"], numbers={"registered": place["registered"]}))
        no_xy = place["gaps"].get("lat") if place else None
        if no_xy:
            findings.append(Finding(text=f"좌표가 없는 곳({no_xy:.0%})은 지도·코스에서 빠집니다.", evidence_ids=["D1"],
                                    numbers={"no_xy": no_xy}))
        warnings = []
        for g in guide:
            warnings.append(AgentWarning(code="CENTER_NEEDED", severity="info",
                                         message=f"센터 표 {g['no']} {g['name']}가 필요하지만 아직 넣지 않았습니다. "
                                                 f"센터 조회 사이트 → 기간 {g['period']} → 왼쪽 메뉴 '센터 자료 넣기'."))
        n_block = sum(1 for k in skipped if cat[k].get("export") == "불가")
        return AgentResult(
            agent=self.name, status="ok", confidence="high",
            summary=f"자료 {len(chosen)}개를 골라 점검했습니다(후보 {len(cands)}개, 반출 불가 {n_block}개 제외).",
            findings=findings, evidence=evidence, warnings=warnings,
            limitations=["데이터허브는 데이터셋 검색 API가 없어, 데이터허브·센터 명세서로 만든 카탈로그 안에서 찾습니다."],
            data_labels=[label],  # type: ignore[list-item]
            payload={"selected": list(chosen), "skipped": list(skipped), "candidates": len(cands), "catalog": len(cat),
                     "reasons": chosen, "skip_reasons": skipped, "inspect": checks, "center_guide": guide,
                     "options": {k: bool(v) for k, v in opts.items()}})

    def tools(self, req: AnalysisRequest, ctx: AgentContext) -> list[AgentTool]:
        def run(args: dict) -> AgentResult:
            return self.explore(req, ctx, {**rule_options(req), **args})

        def check(args: dict) -> str | None:
            return option_problem(req, {**rule_options(req), **args})

        return [AgentTool(
            name="select_datasets",
            description="이번 질문에 쓸 선택 자료를 켜고 끈다. 관광지·조사 지점은 항상 쓴다. 인자를 비우면 규칙 기본값."
                        " weather=센터 5-01·5-02(코스·여행 달 있을 때), bus=센터 3-03, nearby=코스 주변 식당·숙소, photos=조사 사진.",
            params={"weather": {"type": "boolean"}, "bus": {"type": "boolean"}, "nearby": {"type": "boolean"},
                    "photos": {"type": "boolean"}},
            run=run, check=check,
            observe=lambda r: {**default_observe(r), "selected": r.payload["selected"], "skipped": r.payload["skipped"],
                               "missing_center": [g["no"] for g in r.payload["center_guide"]]})]
