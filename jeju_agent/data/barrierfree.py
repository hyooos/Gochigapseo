"""데이터허브 무장애 원본 → 표준 표(places, place_points)
원본마다 형식(2019·2020·2021 코스)이 달라서 좌표 열만 이름으로 찾고 나머지는 서술로 읽음
시설 값: 1(있음) · 0(없음) · 빈칸(정보 없음). 원본 값은 화면·로그에 찍으면 안 됨
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

from ..config import CONFIG_DIR, ROOT
from .loader import detect_encoding
from .regions import in_jeju, infer_emd, parse_address

FACILITIES = ("acc_toilet", "acc_parking", "wheelchair_rental", "stroller_rental", "ramp", "elevator", "braille", "audio_guide")
BARRIERS = ("stairs", "threshold", "inaccessible", "rough")

EV_COLUMNS = [f"ev_{f}" for f in FACILITIES]   # 시설 판정 근거 문장(원문 일부). 사람이 바로 대조할 수 있게 남김
PLACE_COLUMNS = ["place_id", "name", "kind", "category", "indoor", "region", "emd", "lat", "lng", "addr",
                 *FACILITIES, "n_points", "n_stairs", "n_threshold", "n_inaccessible", "n_rough",
                 "max_slope_deg", "n_slope_records", "fee_won", "data_date", "date_basis", "source_file", "location_note",
                 *EV_COLUMNS]
# data/processed에서만 오는 열 (원본 CSV면 빈칸 = 모름)
REC_COLUMNS = ["rec_intellectual", "rec_wheelchair_manual", "rec_wheelchair_electric", "rec_physical_walking",
               "rec_hearing", "rec_visual", "rec_senior", "rec_infant"]          # 코스 추천 대상: Y / Y(동반) / N
INFO_COLUMNS = ["route", "parking_location", "toilet_location", "alt_toilet", "fee_note", "hours_note"]
PLACE_COLUMNS += REC_COLUMNS + INFO_COLUMNS
SNIPPET_MAX = 60
POINT_COLUMNS = ["place_id", "seq", "lat", "lng", "point_name", "detail", "slope_deg", *BARRIERS, "toilet", "parking",
                 "recommended"]

_LAT = ("위도", "lat", "latitude", "y좌표", "y")
_LNG = ("경도", "lon", "lng", "longitude", "x좌표", "x")
_SEQ = ("순서", "id", "번호", "no", "seq")
_POINT = ("포인트", "point", "장소명칭", "지점명", "구분")
_NAME = ("관광지명", "장소명", "코스명", "명칭", "title")
_ADDR = ("주소", "소재지", "address", "addr")
_DATE = ("데이터기준일자", "기준일자", "base_date")
_RECOMMEND = ("추천코스여부", "추천여부", "추천코스")
_SKIP_TEXT = ("image", "이미지", "사진", "url", "전체코스여부")


@lru_cache(maxsize=1)
def keywords() -> dict:
    return yaml.safe_load((CONFIG_DIR / "keywords.yaml").read_text(encoding="utf-8"))


def compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


# ── 낱말 찾기 ──

def facility_flag(texts: list[str], words: list[str], negative: list[str]) -> int | None:
    """'있음'으로 나오면 1, '없음'으로만 나오면 0, 안 나오면 None"""
    return facility_evidence(texts, words, negative)[0]


def _snippet(text: str, word: str) -> str:
    """원문에서 낱말 앞뒤 잘라서 근거 문장으로 씀 (띄어쓰기 달라도 찾음)"""
    t = re.sub(r"^\d+\s+", "", re.sub(r"\s+", " ", str(text)).strip())   # 앞에 붙은 지점 번호 뺌
    m = re.search(r"\s*".join(map(re.escape, word)), t)
    if len(t) <= SNIPPET_MAX or m is None:
        return t[:SNIPPET_MAX] + ("…" if len(t) > SNIPPET_MAX else "")
    a = max(0, m.start() - 20)
    b = min(len(t), a + SNIPPET_MAX)
    return ("…" if a else "") + t[a:b] + ("…" if b < len(t) else "")


def facility_evidence(texts: list[str], words: list[str], negative: list[str]) -> tuple[int | None, str | None]:
    """(판정, 근거 문장). '있음' 근거 먼저, 없으면 '없음' 근거"""
    pos = neg = None
    for t in texts:
        c = compact(t)
        for w in words:
            for m in re.finditer(re.escape(w), c):
                tail = c[m.end(): m.end() + 8]
                if any(n in tail for n in negative):
                    neg = neg or _snippet(t, w)
                else:
                    pos = pos or _snippet(t, w)
    if pos:
        return 1, pos
    if neg:
        return 0, neg
    return None, None


def barrier_flags(text: str) -> dict[str, int]:
    kw = keywords()
    c = compact(text)
    out = {}
    for b in BARRIERS:
        cc = c
        for ex in kw.get("barrier_exceptions", {}).get(b, []):
            cc = cc.replace(ex, "")
        out[b] = int(any(w in cc for w in kw["barriers"][b]))
    return out


# 앞이 숫자·점이면(1100도로 등) 경사 아님. '도시·도민·도청·도움·도착·도보…'도 뺌
_DEG = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d+)?)\s*(?:도|[°˚º])(?!시|민|내|청|로변|립|서관|움|착|보)")
_PCT = re.compile(r"경사\D{0,6}?(\d{1,2}(?:\.\d+)?)\s*%")


def parse_slopes(text: str) -> list[float]:
    """서술 속 경사(도). '8도', '경사 12%'(→ 각도 변환). 0.5~45도만"""
    s = str(text or "")
    vals = [float(v) for v in _DEG.findall(s)]
    vals += [math.degrees(math.atan(float(p) / 100)) for p in _PCT.findall(s)]
    return [round(v, 1) for v in vals if 0.5 <= v <= 45]


def classify(name: str, texts: list[str]) -> tuple[str, int | None]:
    """(분류, 실내 여부). 이름 먼저 보고, 모르면 서술의 실내·실외 낱말 수 비교"""
    kw = keywords()
    n = compact(name)
    category = next((cat for cat, words in kw["categories"].items() if any(w in n for w in words)), "기타")
    if any(w in n for w in kw["indoor"]):
        return category, 1
    if any(w in n for w in kw["outdoor"]):
        return category, 0
    body = compact(" ".join(texts))
    ins = sum(body.count(w) for w in kw["indoor"])
    outs = sum(body.count(w) for w in kw["outdoor"])
    return category, (1 if ins > outs else 0 if outs > ins else None)


# ── 원본 파일 읽기 ──

@dataclass
class RawSite:
    file: str
    name: str
    kind: str                                  # 관광지 | 코스
    data_date: str | None
    columns: list[str]
    points: list[dict] = field(default_factory=list)   # lat, lng, point_name, detail, recommended
    texts: list[str] = field(default_factory=list)     # 장소 전체 서술 (코스형 1행 파일)
    addr: str | None = None
    has_coords: bool = False
    pid: str | None = None                     # processed ID(P001·C01). 없으면 이름으로 만듦
    extra: dict = field(default_factory=dict)  # 정리본 값(REC_COLUMNS·INFO_COLUMNS·fee_won). 표 행에 그대로 얹음


def _find(cols: list[str], names: tuple[str, ...]) -> str | None:
    low = {c.lower().strip(): c for c in cols}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


_FILE_PREFIX = re.compile(r"^(제주특별자치도_)+")
_FILE_SERIES = re.compile(r"(무장애여행정보_|_?무장애여행코스|_?무장애여행위치정보|_?무장애여행)")
_FILE_DATE = re.compile(r"_?(20\d{6})$")
_FILE_NUM = re.compile(r"^(\d{1,3})[.\-_ ]\s*")


def name_from_file(stem: str) -> tuple[str, str | None, str | None]:
    """'제주특별자치도_무장애여행정보_18.박물관은살아있다_20190906' → ('박물관은살아있다', '18', '2019-09-06')."""
    s = _FILE_PREFIX.sub("", stem.strip())
    s = _FILE_SERIES.sub("_", s).strip("_ ")
    date = None
    m = _FILE_DATE.search(s)
    if m:
        d = m.group(1)
        date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        s = s[: m.start()].strip("_ ")
    num = None
    m = _FILE_NUM.match(s)
    if m:
        num, s = m.group(1), s[m.end():]
    return s.strip("_ ") or stem, num, date


def _date(v) -> str | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    t = pd.to_datetime(str(v).strip(), errors="coerce", format="mixed")
    return None if pd.isna(t) else t.strftime("%Y-%m-%d")


def _yes(v) -> int | None:
    s = str(v).strip().upper() if v is not None else ""
    if s in ("Y", "1", "TRUE", "O", "예", "추천"):
        return 1
    if s in ("N", "0", "FALSE", "X", "아니오"):
        return 0
    return None


def read_raw_file(path: str | Path) -> RawSite:
    path = Path(path)
    df = pd.read_csv(path, encoding=detect_encoding(path), dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    cols = list(df.columns)
    name, _num, fdate = name_from_file(path.stem)
    name_col = _find(cols, _NAME)
    if name_col and df[name_col].str.strip().ne("").any():
        name = df[name_col].str.strip().replace("", None).dropna().iloc[0]
    kind = "코스" if "코스" in path.stem else "관광지"
    lat_c, lng_c = _find(cols, _LAT), _find(cols, _LNG)
    seq_c, pt_c = _find(cols, _SEQ), _find(cols, _POINT)
    addr_c, date_c, rec_c = _find(cols, _ADDR), _find(cols, _DATE), _find(cols, _RECOMMEND)
    used = {lat_c, lng_c, seq_c, date_c, rec_c, addr_c, name_col}
    text_cols = [c for c in cols if c not in used and not any(k in c.lower() for k in _SKIP_TEXT)]

    date = None
    if date_c:
        date = next((d for d in map(_date, df[date_c]) if d), None)
    site = RawSite(file=path.name, name=name, kind=kind, data_date=date or fdate, columns=cols)
    if addr_c:
        site.addr = next((a.strip() for a in df[addr_c] if a and a.strip()), None)
    site.has_coords = bool(lat_c and lng_c)
    for i, row in df.iterrows():
        detail = " / ".join(str(row[c]).strip() for c in text_cols if str(row[c]).strip() and c != pt_c)
        point = {"seq": i + 1, "point_name": str(row[pt_c]).strip() if pt_c else "",
                 "detail": detail, "recommended": _yes(row[rec_c]) if rec_c else None,
                 "lat": None, "lng": None}
        if site.has_coords:
            try:
                la, lo = float(row[lat_c]), float(row[lng_c])
                if la > 90 and lo < 90:      # 위도·경도 뒤바뀐 원본
                    la, lo = lo, la
                if in_jeju(la, lo):
                    point["lat"], point["lng"] = la, lo
            except (TypeError, ValueError):
                pass
            site.points.append(point)
        else:
            site.texts.append(" / ".join(filter(None, [point["point_name"], detail])))
    return site


# ── 표준 표 만들기 ──

def place_id_for(name: str) -> str:
    return "BF-" + hashlib.sha1(compact(name).encode("utf-8")).hexdigest()[:6]


def _merge_course_texts(sites: list[RawSite], report: list[str]) -> list[RawSite]:
    """좌표 없는 코스형 파일은 이름 같은 좌표 파일에 서술 합침. 못 찾으면 좌표 없는 장소로 둠"""
    with_pts = {compact(s.name): s for s in sites if s.has_coords}
    out = []
    for s in sites:
        if s.has_coords:
            out.append(s)
            continue
        key = compact(s.name)
        target = with_pts.get(key) or next((v for k, v in with_pts.items() if key in k or k in key), None)
        if target is not None:
            target.texts += s.texts
            target.addr = target.addr or s.addr
            target.kind = "코스" if s.kind == "코스" else target.kind
            report.append(f"  · '{s.file}' 서술을 같은 장소 파일 '{target.file}'에 합침")
        else:
            out.append(s)
            report.append(f"  · '{s.file}' 좌표 열 없음 → 지도·코스에서는 빠짐(현황 집계에는 포함)")
    return out


def standardize(sites: list[RawSite]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """원본 → (places, place_points, 보고 줄). 같은 이름 여러 개면 기준일 최신 것만"""
    kw = keywords()
    report: list[str] = []
    latest: dict[tuple[str, bool], RawSite] = {}
    for s in sites:   # 좌표 파일끼리, 코스형끼리만 비교. 코스형 서술은 아래에서 합침
        k = (s.pid or compact(s.name), s.has_coords)
        if k in latest:
            old = latest[k]
            keep = s if (s.data_date or "") > (old.data_date or "") else old
            report.append(f"  · 같은 장소 '{s.name}' 파일이 2개 → 기준일 {keep.data_date or '모름'}인 '{keep.file}' 사용")
            latest[k] = keep
        else:
            latest[k] = s
    sites = _merge_course_texts(list(latest.values()), report)

    places, points = [], []
    for s in sites:
        pid = s.pid or place_id_for(s.name)
        pts = []
        for p in s.points:
            bf = barrier_flags(" ".join([p["point_name"], p["detail"]]))
            slopes = parse_slopes(p["detail"]) + parse_slopes(p["point_name"])
            body = [p["point_name"], p["detail"]]
            pts.append({"place_id": pid, "seq": p["seq"], "lat": p["lat"], "lng": p["lng"],
                        "point_name": p["point_name"], "detail": p["detail"],
                        "slope_deg": max(slopes) if slopes else None, **bf,
                        "toilet": facility_flag(body, kw["facilities"]["acc_toilet"], kw["negative"]),
                        "parking": facility_flag(body, kw["facilities"]["acc_parking"], kw["negative"]),
                        "recommended": p["recommended"]})
        all_texts = [f"{p['point_name']} {p['detail']}" for p in s.points] + s.texts
        site_slopes = [p["slope_deg"] for p in pts if p["slope_deg"] is not None]
        for t in s.texts:
            site_slopes += parse_slopes(t)
        coords = [(p["lat"], p["lng"]) for p in pts if p["lat"] is not None]
        lat = sum(c[0] for c in coords) / len(coords) if coords else None
        lng = sum(c[1] for c in coords) / len(coords) if coords else None
        city, emd = parse_address(s.addr)
        note = ""
        if (city is None or emd is None) and lat is not None:
            c2, e2 = infer_emd(lat, lng)
            city, emd = city or c2, emd or e2
            note = "읍면동은 좌표로 추정"
        category, indoor = classify(s.name, all_texts)
        text_barriers = [barrier_flags(t) for t in s.texts]
        fev = {f: facility_evidence(all_texts, kw["facilities"][f], kw["negative"]) for f in FACILITIES}
        row = {"place_id": pid, "name": s.name, "kind": s.kind, "category": category, "indoor": indoor,
               "region": city, "emd": emd, "lat": lat, "lng": lng, "addr": s.addr,
               **{f: fev[f][0] for f in FACILITIES}, **{f"ev_{f}": fev[f][1] for f in FACILITIES},
               "n_points": len(pts) or len(s.texts),
               **{f"n_{b}": sum(p[b] for p in pts) + sum(t[b] for t in text_barriers) for b in BARRIERS},
               "max_slope_deg": max(site_slopes) if site_slopes else None, "n_slope_records": len(site_slopes),
               "fee_won": None, "data_date": s.data_date, "source_file": s.file, "location_note": note}
        row.update(s.extra)
        places.append(row)
        points += pts
        found = [f for f in FACILITIES if row[f] == 1]
        report.append(f"- {s.file}: '{s.name}'({s.kind}) 지점 {row['n_points']}개, 좌표 {'있음' if coords else '없음'}, "
                      f"기준일 {s.data_date or '모름'}, 확인된 시설 {', '.join(found) or '없음'}, "
                      f"계단 {row['n_stairs']}·접근불가 {row['n_inaccessible']}·경사 기록 {row['n_slope_records']}")
    pl = pd.DataFrame(places, columns=PLACE_COLUMNS)
    pt = pd.DataFrame(points, columns=POINT_COLUMNS)
    return pl, pt, report


def convert_folder(folder: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    folder = Path(folder)
    files = sorted(p for p in folder.glob("*.csv") if not p.name.startswith("_"))
    shown = folder.resolve().relative_to(ROOT) if folder.resolve().is_relative_to(ROOT) else folder.name
    sites, report = [], [f"원본 폴더: {shown} (CSV {len(files)}개)"]
    for f in files:
        try:
            sites.append(read_raw_file(f))
        except Exception as ex:   # 한 파일 깨져도 나머지는 변환
            report.append(f"- {f.name}: 읽기 실패({type(ex).__name__}) — 파일을 열어 형식을 확인하세요")
    pl, pt, rep = standardize(sites)
    return pl, pt, report + rep


# ── data/processed (build_processed.py 결과) ──

def _processed_points(df: pd.DataFrame) -> list[dict]:
    out = []
    for r in df.itertuples(index=False):
        # access_info는 분류 문구라 낱말 찾기에 넣으면 전부 계단으로 잡힘
        # 실제 내용은 description에 있고, 장애인화장실(Y)만 낱말로 넣음
        detail = " / ".join(filter(None, [r.description.strip(), "장애인화장실" if r.has_disabled_toilet == "Y" else ""]))
        try:
            la, lo = float(r.lat), float(r.lon)
        except ValueError:
            la = lo = None
        ok = la is not None and in_jeju(la, lo)
        out.append({"seq": int(r.seq), "point_name": r.name.strip(), "detail": detail, "recommended": _yes(r.is_recommended),
                    "lat": la if ok else None, "lng": lo if ok else None})
    return out


NOTE_MAX = 120
_FEE_NUM = re.compile(r"(?:일반|성인|어른).{0,40}?(\d{1,3}(?:,\d{3})+|\d{4,6})")


def parse_fee(text: str) -> int | None:
    """입장료 글 → 성인 1인 요금(원). '없음·무료' → 0, 하나로 못 정하면 None"""
    c = compact(text)
    if c in ("없음", "무료"):
        return 0
    m = _FEE_NUM.search(str(text or ""))
    v = int(m.group(1).replace(",", "")) if m else None
    return v if v is not None and 500 <= v <= 100_000 else None


def _note(text: str) -> str:
    """화면에 그대로 보여 줄 짧은 원문. '-'·'없음'·'요금'은 빈칸"""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return "" if compact(t) in ("", "-", "없음", "요금") else t[:NOTE_MAX] + ("…" if len(t) > NOTE_MAX else "")


def _doc_row(text: str, label: str) -> str:
    """md 문서 표 줄 '| 관 람 료 | 일반 9,000원 | |' → '일반 9,000원' (칸 이름 띄어쓰기 달라도 찾음)"""
    m = re.search(r"^\|\s*" + r"\s*".join(map(re.escape, label)) + r"[^|]*\|(.*)$", text, re.M)
    return " ".join(c.strip() for c in m.group(1).split("|") if c.strip()) if m else ""


def _course_extra(r: dict) -> dict:
    """courses.csv 값 → places.csv 열. 장애인 화장실 위치는 '있음·유'일 때만 (아니면 일반 화장실 위치라서)"""
    blank = lambda v: "" if str(v).strip() in ("", "-") else str(v).strip()  # noqa: E731
    out = {c: blank(r.get(c, "")) for c in REC_COLUMNS}
    has_toilet = compact(r.get("disabled_toilet", "")) in ("있음", "유")
    out.update(route=_note(r.get("recommended_route", "")), parking_location=_note(r.get("parking_location", "")),
               toilet_location=_note(r.get("toilet_location", "")) if has_toilet else "",
               alt_toilet=_note(r.get("alt_toilet", "")), fee_note=_note(r.get("admission_fee", "")),
               fee_won=parse_fee(r.get("admission_fee", "")))
    return out


def _place_extra(folder: Path, doc_file: str) -> dict:
    """관광지 문서(docs/P###.md)의 관람료·관람시간. 없으면 빈 값"""
    p = folder / doc_file if doc_file else None
    if p is None or not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    fee = _doc_row(text, "관람료")
    return {"fee_won": parse_fee(fee), "fee_note": _note(fee), "hours_note": _note(_doc_row(text, "관람시간"))}


def read_processed(folder: str | Path) -> list[RawSite]:
    """processed 관광지·코스 → RawSite. ID는 processed 것 그대로 (이름으로 안 이음)"""
    folder = Path(folder)
    load = lambda n: pd.read_csv(folder / n, encoding="utf-8-sig", dtype=str, keep_default_na=False)  # noqa: E731
    places, courses = load("places.csv"), load("courses.csv")
    pp = dict(tuple(load("place_points.csv").groupby("place_id")))
    cp = dict(tuple(load("course_points.csv").groupby("course_id")))
    sites = []
    for r in places.itertuples(index=False):
        pts = pp.get(r.place_id, pd.DataFrame(columns=["base_date"]))
        dates = [d for d in map(_date, pts["base_date"]) if d]
        raw = r.raw_csv or r.raw_zip or r.raw_hwp
        sites.append(RawSite(file=Path(raw).name if raw else r.place_id, name=r.name, kind="관광지",
                             data_date=max(dates) if dates else None, columns=[], points=_processed_points(pts),
                             addr=r.address or None, has_coords=True, pid=r.place_id,
                             extra=_place_extra(folder, r._asdict().get("doc_file", ""))))
    for r in courses.itertuples(index=False):
        pts = cp.get(r.course_id, pd.DataFrame(columns=["base_date", "source_file"]))
        dates = [d for d in map(_date, pts["base_date"]) if d]
        # 코스 개요 시설 칸을 문장으로 넣어서 낱말 찾기가 판정하게 함. '-'·빈칸은 정보 없음
        # summary(소개글)는 '중턱·모래사장' 같은 오탐만 나와서 안 넣음
        texts = [f"{label} {v}" for label, v in (("장애인주차", r.disabled_parking), ("장애인화장실", r.disabled_toilet),
                 ("보조기기 대여", r.assistive_rental), ("편의시설", r.amenities)) if v.strip() not in ("", "-")]
        file = Path(pts["source_file"].iloc[0]).name if len(pts) else r.course_id
        sites.append(RawSite(file=file, name=r.name, kind="코스", data_date=max(dates) if dates else None, columns=[],
                             points=_processed_points(pts), texts=texts,
                             addr=r.address or None, has_coords=True, pid=r.course_id, extra=_course_extra(r._asdict())))
    return sites


def processed_nearby(folder: str | Path) -> pd.DataFrame:
    """코스 주변정보 → 휠체어 진입 'Y'인 음식점·카페·숙소 (2021-12-22 조사)
    '주변 관광지'는 전부 Y라 기본값으로 보고 뺌. 같은 이름이 한 번이라도 N이면 뺌
    """
    f = pd.read_csv(Path(folder) / "course_facilities.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
    f = f[f["category"] != "주변 관광지"]
    conflicted = set(f.loc[f["wheelchair_access"] == "N", "name"])
    f = f[(f["wheelchair_access"] == "Y") & ~f["name"].isin(conflicted)].copy()
    f["phone"] = f["phone"].where(~f["phone"].str.match(r"^01[016789]"), "")   # 개인 휴대폰 같은 번호는 안 실음
    f["distance_km"] = pd.to_numeric(f["distance_km"], errors="coerce")
    f = f.rename(columns={"course_id": "place_id"}).sort_values(["place_id", "distance_km"])
    # address는 맨 끝 (좌표 붙일 때 주소로 찾음, data/README.md 8-3)
    return f[["place_id", "category", "name", "distance_km", "phone", "disabled_toilet", "base_date", "address"]].reset_index(drop=True)


REP_GOOD = ("전경", "입구", "정문", "매표")                        # 앞일수록 대표 사진으로 좋음
REP_BAD = ("주차", "화장실", "계단", "갈림길", "본문 위치 모름")      # 대표로 안 쓰는 사진 (주차장 사진이 첫 사진인 곳이 많음)


BARRIER_WORDS = ("계단", "경사", "턱", "단차", "오르막", "내리막", "자갈", "울퉁", "노면")   # 사진 설명에 든 장애물 낱말
ACCESS_WORDS = ("데크", "화장실", "주차", "승강기", "엘리베이터", "출입구")                # 접근 시설 낱말 (장애물보다 뒤)
MAX_ACCESS, MAX_ROUTE = 4, 4


def processed_photos(folder: str | Path, points: pd.DataFrame) -> pd.DataFrame:
    """앱에 보여 줄 사진만 고름: rep 대표 1장 / access 장애물·접근 최대 4장 / route 코스 최대 4장
    장애물 사진은 조사 지점에 묶인 ZIP 사진만 (HWP 사진 설명은 짐작이라 안 씀). 같은 설명은 한 번만
    """
    folder = Path(folder)
    ph = pd.read_csv(folder / "photos.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
    ph["place_id"] = ph["place_id"].where(ph["place_id"] != "", ph["course_id"])
    hit = points[(points[["stairs", "inaccessible", "threshold", "rough"]].sum(axis=1) > 0) | points["slope_deg"].notna()]
    spots = set(zip(hit["place_id"].astype(str), hit["seq"].astype(int).astype(str)))
    is_spot = [(p, s) in spots for p, s in zip(ph["place_id"], ph["point_seq"])]
    ph["_rank"] = [(s or any(b in c for b in REP_BAD), next((i for i, k in enumerate(REP_GOOD) if k in c), len(REP_GOOD)))
                   for c, s in zip(ph["caption"], is_spot)]
    zip_ = ph["source"] == "zip"
    ph["_acc"] = [0 if s else 1 if z and any(w in c for w in BARRIER_WORDS) else 2 if z and any(w in c for w in ACCESS_WORDS) else 3
                  for c, s, z in zip(ph["caption"], is_spot, zip_)]
    out: list[dict] = []

    for _, g in ph.groupby("place_id", sort=False):
        seen: list[str] = []          # 이미 쓴 설명 (띄어쓰기 뺀 것)

        def fill(rows, kind: str, cap: int) -> None:
            n = 0
            for r in rows:
                k = compact(r.caption)
                if n == cap:
                    break
                if k and any(k in s or s in k for s in seen):   # 같은 걸 다시 찍은 사진
                    continue
                if k:
                    seen.append(k)
                out.append({"place_id": r.place_id, "kind": kind, "point_seq": r.point_seq, "caption": r.caption, "file": r.file})
                n += 1

        before = len(out)
        fill(g.sort_values("_rank", kind="stable").itertuples(), "rep", 1)   # 순위 같으면 원래 순서
        rest = g[~g["file"].isin([o["file"] for o in out[before:]])]
        fill(rest[rest["_acc"] < 3].sort_values("_acc", kind="stable").itertuples(), "access", MAX_ACCESS)
        left = rest[(rest["_acc"] == 3) & ~rest["caption"].str.contains("본문 위치 모름")]   # 설명 없는 HWP 사진은 코스 사진에서 뺌
        m = len(left)
        even = sorted({round(i * (m - 1) / max(1, min(m, MAX_ROUTE) - 1)) for i in range(min(m, MAX_ROUTE))})
        fill((left.iloc[i] for i in even + [i for i in range(m) if i not in even]), "route", MAX_ROUTE)   # 고르게 먼저, 모자라면 나머지로
    return pd.DataFrame(out, columns=["place_id", "kind", "point_seq", "caption", "file"])


def convert_processed(folder: str | Path, dates: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    sites = read_processed(folder)
    pl, pt, rep = standardize(sites)
    if dates is not None:
        pl, msg = apply_dataset_dates(pl, dates)
        rep.append(msg)
    return pl, pt, [f"원본 폴더: data/processed (관광지·코스 {len(sites)}곳)"] + rep


# ── 데이터허브 등록일 (기준일자 없는 관광지용) ──
DATES_FILE = "dataset_dates.csv"   # data_id, title, registered, modified. down_script.py --dates-only로 만듦


def data_id_of(source_file) -> str | None:
    """'622_제주특별자치도_무장애여행정보_한림공원.zip' → '622'"""
    m = re.match(r"(\d+)_", str(source_file or ""))
    return m.group(1) if m else None


def apply_dataset_dates(places: pd.DataFrame, dates: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """기준일자 있으면 date_basis='기준일자', 없으면 등록일로 채우고 '등록일'. 둘 다 없으면 빈칸
    반환: (새 표, 보고서 한 줄)
    """
    reg = {str(k): str(v)[:10] for k, v in zip(dates["data_id"], dates["registered"], strict=False)
           if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v)}
    out = places.copy()
    has = out["data_date"].map(lambda v: isinstance(v, str) and bool(v))
    ids = out["source_file"].map(data_id_of)
    fill = ~has & ids.map(lambda i: i in reg)
    out.loc[has, "date_basis"] = "기준일자"
    out.loc[fill, "data_date"] = ids[fill].map(reg)
    out.loc[fill, "date_basis"] = "등록일"
    left = int((~has & ~fill).sum())
    return out, f"기준일: 조사 기준일자 {int(has.sum())}곳, 데이터허브 등록일로 채움 {int(fill.sum())}곳, 모름 {left}곳"
