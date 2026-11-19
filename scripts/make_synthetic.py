"""[가상] 샘플 데이터 생성: python scripts/make_synthetic.py
시드 고정이라 몇 번 돌려도 같은 파일. 결과: data/sample/*.csv, data/sample/_truth.json
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jeju_agent.data.synthetic import write  # noqa: E402

if __name__ == "__main__":
    for p in write(ROOT / "data" / "sample"):
        print("생성:", p.relative_to(ROOT))
