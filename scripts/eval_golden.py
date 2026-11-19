"""골든 질문셋 정답률 → outputs/golden_report.md

    python scripts/eval_golden.py          # 오프라인
    python scripts/eval_golden.py --api    # LLM 사용 (비용 나감)
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import llm_record  # noqa: E402  (scripts/llm_record.py)
import yaml  # noqa: E402

from jeju_agent.config import OUTPUT_DIR, get_settings  # noqa: E402
from jeju_agent.evaluation import check  # noqa: E402
from jeju_agent.llm import LLMClient  # noqa: E402
from jeju_agent.orchestrator import Orchestrator  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", action="store_true", help="LLM API 모드로 실행")
    ap.add_argument("--record", action="store_true", help="(--api와 함께) 실제 모델 답을 outputs/llm_record_golden.jsonl에 남김(캐시 끔)")
    ap.add_argument("--replay", help="남겨 둔 llm_record 파일로 키 없이 같은 흐름 재생(도구 선택은 규칙)")
    a = ap.parse_args(argv)
    s = get_settings()
    if a.replay:   # 키 없이 남겨 둔 모델 답으로 (실제 호출 없음)
        s = dataclasses.replace(s, llm_mode="api", llm_api_key="replay", llm_cache=False, agent_llm=False)
    elif not a.api:
        s = dataclasses.replace(s, llm_mode="offline")
    elif a.record:
        s = dataclasses.replace(s, llm_cache=False)
    cases = yaml.safe_load((ROOT / "tests" / "golden" / "questions.yaml").read_text(encoding="utf-8"))
    hold_file = ROOT / "tests" / "golden" / "holdout.yaml"
    holdout = yaml.safe_load(hold_file.read_text(encoding="utf-8")) if hold_file.exists() else []
    if a.replay:
        replay = llm_record.Replay(Path(a.replay))
        orch = Orchestrator(s, llm=LLMClient(s, transport=replay))
    else:
        orch = Orchestrator(s)
        if a.api and a.record:
            llm_record.attach(orch.llm, OUTPUT_DIR / "llm_record_golden.jsonl")
    by = defaultdict(lambda: [0, 0])
    wrong, calls, cost, t0 = [], 0, 0.0, time.perf_counter()
    v_n, v_ok, v_fail = 0, 0, defaultdict(int)   # 검증: 분석 결과 있는 문항의 검사 통과 수
    for c in cases:
        resp = orch.ask(c["q"])
        bad = check(resp, c["expect"])
        it = c["expect"]["intent"]
        by[it][1] += 1
        by[it][0] += not bad
        calls += resp.llm_usage.calls
        cost += resp.llm_usage.est_cost_usd
        if bad:
            wrong.append((c["q"], ", ".join(bad)))
        if resp.verification is not None:
            v_n += 1
            v_ok += resp.verification.passed
            for ch in resp.verification.checks:
                v_fail[ch.name] += not ch.ok
    n, ok = len(cases), sum(v[0] for v in by.values())
    lines = [f"# 골든 질문셋 결과 ({'API · ' + s.llm_model if s.llm_enabled else '오프라인(규칙)'}, 데이터 {s.data_mode})", "",
             f"- 전체 정답률(개발 세트): {ok}/{n} = {ok / n:.1%}",
             f"- 질문당 평균 LLM 호출 {calls / n:.1f}회, 평균 예상 비용 ${cost / n:.5f}, 전체 {time.perf_counter() - t0:.1f}초",
             f"- 검증 에이전트 전 항목 통과: {v_ok}/{v_n}문항 (분석 결과가 있는 문항) · 항목별 실패: "
             + (", ".join(f"{k} {v}건" for k, v in v_fail.items() if v) or "0건"), "",
             "| 의도 | 정답 | 문항 | 정답률 |", "|---|---|---|---|"]
    lines += [f"| {k} | {v[0]} | {v[1]} | {v[0] / v[1]:.0%} |" for k, v in sorted(by.items())]
    if wrong:
        lines += ["", "## 틀린 문항", *[f"- {q} → {why}" for q, why in wrong]]
    if holdout:   # 보류 세트: 규칙 고칠 때 안 본 질문 → '처음 보는 질문' 정답률
        h_wrong = []
        for c in holdout:
            bad = check(orch.ask(c["q"]), c["expect"])
            if bad:
                h_wrong.append((c["q"], ", ".join(bad)))
        h_ok = len(holdout) - len(h_wrong)
        lines += ["", f"## 보류 세트(처음 보는 질문) — {h_ok}/{len(holdout)} = {h_ok / len(holdout):.1%}",
                  "규칙을 고칠 때 보지 않은 질문입니다. 틀린 문항은 고치지 않고 그대로 보고합니다(고치면 개발 세트로 옮김)."]
        lines += [f"- {q} → {why}" for q, why in h_wrong]
    text = "\n".join(lines)
    print(text)
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "golden_report.md").write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
