"""data/raw 원본 구조 요약 (열 이름 + 예시 값). 공개 데이터 전용, 센터 CSV엔 쓰면 안 됨

    python scripts/inspect_raw.py [파일이름 일부]
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from jeju_agent.data.loader import detect_encoding  # noqa: E402

RAW = ROOT / "data" / "raw"
SEEN: dict[tuple, str] = {}   # 열 구성 → 처음 본 파일 이름


def count_lines(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            n += chunk.count(b"\n")
    return n


def show_columns(name: str, df: pd.DataFrame) -> None:
    key = tuple(df.columns)
    if key in SEEN:
        print(f"  열 구성: '{SEEN[key]}' 와 같음 ({len(key)}개)")
        return
    SEEN[key] = name
    print(f"  열 {len(key)}개 (새 형식)")
    for i, c in enumerate(df.columns):
        ex = df.iloc[:, i].dropna().astype(str).unique()[:3].tolist()
        dup = " (같은 이름의 열이 또 있음)" if list(df.columns).count(c) > 1 else ""
        print(f"   - [{i}] {c}{dup}: {ex}")


def describe_csv(path: Path) -> None:
    enc = detect_encoding(path)
    df = pd.read_csv(path, encoding=enc, nrows=3000, dtype=str, on_bad_lines="skip")
    print(f"  인코딩: {enc} | 전체 줄 수(헤더 포함): {count_lines(path):,}")
    show_columns(path.name, df)


def describe_xlsx(path: Path) -> None:
    from python_calamine import CalamineWorkbook

    wb = CalamineWorkbook.from_path(str(path))
    for sheet_name in wb.sheet_names:
        sheet = wb.get_sheet_by_name(sheet_name)
        rows = sheet.to_python(nrows=3001)
        print(f"  시트 '{sheet_name}': {sheet.height:,}행 × {sheet.width}열")
        if not rows:
            continue
        # 첫 줄이 제목인 경우 대비해서 빈칸 제일 적은 앞쪽 줄을 열 이름으로 봄
        head_idx = max(range(min(5, len(rows))), key=lambda i: sum(v not in ("", None) for v in rows[i]))
        if head_idx:
            print(f"  (맨 위 {head_idx}줄은 제목으로 보고 건너뜀: {[v for v in rows[0] if v][:3]})")
        df = pd.DataFrame(rows[head_idx + 1:], columns=[str(c).strip() for c in rows[head_idx]])
        show_columns(f"{path.name}/{sheet_name}", df)


def main() -> None:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(p for p in RAW.rglob("*") if p.is_file() and pattern in p.name)
    if not files:
        print("data/raw 에 파일이 없습니다.")
        return
    for p in files:
        mb = p.stat().st_size / 1e6
        print(f"\n■ {p.relative_to(RAW)}  ({mb:,.1f} MB)")
        try:
            suffix = p.suffix.lower()
            if suffix in {".csv", ".txt"}:
                describe_csv(p)
            elif suffix in {".xlsx", ".xls"}:
                describe_xlsx(p)
            elif suffix == ".zip":
                with zipfile.ZipFile(p) as z:
                    for info in z.infolist()[:20]:
                        print(f"   - (zip 안) {info.filename}  {info.file_size / 1e6:,.1f} MB")
            else:
                print("  (지원하지 않는 형식: 어떤 파일인지 알려 주세요)")
        except ModuleNotFoundError:
            print("  python-calamine 이 없습니다 → 파일 맨 위 설명의 pip 명령을 먼저 실행하세요.")
        except Exception as ex:
            print(f"  읽기 실패: {type(ex).__name__}: {ex}")


if __name__ == "__main__":
    main()
