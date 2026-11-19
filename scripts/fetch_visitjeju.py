"""비짓제주 사진 짝 맞추기 → data/visitjeju/visitjeju_photos.csv (VISITJEJU_KEY 필요)

    python scripts/fetch_visitjeju.py

다 만들고 나서 출력된 짝을 눈으로 훑어서 엉뚱한 짝 없는지 확인
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from jeju_agent.config import load_settings  # noqa: E402
from jeju_agent.data.visitjeju import fetch_all, match  # noqa: E402

OUT = ROOT / "data" / "visitjeju" / "visitjeju_photos.csv"


def main() -> None:
    load_settings()   # .env를 환경 변수로
    key = (os.environ.get("VISITJEJU_KEY") or "").strip()
    if not key:
        sys.exit("VISITJEJU_KEY가 비어 있습니다. .env에 비짓제주 API 키를 넣어 주세요.")
    items = fetch_all(key)
    places = pd.read_csv(ROOT / "data" / "public" / "places.csv", encoding="utf-8-sig")
    m = match(places, items)
    OUT.parent.mkdir(exist_ok=True)
    m.to_csv(OUT, index=False, encoding="utf-8-sig")
    names = dict(zip(places["place_id"], places["name"]))
    print(f"비짓제주 콘텐츠 {len(items)}건 → 우리 장소 {len(places)}곳 중 {len(m)}곳에 사진 짝: {OUT.relative_to(ROOT)}")
    for r in m.itertuples():
        print(f"  {r.place_id} {names[r.place_id]}  ↔  {r.title}  ({r.distance_km if pd.notna(r.distance_km) else '좌표 없음'} km)")


if __name__ == "__main__":
    main()
