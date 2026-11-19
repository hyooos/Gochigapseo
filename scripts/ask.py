"""터미널에서 바로 질문 (개발용): python scripts/ask.py "질문1" ["질문2" ...]"""
import itertools
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jeju_agent.config import get_settings  # noqa: E402
from jeju_agent.orchestrator import Orchestrator  # noqa: E402


def width(s: str) -> int:
    """터미널 칸 수 (한글은 2칸)"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def table(columns: list[str], rows: list[list]) -> str:
    """열 맞춘 표. 숫자는 오른쪽, 글자는 왼쪽. 테두리는 ASCII (윈도우 콘솔에서 안 밀리게)"""
    cells = [["" if v is None else str(v) for v in row] for row in rows]
    numeric = [all(row[i] is None or isinstance(row[i], (int, float)) for row in rows) for i in range(len(columns))]
    ws = [max(width(v) for v in [c, *(row[i] for row in cells)]) for i, c in enumerate(columns)]

    def line(vals: list[str]) -> str:
        pad = [" " * (w - width(v)) for v, w in zip(vals, ws)]
        return "  " + "  ".join(p + v if num else v + p for v, p, num in zip(vals, pad, numeric))

    return "\n".join([line(columns), "  " + "  ".join("-" * w for w in ws), *map(line, cells)])


def main(questions: list[str]) -> None:
    orch = Orchestrator(get_settings())
    memory: dict = {}
    for q in questions:
        resp = orch.ask(q, memory)
        print("\n" + "=" * 70)
        print(f"Q. {q}")
        print(f"   {resp.plan.understood}  (해석: {resp.plan.parser})")
        print("=" * 70 + "\n")
        print(resp.answer)
        for r in resp.results:
            for t in r.evidence:
                print(f"\n[근거표 {t.id}] {t.title}  [{t.data_label}]")
                print(table(t.columns, t.rows))
        print("\n실행 기록")
        steps = resp.trace + [s for r in resp.results for s in r.trace]
        for agent, group in itertools.groupby(steps, key=lambda s: s.agent):
            print(f"  {agent:<12}  " + " → ".join(s.action for s in group))
        v = resp.verification
        if v is not None:
            print(f"검증: {sum(c.ok for c in v.checks)}/{len(v.checks)} 통과"
                  + "".join(f"\n  ✗ {c.name}: {c.detail}" for c in v.checks if not c.ok))
        u = resp.llm_usage
        print(f"LLM: {resp.answer_source}, 호출 {u.calls}회(캐시 {u.cached}회), 예상 비용 ${u.est_cost_usd}")
        errs = orch.llm.question_errors()
        if errs:
            print("LLM 오류:", "; ".join(errs))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('사용법: python scripts/ask.py "질문" ["이어서 할 질문" ...]')
        sys.exit(1)
    main(sys.argv[1:])
