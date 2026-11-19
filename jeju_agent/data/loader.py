"""CSV 읽기. 센터 CSV(UTF-8·영문 열)랑 공개 파일(CP949 많음, UTF-8 BOM 일부) 둘 다 처리
데이터를 화면·로그에 찍으면 안 됨 (반출 기준: head() 같은 샘플 출력 금지)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml
from charset_normalizer import from_bytes

from ..config import CONFIG_DIR

_CANDIDATES = ("utf-8-sig", "utf-8", "cp949", "euc-kr")


def detect_encoding(path: Path, sample_bytes: int = 200_000) -> str:
    data = Path(path).read_bytes()
    raw = data[:sample_bytes]
    truncated = len(data) > sample_bytes
    for enc in _CANDIDATES:  # 흔한 순서대로 시도 (utf-8-sig는 BOM 있든 없든 읽음)
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError as e:
            # 표본 끝에서 다바이트 글자 잘린 건 성공으로 봄
            if truncated and e.start >= len(raw) - 3:
                return enc
            continue
    best = from_bytes(raw).best()
    return best.encoding if best else "utf-8"


@lru_cache(maxsize=1)
def _schema() -> dict:
    return yaml.safe_load((CONFIG_DIR / "schema.yaml").read_text(encoding="utf-8"))["tables"]


def normalize_columns(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """원본 열 이름 → 표준 이름, 필수 열 검사"""
    spec = _schema()[table]
    rename: dict[str, str] = {}
    lower = {c.lower().strip(): c for c in df.columns}
    for std, aliases in spec["columns"].items():
        for name in [std, *aliases]:
            hit = lower.get(str(name).lower().strip())
            if hit is not None and hit not in rename:
                rename[hit] = std
                break
    out = df.rename(columns=rename)
    missing = [c for c in spec["required"] if c not in out.columns]
    if missing:
        raise ValueError(f"[{table}] 필수 열 없음: {missing} — config/schema.yaml 의 aliases에 원본 열 이름을 추가하세요.")
    return out


def read_csv_auto(path: str | Path, table: str | None = None, **kw) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_csv(path, encoding=detect_encoding(path), **kw)
    df.columns = [str(c).strip() for c in df.columns]
    return normalize_columns(df, table) if table else df
