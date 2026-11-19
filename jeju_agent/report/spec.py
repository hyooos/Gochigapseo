"""분석 명세: 어떤 자료·기준·계산으로 나온 결과인지 표 하나로 (화면·보고서 공용)
설정·데이터 파일 지문(SHA-256 앞 8자리)으로 다시 돌렸을 때 같은 조건인지 확인 가능
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from ..agents.status import MIN_GROUP
from ..config import CONFIG_DIR, ROOT, access_rules
from ..contracts import MOBILITY_KO, OrchestratorResponse

FINGERPRINT_FILES = ("config/access_rules.yaml", "config/keywords.yaml", "config/catalog.yaml", "data/public/places.csv",
                     "data/public/place_points.csv")


@lru_cache(maxsize=32)
def _sha8(path: str, mtime: float) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:8]


def fingerprints() -> list[tuple[str, str]]:
    out = []
    for rel in FINGERPRINT_FILES:
        p = ROOT / rel
        if p.exists():
            out.append((rel, _sha8(str(p), p.stat().st_mtime)))
    return out


def _datasets(resp: OrchestratorResponse) -> str:
    ex = next((r for r in resp.results if r.agent == "explore"), None)
    if ex is not None:
        d1 = next((t for t in ex.evidence if t.id == "D1"), None)
        if d1 is not None:
            i_name, i_lab = d1.columns.index("자료"), d1.columns.index("구분")
            i_n = d1.columns.index("행 수") if "행 수" in d1.columns else None
            i_d = d1.columns.index("기준일") if "기준일" in d1.columns else None
            parts = []
            for row in d1.rows:
                extra = ", ".join(x for x in ((f"{row[i_n]}행" if i_n is not None and row[i_n] not in ("-", None) else ""),
                                              (f"기준일 {row[i_d]}" if i_d is not None and row[i_d] not in ("-", None) else "")) if x)
                parts.append(f"{row[i_name]} {row[i_lab]}" + (f"({extra})" if extra else ""))
            return " · ".join(parts) + " — 고른 이유·뺀 자료는 근거표 D1·D2"
    names = sorted({e.dataset for r in resp.results for e in r.evidence})
    return " · ".join(names) or "-"


def _criteria(resp: OrchestratorResponse) -> list[tuple[str, str]]:
    rules = access_rules()
    prof = resp.plan.request.profile
    agents = {r.agent for r in resp.results}
    out = []
    if agents & {"access", "course"}:
        s = rules["slope"].get(prof.mobility, rules["slope"]["none"])
        sb = rules["slope_block"]
        slope = ("경사 기준 없음(이동 조건 없음)" if prof.mobility == "none" else
                 f"{MOBILITY_KO[prof.mobility]}: 경사 {s['ok']:g}° 이하 적합, {s['caution']:g}° 넘는 기록이 {sb['count']}곳 이상이거나 "
                 f"최대 {s['caution'] + sb['margin']:g}° 이상이면 부적합, 그 사이는 주의")
        sc = rules["score"]
        share = rules["barriers"].get(prof.mobility, rules["barriers"]["none"])["block_share"]
        stairs = f"계단·접근불가 지점이 조사 지점의 {share:.0%} 이상, " if share <= 1 else ""
        out += [("판정 기준", f"{slope}. 그 밖의 부적합: {stairs}꼭 필요한 시설이 '없음'으로 확인, 조사자 비추천. "
                              "'정보 없음'은 판정에 쓰지 않고 따로 셈 (config/access_rules.yaml)"),
                ("점수", f"기본 {sc['base']}점 − 주의 이유 1개당 {sc['per_caution_reason']} − 장애물 지점당 {sc['per_barrier_point']}"
                         f"(최대 {sc['barrier_cap']}) + 도움 되는 시설 1개당 {sc['facility_bonus']} + 관심사 일치 {sc['interest_bonus']}. "
                         "점수는 순서를 정할 때만 쓰고 적합·주의·부적합은 위 기준으로 정함")]
    if "course" in agents:
        cr = rules["course"]
        c = next(r for r in resp.results if r.agent == "course")
        p = c.payload
        pace = cr["pace"].get(p.get("pace", "보통"), {})
        ko = {"car": "자가용", "taxi": "택시", "bus": "버스"}
        speed = " · ".join(f"{ko.get(k, k)} {v}km/h" for k, v in cr["speed_kmh"].items())
        out.append(("계산 방법", f"이동 시간 = 직선거리 × {cr['road_factor']}(도로 보정, 가정) ÷ 속도({speed}), 버스는 기다림 "
                                f"{cr['bus_wait_min']}분 더함. 속도 '{p.get('pace', '보통')}': 하루 최대 {pace.get('per_day', '-')}곳, "
                                f"한 번 이동 상한 {p.get('max_leg_min') or cr['default_max_leg_min']}분"
                                f"{'(이용자가 정함, 넘지 않음)' if p.get('ride_cap') else ''}. 다음 장소는 "
                                f"점수 − 이동 분 × {cr['leg_penalty_per_min']}가 가장 큰 곳"))
        if p.get("rain_ratio") is not None:
            w = rules["weather"]
            out.append(("날씨", f"여행 달의 과거 일일 날씨에서 비 온 날(강수 {w['rain_mm']:g}mm 이상) 비율 {p['rain_ratio']:.0%}. "
                               f"{w['rain_ratio_high']:.0%} 이상이면 하루 1곳은 실내 장소"))
        if p.get("excluded_ids") or p.get("day_caps"):
            caps = ", ".join(f"{d}일차 {n}곳" for d, n in sorted((p.get("day_caps") or {}).items()))
            out.append(("이용자 수정", f"뺀 장소 {len(p.get('excluded_ids') or [])}곳" + (f", 줄인 날: {caps}" if caps else "")
                                    + " (근거표 C6)"))
    if "status" in agents:
        out.append(("계산 방법", f"장소 수를 세어 비율로 나눔(장소 1곳 = 1). 관광지가 {MIN_GROUP}곳 미만인 묶음은 비율이 "
                                "크게 흔들린다고 경고"))
    return out


def analysis_spec(resp: OrchestratorResponse, settings_view: dict | None = None) -> list[tuple[str, str]]:
    """[(항목, 내용)]. settings_view는 Settings.public_view() (키 없음)"""
    sv = settings_view or {}
    v = resp.verification
    u = resp.llm_usage
    parser = {"rule": "규칙", "llm": "LLM(규칙으로 다시 검사)"}.get(resp.plan.parser, resp.plan.parser)
    rows = [("질문 이해", f"{parser} — {resp.plan.understood.replace(' — 이렇게 이해했어요. 맞나요?', '')}"),
            ("쓴 자료", _datasets(resp))]
    rows += _criteria(resp)
    rows.append(("검증", f"{sum(c.ok for c in v.checks)}/{len(v.checks)} 통과 — 숫자 대조·근거표·출처·단정 표현·정보 없음·판정·코스 조건 등"
                 if v else "-"))
    llm = (f"설명 문장 {'LLM' if resp.answer_source == 'llm' else '문장틀(LLM 없음)'} · 호출 {u.calls}회(캐시 {u.cached}) · "
           f"예상 비용 ${u.est_cost_usd}")
    if sv.get("llm_enabled"):
        llm += f" · 모델 {sv.get('llm_model', '-')}"
    rows.append(("LLM 사용", llm + ". 숫자·판정은 모두 코드가 계산(LLM은 계산하지 않음)"))
    mode = {"sample": "sample(센터 표는 [가상])", "real": "real(센터에서 받은 CSV)"}.get(sv.get("data_mode"), sv.get("data_mode"))
    rows.append(("재현 정보", (f"데이터 모드 {mode} · " if mode else "") +
                 " · ".join(f"{Path(f).name} {h}" for f, h in fingerprints()) +
                 " (SHA-256 앞 8자리: 같은 값이면 같은 설정·자료)"))
    return rows


def spec_markdown(resp: OrchestratorResponse, settings_view: dict | None = None) -> str:
    rows = analysis_spec(resp, settings_view)
    body = "\n".join(f"| {k} | {v.replace('|', '/')} |" for k, v in rows)
    return "| 항목 | 내용 |\n|---|---|\n" + body


__all__ = ["analysis_spec", "spec_markdown", "fingerprints", "CONFIG_DIR"]
