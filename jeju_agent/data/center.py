"""센터 CSV(반출 가능 표) 집계: 달별 날씨 비율(5-01·5-02), 가까운 버스 정류소(3-03)
집계값만 돌려줌. 원본 행은 화면·LLM으로 안 보냄
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..tools.geo import haversine_km


def parse_dates(s: pd.Series) -> pd.Series:
    """'20240105'·'2024-01-05'·'2024/01/05' 다 날짜로. 못 읽으면 NaT"""
    raw = s.astype(str).str.strip()
    digits = raw.str.fullmatch(r"\d{8}")
    out = pd.to_datetime(raw.where(digits), format="%Y%m%d", errors="coerce")
    rest = pd.to_datetime(raw.where(~digits), errors="coerce", format="mixed")
    return out.fillna(rest)


def weather_month_stats(daily: pd.DataFrame, month: int, rules: dict) -> pd.DataFrame:
    """관측소별 같은 달(여러 해) 날씨 비율. 열: observatory_code, n_days, rain_ratio, wind_ratio, hot_ratio, years"""
    d = daily.copy()
    d["date"] = parse_dates(d["base_date"])
    d = d[d["date"].dt.month == month]
    cols = ["observatory_code", "n_days", "rain_ratio", "wind_ratio", "hot_ratio", "years"]
    if d.empty:
        return pd.DataFrame(columns=cols)
    for c in ("daily_rainfall", "maximum_wind_speed", "highest_temperature"):
        d[c] = pd.to_numeric(d.get(c), errors="coerce") if c in d else np.nan
    d["rain"] = d["daily_rainfall"] >= rules["rain_mm"]
    d["windy"] = d["maximum_wind_speed"] >= rules["wind_ms"]
    d["hot"] = d["highest_temperature"] >= rules["hot_c"]
    g = d.groupby(d["observatory_code"].astype(str))
    out = pd.DataFrame({
        "n_days": g.size(),
        "rain_ratio": g.apply(lambda x: x["rain"][x["daily_rainfall"].notna()].mean(), include_groups=False),
        "wind_ratio": g.apply(lambda x: x["windy"][x["maximum_wind_speed"].notna()].mean(), include_groups=False),
        "hot_ratio": g.apply(lambda x: x["hot"][x["highest_temperature"].notna()].mean(), include_groups=False),
        "years": g["date"].agg(lambda x: f"{x.dt.year.min()}~{x.dt.year.max()}"),
    }).reset_index().rename(columns={"index": "observatory_code"})
    for c in ("rain_ratio", "wind_ratio", "hot_ratio"):
        out[c] = out[c].astype(float).round(2)
    return out[cols]


def nearest(src: pd.DataFrame, ref: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """src 행마다 제일 가까운 ref 행 위치랑 거리(km). 둘 다 lat·lng 있어야 함"""
    la, lo = src["lat"].to_numpy(float), src["lng"].to_numpy(float)
    rla, rlo = ref["lat"].to_numpy(float), ref["lng"].to_numpy(float)
    dist = haversine_km(la[:, None], lo[:, None], rla[None, :], rlo[None, :])
    idx = np.argmin(dist, axis=1)
    return idx, dist[np.arange(len(src)), idx]


def place_weather(places: pd.DataFrame, stations: pd.DataFrame, stats: pd.DataFrame) -> pd.DataFrame:
    """관광지마다 그 달 자료 있는 제일 가까운 관측소의 날씨 비율 붙임"""
    cols = ["place_id", "observatory_code", "observatory_name", "obs_km", "n_days", "rain_ratio", "wind_ratio", "hot_ratio"]
    pl = places.dropna(subset=["lat", "lng"])
    st = stations.assign(observatory_code=stations["observatory_code"].astype(str))
    st = st[st["observatory_code"].isin(set(stats["observatory_code"]))].dropna(subset=["lat", "lng"])
    if pl.empty or st.empty:
        return pd.DataFrame(columns=cols)
    idx, km = nearest(pl, st.reset_index(drop=True))
    near = st.reset_index(drop=True).iloc[idx].reset_index(drop=True)
    out = pd.DataFrame({"place_id": pl["place_id"].to_numpy(), "observatory_code": near["observatory_code"],
                        "observatory_name": near.get("observatory_name", near["observatory_code"]),
                        "obs_km": np.round(km, 1)})
    return out.merge(stats, on="observatory_code", how="left")[cols]


def nearest_stops(places: pd.DataFrame, stops: pd.DataFrame) -> pd.DataFrame:
    """관광지마다 제일 가까운 정류소, 거리(m), 정류소 좌표 (지도용)"""
    cols = ["place_id", "stop_nm", "stop_m", "stop_lat", "stop_lng"]
    pl = places.dropna(subset=["lat", "lng"])
    sp = stops.dropna(subset=["lat", "lng"]).reset_index(drop=True)
    if pl.empty or sp.empty:
        return pd.DataFrame(columns=cols)
    idx, km = nearest(pl, sp)
    names = sp["stop_nm"] if "stop_nm" in sp else sp["stop_id"]
    return pd.DataFrame({"place_id": pl["place_id"].to_numpy(), "stop_nm": names.iloc[idx].to_numpy(),
                         "stop_m": np.round(km * 1000).astype(int), "stop_lat": sp["lat"].iloc[idx].to_numpy(float),
                         "stop_lng": sp["lng"].iloc[idx].to_numpy(float)})
