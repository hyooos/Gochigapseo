"""분석 결과 → 보고서(Markdown): 문단, 근거표, 한계, 출처 문구
장소·코스면 방문 전 확인 목록, 센터 CSV 썼으면 반출 신청 사유 초안도 붙임
"""
from __future__ import annotations

from datetime import datetime

from ..contracts import Intent, OrchestratorResponse
from ..provenance import SOURCE_NOTICE, footer
from .spec import spec_markdown
from .cards import card_markdown, common_questions

CARDS_IN_REPORT = 8

TITLES = {Intent.ACCESS: "제주 무장애 관광지 추천 결과", Intent.COURSE: "제주 무장애 여행 코스",
          Intent.STATUS: "제주 무장애 관광 인프라 현황", Intent.REPORT: "제주 무장애 여행 분석 보고서"}
AGENT_KO = {"access": "접근성 진단", "course": "여행 코스 설계", "status": "무장애 관광 현황", "explore": "데이터 탐색"}
CONF_KO = {"high": "높음", "medium": "보통", "low": "낮음"}


def _table_md(columns: list[str], rows: list[list]) -> str:
    head = "| " + " | ".join(map(str, columns)) + " |\n|" + "---|" * len(columns) + "\n"
    body = "".join("| " + " | ".join("" if v is None else str(v).replace("|", "/") for v in r) + " |\n" for r in rows)
    return head + body


BODY_ROWS = 10   # 본문 근거표는 앞 10행만, 전체는 끝 '부록'에


def _evidence_md(t, appendix: list) -> list[str]:
    """근거표 하나. 행 많으면 본문엔 앞부분만, 전체는 부록에"""
    head = [f"### [근거표 {t.id}] {t.title} [{t.data_label}]", f"출처: {t.dataset}", ""]
    if len(t.rows) > BODY_ROWS + 2:
        appendix.append(t)
        body = [_table_md(t.columns, t.rows[:BODY_ROWS]),
                f"… 외 {len(t.rows) - BODY_ROWS}행 — 전체 {len(t.rows)}행은 끝의 '부록 — 근거표 전체'에 있습니다.", ""]
    else:
        body = [_table_md(t.columns, t.rows)]
    return head + body + ([f"주: {t.note}\n"] if t.note else [])


def checklist(resp: OrchestratorResponse) -> list[str]:
    """방문 전 확인 목록 (이용자 조건 + '정보 없음'·주의 항목으로 만듦)"""
    p = resp.plan.request.profile
    items = ["운영시간·휴무일·입장 마감 시각 (일부 장소만 조사 당시 기록)"]
    if "acc_toilet" in p.needs or p.mobility.startswith("wheelchair"):
        items.append("장애인 화장실 위치와 지금 이용 가능한지")
    if p.mobility.startswith("wheelchair"):
        items += ["휠체어 대여가 되는지, 미리 예약해야 하는지", "경사·계단 구간을 피하는 다른 길(우회로)이 있는지"]
    if p.mobility == "stroller":
        items.append("유아차 대여·수유실 여부")
    if p.mobility in ("walker", "senior"):
        items.append("중간에 쉴 의자·그늘이 있는지")
    if "acc_parking" in p.needs:
        items.append("장애인 전용 주차구역 위치")
    if p.vision != "none":
        items.append("점자·음성 안내가 지금도 운영되는지, 활동지원 동행이 가능한지")
    if p.hearing != "none":
        items.append("문자·영상 안내(자막) 제공 여부")
    if p.cognition != "none":
        items.append("붐비지 않는 시간대, 조용히 쉴 수 있는 공간이 있는지")
    if p.transport == "bus":
        items.append("저상버스 운행 시간표 (자료에 없음)")
    if p.transport == "taxi":
        items.append("교통약자 이동지원(콜택시) 예약 방법")
    return items


def report_cards(resp: OrchestratorResponse) -> list[dict]:
    """보고서 카드: 코스면 코스 장소, 장소 찾기면 화면에 보인 장소 앞 8곳"""
    if not resp.results:
        return []
    r = resp.results[-1]
    if r.agent == "course":
        return r.payload.get("cards", [])
    if r.agent == "access":
        by_id = {c["place_id"]: c for c in r.payload.get("cards", [])}
        return [by_id[p] for p in r.payload.get("shown_ids", []) if p in by_id][:CARDS_IN_REPORT]
    return []


def export_request_draft(resp: OrchestratorResponse) -> str | None:
    """센터 CSV 썼을 때 반출 신청 사유 초안. 안 썼으면 None"""
    used = sorted({e.dataset for r in resp.results for e in r.evidence if "제주빅데이터센터" in e.dataset})
    if not used:
        return None
    return (f"본 결과물은 {', '.join(used)}를 활용해 관광지별 날씨 위험 비율(비 온 날·강풍일·폭염일 비율)과 "
            "관광지에서 가장 가까운 버스 정류소까지의 직선거리를 계산한 응용집계입니다. 원본 행이나 개인 단위 값은 포함하지 않으며, "
            "시각화는 PNG로만 제출합니다. 무장애 여행 코스 안내와 기관의 무장애 관광 인프라 현황 보고에 쓰기 위해 반출을 신청합니다.")


def build_markdown(resp: OrchestratorResponse, settings_view: dict | None = None) -> str:
    """settings_view: Settings.public_view() (키 없음). 주면 분석 명세에 데이터 모드·모델 적음"""
    req = resp.plan.request
    labels = {lab for r in resp.results for lab in r.data_labels}
    intent = Intent(resp.results[-1].agent) if resp.results and resp.results[-1].agent in ("access", "course", "status") else req.intent
    out = [
        f"# {TITLES.get(intent, TITLES[Intent.REPORT])}",
        f"- 작성 시각: {datetime.now():%Y-%m-%d %H:%M}",
        f"- 질문: {req.question}",
        f"- 이해한 조건: {resp.plan.understood.replace(' — 이렇게 이해했어요. 맞나요?', '')}",
        "",
        "## 결론",
        resp.answer,
        "",
    ]
    appendix: list = []
    for r in resp.results:
        if r.agent == "explore":   # 자료 고르기·점검은 아래 '자료 탐색' 절에 따로
            continue
        out += [f"## {AGENT_KO.get(r.agent, r.agent)} — 신뢰도 {CONF_KO[r.confidence]}", r.summary, ""]
        for f in r.findings:
            refs = " ".join(f"[근거표 {e}]" for e in f.evidence_ids)
            out.append(f"- {f.text} {refs}".rstrip())
        out.append("")
        for t in r.evidence:
            out += _evidence_md(t, appendix)
        if r.warnings:
            out.append("**주의**")
            out += [f"- {w.message}" for w in r.warnings]
            out.append("")
        if r.limitations:
            out.append("**한계**")
            out += [f"- {x}" for x in r.limitations]
            out.append("")
    cards = report_cards(resp)
    if cards:
        out += ["## 장소 카드", "✓ 있음 · ✗ 없음 · ? 정보 없음(자료에 언급 없음 — 없다는 뜻이 아님). 원문은 무장애 조사 자료의 해당 문장입니다.", ""]
        common = common_questions(cards) if len(cards) > 1 else None
        if common:
            out += ["**모든 장소에 전화로 물어볼 말** (장소별로 더 물어볼 것은 각 카드 아래에)",
                    *[f"{i}. {q}" for i, q in enumerate(common, 1)], ""]
        out += [card_markdown(c, common=common, short=True) for c in cards]
    if intent in (Intent.ACCESS, Intent.COURSE):
        out += ["## 방문 전 확인 목록", *[f"- [ ] {x}" for x in checklist(resp)], ""]
    ex = next((r for r in resp.results if r.agent == "explore"), None)
    if ex is not None:
        out += ["## 자료 탐색 — 이번 분석에 쓴 자료와 쓰지 않은 자료", ex.summary, ""]
        out += [f"- {f.text}" for f in ex.findings] + [""]
        for t in ex.evidence:
            out += _evidence_md(t, appendix)
        for g in ex.payload.get("center_guide", []):
            out.append(f"- 센터 자료 {g['no']} {g['name']} 받는 법: " + " → ".join(g["steps"]))
        out.append("")
    v = resp.verification
    if v is not None:
        out += [f"## 검증 결과 — {sum(c.ok for c in v.checks)}/{len(v.checks)} 통과",
                "분석을 만든 에이전트와 따로, 검증 에이전트가 답과 결과를 규칙으로 다시 확인했습니다.", "",
                _table_md(["검사", "결과", "내용"], [[c.name, "통과" if c.ok else "확인 필요", c.detail] for c in v.checks]), ""]
    if resp.results:
        out += ["## 분석 명세 — 어떤 자료·기준·계산으로 나왔나",
                "논문·사업계획서의 '자료와 방법' 절에 그대로 옮겨 쓸 수 있습니다. 재현 정보의 지문이 같으면 같은 설정·자료입니다.", "",
                spec_markdown(resp, settings_view), ""]
    draft = export_request_draft(resp)
    if draft:
        out += ["## 센터 자료 반출 신청 사유 (초안)", draft, ""]
    datasets = sorted({e.dataset for r in resp.results for e in r.evidence})
    if datasets:
        out += ["## 사용한 자료", *[f"- {d}" for d in datasets], ""]
    if appendix:
        out += ["## 부록 — 근거표 전체", "본문에서 앞부분만 보인 근거표의 전체 행입니다.", ""]
        for t in appendix:
            out += [f"### [근거표 {t.id}] {t.title} [{t.data_label}] — 전체 {len(t.rows)}행", f"출처: {t.dataset}", "",
                    _table_md(t.columns, t.rows), ""]
    out += ["---", footer(labels)]
    assert SOURCE_NOTICE in out[-1]
    return "\n".join(out)
