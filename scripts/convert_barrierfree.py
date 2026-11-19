"""데이터허브 무장애 자료 → 앱이 읽는 data/public

    python scripts/convert_barrierfree.py               # data/processed → data/public
    python scripts/convert_barrierfree.py --dates-only  # 등록일만 채움

끝나면 data/public/_convert_report.txt 확인
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

from jeju_agent.data.barrierfree import (  # noqa: E402
    DATES_FILE,
    apply_dataset_dates,
    convert_folder,
    convert_processed,
    processed_nearby,
    processed_photos,
)

PHOTO_PX = 480   # 화면에서 최대 480px로만 보임. 줄여서 zip 용량 줄임 (약 23MB)
# 얼굴 알아볼 수 있는 사람(어린이 포함)·번호판 읽히는 사진은 안 씀
# (전체 사진 눈으로 확인함. 이미 가려진 얼굴, 마스크·뒷모습 사진은 남김)
PHOTO_EXCLUDE = {
    "photos/P119/P119-043.jpg",   # 어린이 얼굴
    "photos/C29/C29-018.jpg",     # 번호판 읽힘
    "photos/P116/P116-003.jpg",   # 관람객 얼굴
    "photos/C18/C18-009.jpg",     # 관광객 두 명 얼굴
    "photos/C02/C02-014.jpg",     # 사람 두 명 얼굴
    "photos/P121/P121-013.jpg",   # 입구 사람 얼굴
    "photos/P111/P111-008.jpg",   # 낚시터 이용객 두 명
}


def copy_photos(proc: Path, out: Path, points) -> int:
    """화면에 쓸 사진만(대표·장애물·코스) 480px로 줄여 out/photos에 쓰고 photos.csv도 씀"""
    sel = processed_photos(proc, points)
    sel = sel[~sel["file"].isin(PHOTO_EXCLUDE)]
    shutil.rmtree(out / "photos", ignore_errors=True)   # 이 스크립트가 만든 폴더. 선택 바뀌면 남은 파일 지움
    for f in sel["file"]:
        (out / f).parent.mkdir(parents=True, exist_ok=True)
        with Image.open(proc / f) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((PHOTO_PX, PHOTO_PX))
            im.save(out / f, "JPEG", quality=72, optimize=True)
    sel.to_csv(out / "photos.csv", index=False, encoding="utf-8")
    return len(sel)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(ROOT / "data" / "raw" / "barrierfree"))
    ap.add_argument("--processed", default=str(ROOT / "data" / "processed"),
                    help="data/build_processed.py 결과 폴더. places.csv가 있으면 --src 대신 이것을 쓴다")
    ap.add_argument("--out", default=str(ROOT / "data" / "public"))
    ap.add_argument("--dates", default=str(ROOT / "data" / DATES_FILE), help="데이터허브 등록일 표 (없으면 건너뜀)")
    ap.add_argument("--dates-only", action="store_true", help="out/places.csv 에 등록일만 채우고 끝낸다")
    a = ap.parse_args(argv)
    src, proc, out = Path(a.src), Path(a.processed), Path(a.out)
    dates = pd.read_csv(a.dates, dtype=str, encoding="utf-8-sig") if Path(a.dates).exists() else None
    if a.dates_only:
        if dates is None or not (out / "places.csv").exists():
            print(f"필요한 파일이 없습니다: {a.dates} 와 {out / 'places.csv'}\n 먼저 python data/down_script.py --dates-only 를 실행하세요.")
            return 1
        places = pd.read_csv(out / "places.csv", dtype={"data_date": str, "date_basis": str}, encoding="utf-8-sig")
        places, msg = apply_dataset_dates(places, dates)
        places.to_csv(out / "places.csv", index=False, encoding="utf-8")
        print(msg)
        return 0
    if dates is None:
        print(f"[알림] {a.dates} 가 없어 등록일을 채우지 않습니다(기준일자 없는 곳은 '모름'). python data/down_script.py --dates-only")
    if (proc / "places.csv").exists():
        places, points, report = convert_processed(proc, dates)
    elif src.exists() and any(src.glob("*.csv")):
        places, points, report = convert_folder(src)
    else:
        print(f"원본이 없습니다: {proc} 또는 {src}\n data/README.md(정리본) 또는 data/public/README.md(원본 CSV)를 참고하세요.")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    places.to_csv(out / "places.csv", index=False, encoding="utf-8")
    points.to_csv(out / "place_points.csv", index=False, encoding="utf-8")
    (out / "_convert_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    no_xy = int(places["lat"].isna().sum())
    print(f"변환 완료: 관광지 {len(places)}곳(좌표 없음 {no_xy}곳), 조사 지점 {len(points)}개")
    if (proc / "photos.csv").exists():
        print(f"사진 {copy_photos(proc, out, points)}장 → {out / 'photos'} (장소마다 대표 1 + 장애물·접근 4 + 코스 4장까지)")
    if (proc / "course_facilities.csv").exists():
        near = processed_nearby(proc)
        near.to_csv(out / "nearby.csv", index=False, encoding="utf-8")
        print(f"코스 주변 휠체어 진입 가능 식당·카페·숙소 {len(near)}곳 → {out / 'nearby.csv'}")
    print(f"입장료 아는 곳 {int(places['fee_won'].notna().sum())}곳 / {len(places)}곳")
    print(f"→ {out / 'places.csv'}\n→ {out / 'place_points.csv'}\n→ {out / '_convert_report.txt'} (파일별 결과 확인)")
    for f in ("acc_toilet", "acc_parking", "wheelchair_rental"):
        n_yes = int((places[f] == 1).sum())
        n_unk = int(places[f].isna().sum())
        print(f"  {f}: 있음 확인 {n_yes}곳, 정보 없음 {n_unk}곳")
    print("'정보 없음'이 너무 많으면 원본 표현을 보고 config/keywords.yaml 에 낱말을 추가한 뒤 다시 실행하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
