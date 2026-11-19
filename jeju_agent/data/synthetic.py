"""[가상] 합성 데이터 (시드 고정). 장소 이름엔 전부 '(가상)'이 붙음
- 센터 표 3개(5-01, 5-02, 3-03) → data/sample/center (sample 모드용)
- 가상 관광지 원본 + 정답: 변환기 테스트용 (파일로 안 남김)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .barrierfree import convert_folder
from .regions import EMD

SEED = 20261114


@dataclass
class Spec:
    name: str
    emd: str
    fmt: str                       # "2019" | "2020" | "course"
    toilet: int | None = None      # 장애인 화장실 1/0/None(언급 없음)
    parking: int | None = None
    wc_rental: bool = False
    stroller: bool = False
    ramp: bool = False
    elevator: bool = False
    braille: bool = False
    audio: bool = False
    slopes: list[float] = field(default_factory=list)
    stairs: int = 0                # 계단 지점 수
    inaccessible: int = 0          # 접근불가 지점 수 (계단이랑 겹칠 수 있음)
    threshold: int = 0
    rough: int = 0
    flat: int = 1
    fee: int | None = None
    offset: tuple[float, float] = (0.0, 0.0)   # 읍면동 중심에서 떨어진 정도(도)


# 일부러 여러 경우 만듦: 누구나 적합 / 휠체어 부적합 / 경사 주의 / 화장실 없음 / 정보 없음 …
SPECS: list[Spec] = [
    Spec("물빛 전시관(가상)", "서홍동", "2020", toilet=1, parking=1, wc_rental=True, stroller=True, ramp=True, elevator=True,
         braille=True, audio=True, slopes=[3.0], flat=2, fee=5000, offset=(0.012, -0.004)),
    Spec("바람오름 탐방로(가상)", "구좌읍", "2020", toilet=None, parking=1, slopes=[12.0, 14.0], stairs=3, inaccessible=2,
         flat=0, fee=0, offset=(-0.02, 0.01)),
    Spec("돌담 해안산책로(가상)", "애월읍", "2019", toilet=None, parking=1, slopes=[6.0], threshold=1, rough=1, flat=2,
         fee=0, offset=(0.01, -0.03)),
    Spec("노을 해변(가상)", "한림읍", "2020", toilet=0, parking=1, slopes=[2.0], flat=2, fee=0, offset=(0.01, -0.02)),
    Spec("푸른숲 치유숲길(가상)", "남원읍", "2020", toilet=1, parking=1, wc_rental=True, ramp=True, slopes=[4.0, 5.5],
         rough=1, flat=3, fee=0, offset=(0.06, -0.02)),
    Spec("옛집 민속마을(가상)", "표선면", "2019", toilet=1, parking=1, wc_rental=True, stroller=True, slopes=[3.5],
         threshold=1, flat=2, fee=10000, offset=(-0.01, 0.0)),
    Spec("별빛 과학관(가상)", "아라동", "2020", toilet=1, parking=1, wc_rental=True, elevator=True, audio=True, flat=2,
         fee=3000, offset=(-0.01, 0.0)),
    Spec("감귤길 마을산책로(가상)", "효돈동", "course", toilet=1, parking=None, slopes=[5.0], flat=1, fee=0,
         offset=(0.005, 0.0)),
    Spec("갯바위 포구길(가상)", "성산읍", "2020", toilet=0, parking=0, slopes=[9.0], stairs=1, rough=2, flat=1, fee=0,
         offset=(0.01, 0.02)),
    Spec("하늘정원 공원(가상)", "노형동", "2019", toilet=1, parking=1, ramp=True, stroller=True, slopes=[4.0], flat=3,
         fee=0, offset=(0.005, 0.005)),
    Spec("파도소리 해변(가상)", "중문동", "2020", toilet=1, parking=1, wc_rental=True, slopes=[3.0], flat=2, fee=0,
         offset=(-0.005, 0.01)),
    Spec("용암동굴 체험관(가상)", "구좌읍", "2019", toilet=1, parking=1, elevator=True, stairs=1, slopes=[7.0], flat=1,
         fee=6000, offset=(-0.03, -0.06)),
    Spec("작은섬 둘레길(가상)", "우도면", "2020", toilet=None, parking=None, slopes=[8.0, 6.5], rough=2, threshold=1,
         flat=1, fee=0, offset=(0.0, 0.0)),
    Spec("녹차밭 체험장(가상)", "안덕면", "2019", toilet=1, parking=1, stroller=True, slopes=[5.0], rough=1, flat=1,
         fee=0, offset=(0.04, 0.02)),
    Spec("등대 해안길(가상)", "대정읍", "2020", toilet=None, parking=1, slopes=[4.5], stairs=1, flat=2, fee=0,
         offset=(-0.01, 0.0)),
    Spec("해녀 기념관(가상)", "구좌읍", "2020", toilet=1, parking=1, wc_rental=True, ramp=True, braille=True, audio=True,
         flat=2, fee=1100, offset=(0.005, 0.03)),
    Spec("억새 오름길(가상)", "조천읍", "2019", toilet=None, parking=1, slopes=[10.0, 13.0], stairs=2, inaccessible=1,
         flat=0, fee=0, offset=(-0.06, 0.03)),
    Spec("수국 정원(가상)", "남원읍", "2020", toilet=1, parking=1, stroller=True, slopes=[3.0], flat=2, fee=6000,
         offset=(0.01, 0.06)),
    Spec("화산섬 미술관(가상)", "한경면", "2019", toilet=1, parking=1, ramp=True, elevator=True, flat=2, fee=4000,
         offset=(0.01, 0.02)),
    Spec("모래언덕 해수욕장(가상)", "표선면", "2020", toilet=1, parking=1, wc_rental=True, slopes=[2.5], rough=1, flat=1,
         fee=0, offset=(-0.005, 0.01)),
    Spec("폭포 전망 산책로(가상)", "천지동", "2019", toilet=1, parking=1, slopes=[6.0, 8.5], stairs=1, flat=1, fee=2000,
         offset=(-0.002, 0.0)),
    Spec("곶자왈 숲길(가상)", "한경면", "2020", toilet=None, parking=1, slopes=[5.0], rough=3, flat=2, fee=0,
         offset=(-0.03, 0.05)),
    Spec("항구 전시관(가상)", "건입동", "2020", toilet=1, parking=None, elevator=True, ramp=True, audio=True, flat=1,
         fee=0, offset=(0.0, 0.0)),
    Spec("들꽃 생태공원(가상)", "조천읍", "2019", toilet=None, parking=None, slopes=[4.0], flat=2, fee=0,
         offset=(-0.02, -0.02)),
    Spec("저수지 둘레길(가상)", "성산읍", "course", toilet=None, parking=1, slopes=[5.5], rough=1, flat=1, fee=0,
         offset=(-0.03, -0.04)),
    Spec("검은모래 해안(가상)", "삼양동", "2020", toilet=1, parking=1, slopes=[3.5], threshold=1, flat=2, fee=0,
         offset=(0.002, 0.0)),
    Spec("자연휴양림 무장애길(가상)", "봉개동", "2019", toilet=1, parking=1, wc_rental=True, ramp=True, slopes=[4.5],
         flat=3, fee=1000, offset=(-0.03, 0.03)),
    Spec("풍차 해안도로(가상)", "구좌읍", "2020", toilet=0, parking=1, slopes=[6.0], threshold=1, inaccessible=1, flat=1,
         fee=0, offset=(0.01, -0.04)),
]
# 좌표 파일이랑 짝 맞는 코스형 파일 (변환기 '서술 합치기' 테스트용)
COURSE_TWINS = ["푸른숲 치유숲길(가상)", "노을 해변(가상)"]
LONE_COURSE = "숲속 쉼터길(가상)"


def _points(sp: Spec) -> list[tuple[str, str]]:
    """사양 → (지점 이름, 서술). 서술은 실제 원본 말투 흉내 (keywords.yaml 낱말)"""
    pts: list[tuple[str, str]] = []
    pts.append(("주차장", {1: "장애인전용주차 2면", 0: "장애인 주차구역 없음", None: "일반 주차 가능"}[sp.parking]))
    entrance = "무턱 출입구" + (", 경사로 설치" if sp.ramp else "")
    thr = sp.threshold
    if thr:
        entrance, thr = "출입구 턱 5cm", thr - 1
    pts.append(("출입구", entrance))
    pts.append(("화장실", {1: "장애인화장실(남녀 구분)", 0: "장애인화장실 없음, 일반 화장실만", None: "일반 화장실"}[sp.toilet]))
    rent = [x for x, on in (("휠체어 무료대여", sp.wc_rental), ("유모차 대여 가능", sp.stroller)) if on]
    if rent:
        pts.append(("매표소", ", ".join(rent)))
    if sp.elevator:
        pts.append(("전시동", "엘리베이터로 2층 이동"))
    guide = [x for x, on in (("점자 안내판", sp.braille), ("음성안내 기기 대여", sp.audio)) if on]
    if guide:
        pts.append(("안내", " · ".join(guide)))
    rough, inacc = sp.rough, sp.inaccessible
    for k, deg in enumerate(sp.slopes, 1):
        text = f"{deg:g}도"
        if rough:
            text += ". 포장상태 좋지않음. (시멘트. 울퉁)"
            rough -= 1
        pts.append((f"산책로 구간{k}", text))
    for k in range(sp.stairs):
        text = "계단 12단"
        if inacc:
            text += ", 휠체어 접근불가"
            inacc -= 1
        pts.append((f"전망대{k + 1}", text))
    for k in range(inacc):
        pts.append((f"정자{k + 1}", "정자 접근로 턱으로 인해 접근불가"))
        thr = max(0, thr - 1)
    for k in range(thr):
        pts.append((f"쉼터{k + 1}", "쉼터 진입 턱 3cm"))
    for k in range(rough):
        pts.append((f"흙길 구간{k + 1}", "흙길 약 200m, 노면 울퉁불퉁"))
    for k in range(sp.flat):
        pts.append((f"데크길{k + 1}", "데크길 평탄, 폭 1.5m"))
    return pts


def truth(sp: Spec) -> dict:
    """사양에서 기대하는 변환 결과 (테스트 정답)"""
    pts = _points(sp)
    det = [d for _, d in pts]
    return {"acc_toilet": sp.toilet, "acc_parking": sp.parking, "max_slope_deg": max(sp.slopes) if sp.slopes else None,
            "n_stairs": sum("계단" in d for d in det),
            "n_inaccessible": sum("접근불가" in d for d in det),
            "n_threshold": sum(("턱" in d and "무턱" not in d) for d in det),
            "wheelchair_rental": 1 if sp.wc_rental else None}


def _site_xy(sp: Spec) -> tuple[float, float]:
    _, la, lo = EMD[sp.emd]
    return la + sp.offset[0], lo + sp.offset[1]


def write_raw(folder: Path, rng: np.random.Generator | None = None) -> list[Path]:
    rng = rng or np.random.default_rng(SEED)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for n, sp in enumerate(SPECS, 1):
        la, lo = _site_xy(sp)
        pts = _points(sp)
        lat = np.round(la + rng.normal(0, 0.0012, len(pts)), 6)
        lng = np.round(lo + rng.normal(0, 0.0015, len(pts)), 6)
        if sp.fmt == "2020":
            df = pd.DataFrame({"순서": range(1, len(pts) + 1), "위도": lat, "경도": lng,
                               "구분": ["편의시설" if p in ("주차장", "화장실", "매표소", "안내") else "이동경로" for p, _ in pts],
                               "포인트": range(1, len(pts) + 1),
                               "상세정보": [f"{p}: {d}" for p, d in pts], "설명": [""] * len(pts),
                               "전체코스여부": [""] * len(pts), "추천코스여부": ["Y" if "데크" in p else "" for p, _ in pts],
                               "데이터기준일자": ["2020-12-22"] * len(pts)})
            path = folder / f"제주특별자치도_무장애여행정보_{n}-{sp.name}_20201222.csv"
            df.to_csv(path, index=False, encoding="cp949")
        elif sp.fmt == "2019":
            df = pd.DataFrame({"ID": range(1, len(pts) + 1), "lat": lat, "lon": lng, "Point": range(1, len(pts) + 1),
                               "info": [p for p, _ in pts], "A": [""] * len(pts), "B": [d for _, d in pts],
                               "Description": [""] * len(pts), "Image": [""] * len(pts), "etc": [""] * len(pts)})
            path = folder / f"제주특별자치도_무장애여행정보_{n}.{sp.name}_20190910.csv"
            df.to_csv(path, index=False, encoding="cp949")
        else:  # 코스형: 좌표 없는 1행 + 같은 이름 좌표 파일(2020형) 한 쌍
            df = pd.DataFrame({"순서": range(1, len(pts) + 1), "위도": lat, "경도": lng, "포인트": range(1, len(pts) + 1),
                               "상세정보": [f"{p}: {d}" for p, d in pts], "데이터기준일자": ["2021-12-22"] * len(pts)})
            df.to_csv(folder / f"제주특별자치도_{sp.name}_무장애여행위치정보_20211222.csv", index=False, encoding="cp949")
            path = _course_row(folder, sp.name, sp.emd)
        out.append(path)
    for name in COURSE_TWINS:
        sp = next(s for s in SPECS if s.name == name)
        out.append(_course_row(folder, name, sp.emd))
    out.append(_course_row(folder, LONE_COURSE, "조천읍"))   # 좌표 파일 없는 코스 → 지도·코스에서 빠지는 경우
    return out


def _course_row(folder: Path, name: str, emd: str) -> Path:
    city = EMD[emd][0]
    row = {"코스명": name, "주소": f"제주특별자치도 {city} {emd}", "개요": "[가상] 교통약자를 고려한 산책 코스",
           "주차장": "주차장 있음", "화장실": "일반 화장실", "코스": "주차장 → 데크길 → 전망 쉼터", "노면상태": "데크·시멘트",
           "경사도": "완만", "추천여부": "추천"}
    path = folder / f"제주특별자치도_{name}_무장애여행코스_20211222.csv"
    pd.DataFrame([row]).to_csv(path, index=False, encoding="utf-8-sig")
    return path


# ── 센터 CSV (명세서 v13 열 구조) ──
STATIONS = [("J101", "[가상] 제주시 관측소", "일도2동"), ("J102", "[가상] 애월 관측소", "애월읍"),
            ("J103", "[가상] 한경 관측소", "한경면"), ("J104", "[가상] 구좌 관측소", "구좌읍"),
            ("J105", "[가상] 성산 관측소", "성산읍"), ("J106", "[가상] 표선 관측소", "표선면"),
            ("J107", "[가상] 남원 관측소", "남원읍"), ("J108", "[가상] 서귀포 관측소", "서홍동"),
            ("J109", "[가상] 중문 관측소", "중문동"), ("J110", "[가상] 대정 관측소", "대정읍")]
_TEMP = [6.0, 7.0, 10.0, 14.0, 18.0, 22.0, 26.5, 27.5, 24.0, 19.0, 13.5, 8.5]       # 월 평균기온 (대략)
_RAIN = [0.45, 0.40, 0.35, 0.33, 0.33, 0.47, 0.52, 0.48, 0.40, 0.25, 0.33, 0.42]    # 비 온 날 비율 (대략)
_WIND = [11.0, 10.5, 9.5, 8.5, 7.5, 7.5, 8.0, 8.5, 9.0, 8.5, 9.5, 10.5]             # 최대풍속 평균 (m/s)


def center_frames(rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    st = pd.DataFrame([{"observatory_code": c, "observatory_name": n, "start_date": "2010-01-01", "end_date": "",
                        "management_office": "[가상] 관리기관", "longitude": EMD[e][2], "latitude": EMD[e][1],
                        "legal_dongri_code": "", "sea_level_altitude": 20} for c, n, e in STATIONS])
    days = pd.date_range("2024-01-01", "2025-12-31", freq="D")
    rows = []
    for code, _, emd in STATIONS:
        south = EMD[emd][1] < 33.35
        m = days.month.to_numpy() - 1
        mean = np.array(_TEMP)[m] + (0.8 if south else 0) + rng.normal(0, 2.0, len(days))
        rain_p = np.array(_RAIN)[m] + (0.04 * np.isin(days.month, [6, 7, 8, 9]) if south else 0.0)
        rain = np.where(rng.random(len(days)) < rain_p, np.round(rng.gamma(1.4, 9.0, len(days)), 1), 0.0)
        wmax = np.round(np.clip(np.array(_WIND)[m] + rng.normal(0, 3.5, len(days)), 1, None), 1)
        typhoon = (np.isin(days.month, [8, 9])) & (rng.random(len(days)) < 0.03)
        wmax = np.where(typhoon, np.round(rng.uniform(18, 28, len(days)), 1), wmax)
        rows.append(pd.DataFrame({
            "base_date": days.strftime("%Y%m%d"), "observatory_code": code,
            "average_temperature": np.round(mean, 1), "lowest_temperature": np.round(mean - 3.5, 1),
            "lowest_temperature_time": "", "highest_temperature": np.round(mean + 4.0 + rng.normal(0, 1.0, len(days)), 1),
            "highest_temperature_time": "", "daily_rainfall": rain, "maximum_wind_speed": wmax,
            "maximum_wind_speed_time": "", "average_wind_speed": np.round(wmax * 0.45, 1), "maximum_wind_speed_direction": ""}))
    daily = pd.concat(rows, ignore_index=True)
    # 버스 정류소: 읍면동 중심 주변 + 관광지 일부(70%) 근처
    stops, k = [], 0
    for emd, (_, la, lo) in EMD.items():
        if emd == "추자면":
            continue
        for _ in range(6):
            k += 1
            stops.append((f"{4050000 + k}", f"[가상] {emd} 정류소{k}", la + rng.normal(0, 0.01), lo + rng.normal(0, 0.012)))
    for i, sp in enumerate(SPECS):
        if i % 10 in (1, 4, 8):      # 일부 관광지는 가까운 정류소 없게
            continue
        la, lo = _site_xy(sp)
        k += 1
        stops.append((f"{4050000 + k}", f"[가상] {sp.name.replace('(가상)', '')} 입구", la + rng.normal(0, 0.0015),
                      lo + rng.normal(0, 0.0015)))
    bus = pd.DataFrame([{"serial_number": i + 1, "base_date": "20251001", "bus_station_id": s, "bus_station_name": n,
                         "bus_city_type": "", "longitude": round(lo, 6), "latitude": round(la, 6)}
                        for i, (s, n, la, lo) in enumerate(stops)])
    return {"weather_stations": st, "weather_daily": daily, "bus_stops": bus}


def generate(folder: Path | None = None) -> dict[str, pd.DataFrame]:
    """메모리에서 만듦 (파일은 folder/raw에만). 반환: places, place_points, 센터 표들"""
    import tempfile

    rng = np.random.default_rng(SEED)
    raw = (folder / "raw") if folder else Path(tempfile.mkdtemp()) / "raw"
    write_raw(raw, rng)
    places, points, report = convert_folder(raw)
    fees = {sp.name: sp.fee for sp in SPECS}
    places["fee_won"] = places["name"].map(fees)
    out = {"places": places, "place_points": points, **center_frames(rng)}
    out["_report"] = report  # type: ignore[assignment]
    return out


def write(folder: Path) -> list[Path]:
    """가상 센터 표만 folder/center에 씀 (관광지 표는 실데이터)"""
    d = generate()
    (folder / "center").mkdir(parents=True, exist_ok=True)
    written = []
    for name in ("weather_stations", "weather_daily", "bus_stops"):
        p = folder / "center" / f"{name}.csv"
        d[name].to_csv(p, index=False, encoding="utf-8")
        written.append(p)
    return written
