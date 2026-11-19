"""에이전트가 데이터 얻는 창구 (repo.get("places")처럼만 쓰면 됨)
- 관광지·사진: data/public ([실데이터])
- 센터 CSV: data/center (이용자가 넣음). 없으면 sample 모드일 때만 data/sample/center ([가상])
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..config import DATA_DIR
from . import kto
from .loader import read_csv_auto

TABLES = ("places", "place_points")
CENTER_TABLES = ("weather_stations", "weather_daily", "bus_stops")
CENTER_INFO = {  # 표 → (명세서 번호, 표 이름). 센터 조회 사이트에서 고르는 이름
    "weather_stations": ("5-01", "날씨 관측소 정보"),
    "weather_daily": ("5-02", "일일 날씨 정보"),
    "bus_stops": ("3-03", "버스 정류소 정보"),
}
REAL_SOURCES = {
    "places": "제주데이터허브 무장애여행정보·무장애여행코스 (제주특별자치도)",
    "place_points": "제주데이터허브 무장애여행정보·무장애여행코스 (제주특별자치도)",
}
_DTYPES = {"place_id": str, "observatory_code": str, "stop_id": str, "base_date": str}


class DataNotReady(RuntimeError):
    pass


@dataclass
class Dataset:
    name: str
    df: pd.DataFrame
    label: str                 # "가상" | "실데이터"
    source: str
    path: str
    extra: dict = field(default_factory=dict)


class Repository:
    def __init__(self, mode: str = "sample", data_dir: Path = DATA_DIR, center_dir: Path | None = None):
        self.mode = mode
        self.data_dir = data_dir
        self.base = data_dir / "public"
        self.center_dir = center_dir or data_dir / "center"
        self._cache: dict[str, Dataset] = {}
        self._photos: pd.DataFrame | None = None
        self._nearby: pd.DataFrame | None = None
        self._visitjeju: pd.DataFrame | None = None
        self._kto: pd.DataFrame | None = None

    @property
    def label(self) -> str:
        """관광지 자료 표시 (항상 실데이터). 센터 표는 Dataset.label 따로 봄"""
        return "실데이터"

    def photos_df(self) -> pd.DataFrame:
        """사진 목록 표. 없으면 빈 표"""
        if self._photos is None:
            p = self.base / "photos.csv"
            self._photos = (pd.read_csv(p, encoding="utf-8-sig", dtype=str, keep_default_na=False) if p.exists()
                            else pd.DataFrame(columns=["place_id", "kind", "point_seq", "caption", "file"]))
        return self._photos

    def nearby_df(self) -> pd.DataFrame:
        """코스 주변정보 표. 없으면 빈 표. 가게 좌표는 안 둠
        카카오 결과는 저장 금지라 코스 짤 때 주소로 실시간 확인하고 버림 (tools/geo.LiveXY)
        """
        if self._nearby is None:
            p = self.base / "nearby.csv"
            nd = (pd.read_csv(p, encoding="utf-8-sig", dtype={"place_id": str, "phone": str}, keep_default_na=False)
                  if p.exists() else pd.DataFrame(columns=["place_id", "category", "name", "distance_km", "phone",
                                                           "disabled_toilet", "base_date", "address"]))
            self._nearby = nd.assign(lat=float("nan"), lng=float("nan"))
        return self._nearby

    def kto_df(self) -> pd.DataFrame:
        """관광공사 무장애 제주 식당 (fetch_kto_barrierfree.py가 받아 둔 표)
        화장실·출입 문장을 나눈 표(data/kto.py). 없으면 빈 표. 실행 중엔 API 안 부름
        """
        if self._kto is None:
            self._kto = kto.load(self.base)
        return self._kto

    def visitjeju_df(self) -> pd.DataFrame:
        """짝 맞춘 비짓제주 표. 없으면 빈 표. 실행 중엔 API 안 부름"""
        if self._visitjeju is None:
            p = self.data_dir / "visitjeju" / "visitjeju_photos.csv"
            self._visitjeju = (pd.read_csv(p, encoding="utf-8-sig", dtype=str, keep_default_na=False) if p.exists()
                               else pd.DataFrame(columns=["place_id", "alltag"]))
        return self._visitjeju

    def visitjeju_photo(self, place_id: str) -> dict | None:
        """비짓제주 대표 사진. 없으면 None"""
        hit = self.visitjeju_df()
        hit = hit[hit["place_id"] == place_id] if "place_id" in hit.columns else hit.iloc[0:0]
        return hit.iloc[0].to_dict() if not hit.empty else None

    def photos(self, place_id: str, kind: str = "rep", limit: int = 4) -> list[tuple[Path, str]]:
        """(사진 파일, 설명). kind: rep · access · route. 파일 있는 것만
        설명은 build_processed가 자동으로 붙인 거라 틀릴 수 있음
        """
        self.photos_df()
        mine = self._photos[(self._photos["place_id"] == place_id) & (self._photos["kind"] == kind)]
        return [(self.base / f, c) for f, c in zip(mine["file"], mine["caption"]) if (self.base / f).exists()][:limit]

    def nearby(self, place_id: str, max_km: float = 3.0) -> list[dict]:
        """코스 주변 휠체어 진입 가능 조사된 곳(2021-12-22): 음식점·카페 2 + 숙소 1. 화장실 Y 먼저, 가까운 순
        관광지(P###)랑 자료 없는 코스는 빈 목록
        """
        self.nearby_df()
        n = self._nearby[self._nearby["place_id"] == place_id]
        n = n.assign(seq=range(1, len(n) + 1))   # 주변 장소 번호(C01-N3의 3) = 같은 기준 장소 행 순서
        n = n[pd.to_numeric(n["distance_km"], errors="coerce") <= max_km]
        n = n.assign(_t=(n["disabled_toilet"] != "Y")).sort_values(["_t", "distance_km"]).drop(columns="_t")
        food = n[n["category"] != "숙박 시설"].head(2)
        return pd.concat([food, n[n["category"] == "숙박 시설"].head(1)]).to_dict("records")

    # ── 읽기 ──
    def _center_files(self, table: str) -> tuple[list[Path], str]:
        files = sorted(self.center_dir.glob(f"{table}*.csv")) if self.center_dir.exists() else []
        if files:
            return files, "실데이터"
        sample = self.data_dir / "sample" / "center" / f"{table}.csv"
        if self.mode == "sample" and sample.exists():
            return [sample], "가상"
        return [], ""

    def has(self, table: str) -> bool:
        if table in CENTER_TABLES:
            return bool(self._center_files(table)[0])
        return (self.base / f"{table}.csv").exists()

    def get(self, table: str) -> Dataset:
        if table not in TABLES + CENTER_TABLES:
            raise KeyError(table)
        if table in self._cache:
            return self._cache[table]
        if table in CENTER_TABLES:
            files, label = self._center_files(table)
            if not files:
                no, nm = CENTER_INFO[table]
                raise DataNotReady(f"센터 {no} {nm} CSV가 없습니다. 화면 왼쪽 메뉴 '센터 자료 넣기'에서 넣을 수 있습니다.")
            df = pd.concat([read_csv_auto(f, table=table, dtype=_DTYPES) for f in files], ignore_index=True)
            no, nm = CENTER_INFO[table]
            source = f"제주빅데이터센터, {nm}({no})" if label == "실데이터" else f"[가상] 센터 {nm}({no}) 명세 구조 합성"
            path = ", ".join(f.name for f in files)
        else:
            p = self.base / f"{table}.csv"
            if not p.exists():
                raise DataNotReady(f"{table} 데이터가 없습니다 ({p.name}). git에서 data/public을 받거나 "
                                   "data/public/README.md 의 변환 절차(scripts/convert_barrierfree.py)를 따르세요.")
            df = read_csv_auto(p, table=table, dtype=_DTYPES)
            label = self.label
            source = REAL_SOURCES[table]
            path = str(p.relative_to(self.data_dir.parent)) if p.is_relative_to(self.data_dir.parent) else str(p)
        ds = Dataset(name=table, df=df, label=label, source=source, path=path)
        self._cache[table] = ds
        return ds

    def places(self) -> pd.DataFrame:
        """관광지 표 + 숫자 열 정리 (빈칸은 NaN = 정보 없음)"""
        df = self.get("places").df.copy()
        for c in ("lat", "lng", "max_slope_deg", "fee_won", "indoor", "acc_toilet", "acc_parking", "wheelchair_rental",
                  "stroller_rental", "ramp", "elevator", "braille", "audio_guide"):
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in ("n_points", "n_stairs", "n_threshold", "n_inaccessible", "n_rough", "n_slope_records"):
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
        return df

    def center_status(self) -> dict[str, dict]:
        """화면 표시용: 센터 표별 파일 수·행 수·라벨"""
        out = {}
        for t in CENTER_TABLES:
            files, label = self._center_files(t)
            rows = None
            if files:
                try:
                    rows = len(self.get(t).df)
                except Exception:  # 형식 틀린 파일은 화면에서 안내만
                    rows = -1
            out[t] = {"no": CENTER_INFO[t][0], "name": CENTER_INFO[t][1], "files": len(files), "rows": rows, "label": label}
        return out

    def clear_cache(self, center_only: bool = False) -> None:
        for k in list(self._cache):
            if not center_only or k in CENTER_TABLES:
                self._cache.pop(k)
