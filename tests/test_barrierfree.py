"""데이터허브 무장애여행 원본 변환기 테스트 (형식 3종, 낱말 찾기, 경사 읽기, '정보 없음' 유지)."""
import math
from pathlib import Path

import pandas as pd
import pytest

from jeju_agent.data import barrierfree as bf
from jeju_agent.data.synthetic import LONE_COURSE, SPECS, truth, write, write_raw

KW = bf.keywords()


def flag(*texts, fac="acc_toilet"):
    return bf.facility_flag(list(texts), KW["facilities"][fac], KW["negative"])


def test_facility_three_values():
    assert flag("내부: 장애인화장실(남녀 구분)") == 1
    assert flag("장애인 화장실 없음, 일반 화장실만") == 0
    assert flag("일반 화장실") is None              # 언급 없음 → 정보 없음(없다고 단정하지 않음)
    assert flag("장애인화장실 없음", "별관 장애인화장실 있음") == 1   # 한 곳이라도 있으면 있음
    assert flag("주차장: 장애인 주차구역 없음", fac="acc_parking") == 0


def test_barrier_words_and_exceptions():
    assert bf.barrier_flags("무턱 출입구, 경사로 설치")["threshold"] == 0
    assert bf.barrier_flags("정자 접근로 턱으로 인해 접근불가") == {"stairs": 0, "threshold": 1, "inaccessible": 1, "rough": 0}
    assert bf.barrier_flags("6도. 포장상태 좋지않음. (시멘트. 울퉁)")["rough"] == 1
    assert bf.barrier_flags("전망대 계단 12단")["stairs"] == 1
    assert bf.barrier_flags("계단 없음, 평탄")["stairs"] == 0


@pytest.mark.parametrize("text,want", [
    ("8도", [8.0]),
    ("6도. 포장상태 좋지않음", [6.0]),
    ("경사 12%", [6.8]),
    ("구간 경사 3.5도와 9도", [3.5, 9.0]),
    ("제주도 서귀포시", []),
    ("1100도로 옆 쉼터", []),
    ("경사구간 : 50m/9~12˚. 종단 경사 5˚", [12.0, 5.0]),   # 실데이터는 ˚(U+02DA)로 적힌 곳이 많다
    ("도움이 필요함 2도움", []),
])
def test_parse_slopes(text, want):
    assert bf.parse_slopes(text) == want


@pytest.mark.parametrize("stem,want", [
    ("제주특별자치도_무장애여행정보_18.박물관은살아있다_20190906", ("박물관은살아있다", "18", "2019-09-06")),
    ("제주특별자치도_무장애여행정보_22-신도포구(돌고래소공원)_20201222", ("신도포구(돌고래소공원)", "22", "2020-12-22")),
    ("제주특별자치도_검은여로_무장애여행코스_20211222", ("검은여로", None, "2021-12-22")),
    ("제주특별자치도_행원리풍차마을_무장애여행위치정보", ("행원리풍차마을", None, None)),
])
def test_name_from_file(stem, want):
    assert bf.name_from_file(stem) == want


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    folder = tmp_path_factory.mktemp("raw")
    write_raw(folder)
    return bf.convert_folder(folder)


def test_synthetic_raw_converts_to_intended_values(converted):
    """가상 원본(2019형·2020형 CP949, 코스형 UTF-8)을 변환하면 사양에 적은 값이 그대로 나온다."""
    places, points, report = converted
    by_name = places.set_index("name")
    for sp in SPECS:
        row = by_name.loc[sp.name]
        for k, v in truth(sp).items():
            got = row[k]
            got = None if pd.isna(got) else (float(got) if k == "max_slope_deg" else int(got))
            assert got == v, f"{sp.name} {k}: {got} != {v}"
    assert len(points) == int(places["n_points"].sum()) - 1   # 좌표 없는 코스형 1행은 지점 표에 없음


def test_course_rows_merge_and_lone_course_keeps_no_coords(converted):
    places, _, report = converted
    twin = places.set_index("name").loc["노을 해변(가상)"]
    assert twin["kind"] == "코스" and not pd.isna(twin["lat"]) and twin["region"] == "제주시"   # 주소에서 시를 읽음
    lone = places.set_index("name").loc[LONE_COURSE]
    assert math.isnan(lone["lat"]) and lone["kind"] == "코스"
    text = "\n".join(report)
    assert "좌표 열 없음" in text and "합침" in text


def test_report_never_contains_raw_values(converted):
    """변환 보고에는 열 이름·개수만 있고 서술 원문은 없다."""
    _, points, report = converted
    text = "\n".join(report)
    assert "포장상태" not in text and "정자 접근로" not in text


def test_duplicate_site_keeps_latest(tmp_path):
    df = pd.DataFrame({"위도": [33.4], "경도": [126.5], "상세정보": ["장애인화장실"], "데이터기준일자": ["2019-01-01"]})
    df.to_csv(tmp_path / "제주특별자치도_무장애여행정보_1.같은곳_20190101.csv", index=False, encoding="cp949")
    df.assign(상세정보="장애인화장실 없음", 데이터기준일자="2021-01-01").to_csv(
        tmp_path / "제주특별자치도_무장애여행정보_2-같은곳_20210101.csv", index=False, encoding="cp949")
    places, _, report = bf.convert_folder(tmp_path)
    assert len(places) == 1 and places.iloc[0]["data_date"] == "2021-01-01" and places.iloc[0]["acc_toilet"] == 0


def test_broken_file_does_not_stop_conversion(tmp_path):
    (tmp_path / "깨진파일.csv").write_bytes(b"\x00\x01\x02")
    pd.DataFrame({"lat": [33.4], "lon": [126.5], "info": ["데크길 평탄"]}).to_csv(tmp_path / "ok_20200101.csv", index=False)
    places, _, report = bf.convert_folder(tmp_path)
    assert len(places) >= 1


def test_sample_center_matches_generator(tmp_path):
    """저장소의 data/sample/center 가 생성기 결과와 같다 (생성기를 고치면 make_synthetic.py를 다시 돌려 커밋)."""
    root = Path(__file__).resolve().parents[1] / "data" / "sample"
    for p in write(tmp_path):
        assert (root / "center" / p.name).read_bytes() == p.read_bytes()


def test_read_processed(tmp_path):
    """data/processed 정리본: ID 유지, access_info 분류 문구는 장애물로 안 셈, 코스 시설 칸(있음/없음/-) 판정."""
    pts_cols = "seq,lat,lon,name,description,access_info,has_disabled_toilet,is_recommended,base_date,source_file"
    (tmp_path / "places.csv").write_text(
        "place_id,name,address,raw_csv,raw_zip,raw_hwp\nP001,가상공원,제주특별자치도 제주시 한림읍 협재리 1,,raw/a.zip,\n", encoding="utf-8")
    (tmp_path / "place_points.csv").write_text(
        f"place_id,{pts_cols}\n"
        "P001,1,33.39,126.23,주차장,,접근성 정보 있음 (계단 혹은 경사 있음. 길 상태 안 좋음),N,Y,,x\n"
        "P001,2,33.39,126.24,화장실,경사구간 : 30m/9˚,장애인화장실,Y,N,2020-12-22,x\n", encoding="utf-8")
    (tmp_path / "courses.csv").write_text(
        "course_id,name,address,summary,disabled_parking,disabled_toilet,assistive_rental,amenities\n"
        "C01,가상해안길,제주 제주시 애월읍 하귀2리 1,중턱에서 보는 모래사장,없음,-,없음,없음\n", encoding="utf-8")
    (tmp_path / "course_points.csv").write_text(
        f"course_id,{pts_cols}\nC01,1,33.48,126.39,정자 앞,정자 입구 계단 3개,,N,Y,2021-12-22,raw/c.csv\n", encoding="utf-8")
    places, points, _ = bf.convert_processed(tmp_path)
    p, c = places.set_index("place_id").loc["P001"], places.set_index("place_id").loc["C01"]
    assert (p["kind"], c["kind"]) == ("관광지", "코스") and p["source_file"] == "a.zip"
    assert p["n_stairs"] == 0 and p["acc_toilet"] == 1 and p["max_slope_deg"] == 9.0 and p["data_date"] == "2020-12-22"
    assert c["n_stairs"] == 1 and c["n_threshold"] == 0 and c["n_rough"] == 0   # 소개글(중턱·모래사장)은 안 셈
    assert c["acc_parking"] == 0 and pd.isna(c["acc_toilet"]) and c["region"] == "제주시"
    assert len(points) == 3


def test_processed_photos_picks_representative_and_barrier_spots(tmp_path):
    """대표(전경·입구 우선) → 장애물·접근(조사 지점에 묶인 ZIP 사진만) → 코스(남은 사진을 고르게).
    HWP 설명은 짐작이라 장애물로 쓰지 않고, 한 장소에서 같은(포함 관계) 설명은 한 번만 쓴다."""
    plain = "".join(f"P002-00{i},P002,,,zip,{'길2' if i == 3 else f'길{i}'},p{i}\n" for i in range(1, 7))
    (tmp_path / "photos.csv").write_text(
        "photo_id,place_id,course_id,point_seq,source,caption,file\n"
        "P001-001,P001,,1,zip,주차장,a\nP001-002,P001,,2,zip,평탄한 길,b\nP001-003,P001,,3,zip,턱,c\n"
        "C01-001,,C01,,hwp,코스 입구,d\nC01-002,,C01,,hwp,코스 전경,e\nC01-003,,C01,,hwp,코스 입구 오르막,f\n" + plain,
        encoding="utf-8")
    points = pd.DataFrame({"place_id": ["P001"] * 3, "seq": [1, 2, 3], "stairs": [0, 0, 0], "inaccessible": [0, 0, 0],
                           "threshold": [0, 0, 1], "rough": [0, 0, 0], "slope_deg": [None, None, None]})
    got = list(zip(*bf.processed_photos(tmp_path, points)[["file", "kind"]].T.values))
    # P001: 대표 평탄한 길(b), 장애물은 턱 기록 지점(c) → '주차' 낱말(a)
    assert got[:3] == [("b", "rep"), ("c", "access"), ("a", "access")]
    # C01(HWP): '전경'(e) 대표. '오르막'(f)은 장애물로 안 쓰고, '코스 입구'(d)를 포함한 설명이라 다시 쓰지 않는다
    assert got[3:5] == [("e", "rep"), ("d", "route")]
    # P002: 대표 p1, 고르게 고른 칸 중 p3은 p2와 설명이 같아 빼고 남은 p4로 채운다
    assert got[5:] == [("p1", "rep"), ("p2", "route"), ("p5", "route"), ("p6", "route"), ("p4", "route")]


@pytest.mark.parametrize("text,want", [
    ("없음", 0), ("없 음", 0), ("일반 15,000원/ 청소년 14,000원", 15000), ("1. 어른(25~64세) : 개인 1500원", 1500),
    ("구분 성인 청소년/군인 어린이 개인 3,000원 2,000원", 3000), ("유료(1,000~2,500 / 장애인 무료)", None), ("요금", None), ("", None),
])
def test_parse_fee(text, want):
    assert bf.parse_fee(text) == want                      # 하나로 못 정하면 None(모름) — 지어내지 않는다


def test_course_extra_and_nearby(tmp_path):
    """장애인 화장실 위치는 '있음·유'일 때만, '-'는 빈칸. 주변정보는 '주변 관광지'·조사가 엇갈린 이름을 빼고 휴대전화는 지운다."""
    row = {"rec_wheelchair_manual": "Y(동반)", "rec_infant": "-", "disabled_toilet": "없음", "toilet_location": "일반 화장실",
           "alt_toilet": "마트 화장실", "recommended_route": "1. 주차장 → 2. 반환점", "admission_fee": "없음", "parking_location": "-"}
    ex = bf._course_extra(row)
    assert ex["rec_wheelchair_manual"] == "Y(동반)" and ex["rec_infant"] == "" and ex["toilet_location"] == ""
    assert ex["route"] == "1. 주차장 → 2. 반환점" and ex["fee_won"] == 0 and ex["parking_location"] == ""
    assert bf._course_extra({**row, "disabled_toilet": "유"})["toilet_location"] == "일반 화장실"
    (tmp_path / "course_facilities.csv").write_text(
        "course_id,category,name,distance_km,address,phone,wheelchair_access,disabled_toilet,base_date,source_file\n"
        "C01,음식점,가게,0.5,a,064-1,Y,Y,2021-12-22,x\nC01,주변 관광지,공원,0.1,a,,Y,N,2021-12-22,x\n"
        "C01,카페/편의점,엇갈림,0.2,a,,Y,N,2021-12-22,x\nC02,카페/편의점,엇갈림,0.3,a,,N,N,2021-12-22,x\n"
        "C02,숙박 시설,민박,1.0,a,010-12,Y,N,2021-12-22,x\n", encoding="utf-8")
    n = bf.processed_nearby(tmp_path)
    assert list(n["name"]) == ["가게", "민박"] and list(n["phone"]) == ["064-1", ""]
