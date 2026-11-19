"""검증 에이전트: 분석한 에이전트랑 따로, 답 나가기 전에 규칙으로 다시 확인
LLM 설명이 걸리면 문장틀로 되돌리고, 결과가 걸리면 화면·보고서에 알림

검사: 숫자 대조, 근거표 번호, 출처·[가상] 표시, 단정 표현, 정보 없음 ≠ 없음, 판정 일관성,
코스 조건, 개인 조건 기록 안 함, 반출 기준(10 미만 셀), 자료 선택
"""
from __future__ import annotations

import json
import re

from .config import access_rules, catalog
from .contracts import AgentResult, Plan, TraceStep, Verification, VerifyCheck
from .privacy import TERMS, profile_words

# ── 숫자·근거표 대조 (LLM 설명 채택에도 씀) ──
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _norm_num(n: str) -> str:
    """'04'랑 '4', '0.50'이랑 '0.5'는 같게 봄. 소수(1.0)랑 정수(1)는 구분"""
    if "." not in n:
        return str(int(n))
    v = f"{float(n):g}"
    return v if "." in v else v + ".0"


def _strip_thousands(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)


def unknown_numbers(candidate: str, reference: str) -> list[str]:
    """LLM 설명 숫자 중 코드 계산에 없는 것
    0~1 비율은 백분율도 같은 숫자로 보고(0.97 → 97%), 소수랑 정수는 구분(1.0 ≠ 1곳)
    """
    found = _NUM.findall(_strip_thousands(reference))
    ref = {_norm_num(n) for n in found}
    pct = {f"{round(float(n) * 100, 6):g}" for n in found if "." in n and 0 < float(n) <= 1}

    def known(n: str) -> bool:
        if _norm_num(n) in ref or n in pct:
            return True
        return "." in n and float(n).is_integer() and str(int(float(n))) in pct

    return list(dict.fromkeys(n for n in _NUM.findall(_strip_thousands(candidate)) if not known(n)))


def numbers_consistent(candidate: str, reference: str) -> bool:
    """LLM 설명 숫자가 전부 초안에 있는지. 하나라도 새 숫자면 거부"""
    return not unknown_numbers(candidate, reference)


_ID = r"[A-Za-z]\d{1,2}"
_REF = re.compile(rf"(?:근거표\s*[:：]?\s*|\[)({_ID}(?:\s*[,·/~]\s*{_ID})*)")


def cited_evidence(text: str) -> list[str]:
    """'[근거표 A1]', '근거표: A1, C2', '[A1]' 등에서 번호 뽑음 (대문자로)"""
    return [i.upper() for m in _REF.findall(text) for i in re.findall(_ID, m)]


def evidence_ids(results: list[AgentResult]) -> set[str]:
    return {e.id for r in results for e in r.evidence}


def answer_problem(candidate: str | None, reference: str, ids: set[str]) -> str | None:
    """LLM 설명 못 쓰는 이유. 쓸 수 있으면 None (기록용, 최대 5개)
    1) 기준에 없는 숫자 X  2) 근거표 있는 분석이면 표시 1개 이상  3) 없는 번호 X  4) 단정 표현·'정보 없음'→'없음' X
    """
    if not candidate or not candidate.strip():
        return "LLM 응답 없음"
    bad = unknown_numbers(candidate, reference)
    if bad:
        return f"초안에 없는 숫자: {', '.join(bad[:5])}"
    cited = cited_evidence(candidate)
    if ids and not cited:
        return "근거표 표시 없음"
    wrong = [i for i in dict.fromkeys(cited) if i not in ids]
    if wrong:
        return f"없는 근거표: {', '.join(wrong[:5])}"
    hits = forbidden_hits(candidate)
    if hits:
        return f"단정 표현: {', '.join(hits[:3])}"
    return None


# ── 문장 검사 ──
def forbidden_hits(text: str) -> list[str]:
    """자료가 보장 못하는 단정 표현"""
    pats = access_rules()["verify"]["forbidden"]
    return [m.group(0) for p in pats for m in re.finditer(p, text)]


def _fac_labels() -> dict[str, str]:
    from .agents.access import FAC_KO  # 순환 import 방지
    return FAC_KO


def absent_claims(text: str, confirmed_none: set[str]) -> list[str]:
    """'화장실 없음'이라 했는데 자료에 '없음 확인'인 곳이 하나도 없는 시설"""
    out = []
    for key, label in _fac_labels().items():
        pat = re.escape(label).replace(r"\ ", r"\s*") + r"\s*(?:이|가|은|는|도)?\s*없(?:음|습니다|어요|다|는)"
        if re.search(pat, text) and key not in confirmed_none:
            out.append(label)
    return out


# ── 결과 검사 ──
def _confirmed_none(results: list[AgentResult]) -> set[str]:
    out = set()
    for r in results:
        for c in r.payload.get("cards", []) + r.payload.get("items", []):   # items: 근처 가게의 '없음 확인'
            out |= {f["key"] for f in c["facilities"] if f["state"] == "없음"}
        for v in r.payload.get("verdicts", []):
            if v.get("acc_toilet") == 0:
                out.add("acc_toilet")
    return out


def _access_consistency(r: AgentResult) -> list[str]:
    if r.payload.get("place_q"):   # 장소 하나 물어보기는 이용자가 고른 곳이라 판정 상관없이 보여 줌
        return []
    bad = []
    by_id = {v["place_id"]: v for v in r.payload.get("verdicts", [])}
    cards = {c["place_id"]: c for c in r.payload.get("cards", [])}
    for pid in r.payload.get("shown_ids", []):
        v = by_id.get(pid)
        if v and v["verdict"] == "부적합":
            bad.append(f"{v['name']} 부적합인데 보여 줌")
        c = cards.get(pid)
        if c and any(f["needed"] and f["state"] == "없음" for f in c["facilities"]):
            bad.append(f"{c['name']} 필요 시설 없음 확인인데 보여 줌")
    return bad


def _course_violations(r: AgentResult, plan: Plan) -> list[str]:
    p = r.payload
    it = p.get("itinerary", [])
    bad = []
    if any(x.get("verdict") == "부적합" for x in it):
        bad.append("부적합 장소 포함")
    ids = [x["place_id"] for x in it]
    if len(ids) != len(set(ids)):
        bad.append("같은 장소 반복")
    days = plan.request.days or 1
    if any(x["day"] > days for x in it):
        bad.append(f"{days}일을 넘는 일차")
    per_day = access_rules()["course"]["pace"].get(p.get("pace"), {}).get("per_day")
    caps = {int(d): int(n) for d, n in (p.get("day_caps") or {}).items()}   # 이용자가 정한 그날 장소 수가 우선
    if per_day:
        for d in {x["day"] for x in it}:
            if sum(1 for x in it if x["day"] == d) > caps.get(d, per_day):
                bad.append(f"{d}일차 장소 수 초과")
    gone = set(p.get("excluded_ids") or [])   # 수정 대화에서 뺀 장소
    back = [x["name"] for x in it if x["place_id"] in gone]
    if back:
        bad.append(f"뺀 장소가 다시 들어감: {', '.join(back)}")
    for d, n in (p.get("day_caps") or {}).items():
        if sum(1 for x in it if x["day"] == int(d)) > int(n):
            bad.append(f"{d}일차를 {n}곳 이하로 맞추지 않음")
    cap = p.get("ride_cap")
    if cap is not None:
        over = [x["name"] for x in it if x.get("leg_min", 0) > cap]
        if over:
            bad.append(f"이동 {cap}분 초과: {', '.join(over[:3])}")
    return bad


def _sensitive_words(plan: Plan) -> list[str]:
    """이 이용자 조건 낱말 + 질문 해석 조건 낱말 전체 (LLM이 줄여 써도 잡게)"""
    return list(dict.fromkeys(profile_words(plan.request.profile) + list(TERMS)))


def _center_cells(results: list[AgentResult]) -> tuple[int, list[str]]:
    """센터 자료 근거표 수, 10 미만 셀 있는 표"""
    n, bad = 0, []
    for r in results:
        for t in r.evidence:
            if "센터" not in t.dataset:   # 실데이터: "제주빅데이터센터, …" · 가상: "[가상] 센터 …"
                continue
            n += 1
            cols = [i for i, c in enumerate(t.columns) if c.endswith(("수", "명", "건"))]
            if any(isinstance(row[i], (int, float)) and 0 < row[i] < 10 for row in t.rows for i in cols):
                bad.append(t.id)
    return n, bad


def verify(plan: Plan, results: list[AgentResult], answer: str, draft: str,
           trace: list[TraceStep] | None = None) -> Verification:
    checks: list[VerifyCheck] = []
    ok_results = [r for r in results if r.status in ("ok", "partial")]
    ids = evidence_ids(results)
    reference = draft + "\n" + json.dumps([[t.title, t.columns, t.rows] for r in results for t in r.evidence],
                                          ensure_ascii=False, default=str)

    bad = unknown_numbers(answer, reference)
    checks.append(VerifyCheck(name="숫자 대조", ok=not bad,
                              detail=f"초안에 없는 숫자: {', '.join(bad[:5])}" if bad else "답의 숫자가 모두 코드 계산 결과에 있음"))
    if ids:
        cited = cited_evidence(answer)
        wrong = [i for i in dict.fromkeys(cited) if i not in ids]
        ok = bool(cited) and not wrong
        checks.append(VerifyCheck(name="근거표 번호", ok=ok,
                                  detail=(f"없는 근거표: {', '.join(wrong)}" if wrong else "근거표 표시 없음" if not cited
                                          else f"근거표 {len(set(cited))}개를 댐")))
    if ok_results:
        missing = [t.id for r in ok_results for t in r.evidence if not t.dataset or t.data_label not in ("가상", "실데이터")]
        checks.append(VerifyCheck(name="출처·자료 구분", ok=not missing,
                                  detail=f"출처·표시 없는 표: {', '.join(missing)}" if missing else "모든 근거표에 출처와 [가상]/[실데이터] 표시"))
    hits = forbidden_hits(answer)
    checks.append(VerifyCheck(name="단정 표현 없음", ok=not hits, detail=", ".join(hits[:3]) or "없음"))
    absent = absent_claims(answer, _confirmed_none(results))
    checks.append(VerifyCheck(name="정보 없음 ≠ 없음", ok=not absent,
                              detail=f"자료에 '없음 확인'이 없는데 없다고 말함: {', '.join(absent)}" if absent
                              else "'정보 없음'을 '없음'으로 말하지 않음"))
    for r in ok_results:
        if r.agent == "access" and r.payload.get("shown_ids") is not None:
            b = _access_consistency(r)
            checks.append(VerifyCheck(name="판정 일관성", ok=not b, detail="; ".join(b[:3]) or "보여 준 장소는 모두 적합·주의"))
        if r.agent == "course" and r.payload.get("itinerary") is not None:
            b = _course_violations(r, plan)
            cap = r.payload.get("ride_cap")
            checks.append(VerifyCheck(name="코스 조건", ok=not b, detail="; ".join(b[:3]) or (
                "부적합·반복 없음, 하루 장소 수·일수 지킴" + (f", 모든 이동 {cap}분 이내" if cap else ""))))
    if trace is not None:
        steps = list(trace) + [t for r in results for t in r.trace]
        text = json.dumps([s.model_dump() for s in steps], ensure_ascii=False)
        leaked = [w for w in _sensitive_words(plan) if w and w in text]
        checks.append(VerifyCheck(name="개인 조건 기록 안 함", ok=not leaked,
                                  detail="분석 기록에 조건 낱말이 남음" if leaked else "분석 기록에 이동 방법·필요 시설이 없음"))
    choice = _data_choice(results)
    if choice is not None:
        ex = next(r for r in results if r.agent == "explore")
        checks.append(VerifyCheck(name="자료 선택", ok=not choice, detail="; ".join(choice[:3]) or
                                  f"고른 자료 {len(ex.payload['selected'])}개만 사용, 반출 불가 표 없음"))
    n_center, cells = _center_cells(results)
    if n_center:
        checks.append(VerifyCheck(name="센터 자료 반출 기준", ok=not cells,
                                  detail=f"10 미만 셀이 있는 표: {', '.join(cells)}" if cells
                                  else f"센터 자료 표 {n_center}개 — 비율·거리 응용집계, 10 미만 셀 없음"))
    return Verification(passed=all(c.ok for c in checks), checks=checks)


EVIDENCE_DATA = {"C3": "c5_02", "C4": "c3_03", "S4": "c3_03", "C5": "bf_nearby"}   # 근거표 → 그 표를 만든 자료(카탈로그 ID)


def _data_choice(results: list[AgentResult]) -> list[str] | None:
    """탐색 결과 있으면 문제 목록(빈 목록 = 통과), 없으면 None (검사 안 함)"""
    ex = next((r for r in results if r.agent == "explore"), None)
    if ex is None:
        return None
    cat = catalog()["datasets"]
    chosen = ex.payload.get("selected", [])
    bad = [f"카탈로그에 없는 자료 {k}" for k in chosen if k not in cat]
    bad += [f"반출 불가 표 {cat[k]['code']}" for k in chosen if k in cat and cat[k].get("export") == "불가"]
    used = {EVIDENCE_DATA[t.id] for r in results if r.agent != "explore" for t in r.evidence if t.id in EVIDENCE_DATA}
    bad += [f"고르지 않은 자료를 씀({cat[k]['name']})" for k in sorted(used - set(chosen))]
    return bad


ANSWER_CHECKS = {"숫자 대조", "근거표 번호", "단정 표현 없음", "정보 없음 ≠ 없음"}


def answer_failed(v: Verification) -> bool:
    """답 문장 때문에 걸렸는지 (→ 문장틀로 되돌리면 됨)"""
    return any(not c.ok and c.name in ANSWER_CHECKS for c in v.checks)
