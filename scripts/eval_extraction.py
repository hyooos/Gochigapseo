"""시설 추출 정확도 평가

    python scripts/eval_extraction.py synthetic            # 합성 데이터 (정답 자동)
    python scripts/eval_extraction.py template --n 30      # 사람이 채울 표 → outputs/label_template.csv
    python scripts/eval_extraction.py score outputs/label_template.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from jeju_agent.config import OUTPUT_DIR  # noqa: E402
from jeju_agent.data import Repository  # noqa: E402
from jeju_agent.data.extraction_eval import label_template, report_md, score, truth_labels  # noqa: E402
from jeju_agent.data.synthetic import SPECS, generate, truth  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["synthetic", "template", "score"])
    ap.add_argument("labels", nargs="?", help="score: 사람이 채운 표 CSV")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--show-auto", action="store_true")
    a = ap.parse_args(argv)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if a.cmd == "synthetic":
        places = generate()["places"]
        res = score(places, truth_labels({sp.name: truth(sp) for sp in SPECS}, places))
        md = report_md(res, "[가상] 합성 데이터(생성 시 의도한 값과 비교)")
    else:
        places = Repository().places()
        if a.cmd == "template":
            out = OUTPUT_DIR / "label_template.csv"
            label_template(places, a.n, a.seed, a.show_auto).to_csv(out, index=False, encoding="utf-8-sig")
            print(f"표시용 표를 만들었습니다: {out} ({min(a.n, len(places))}곳)")
            return 0
        if not a.labels:
            print("채점할 표 CSV 경로를 주세요: python scripts/eval_extraction.py score outputs/label_template.csv")
            return 1
        labels = pd.read_csv(a.labels, encoding="utf-8-sig", dtype=str)
        res = score(places, labels)
        if not res["n_labels"]:
            print("human_state 가 채워진 행이 없습니다.")
            return 1
        md = report_md(res, "[실데이터] 사람이 표시한 정답과 비교")
    (OUTPUT_DIR / "extraction_eval.md").write_text(md, encoding="utf-8")
    print(md)
    return 0 if res["wrong_claims"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
