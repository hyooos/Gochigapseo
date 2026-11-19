import pandas as pd
import pytest

from jeju_agent.data import Repository
from jeju_agent.data.loader import detect_encoding, read_csv_auto


@pytest.mark.parametrize("enc", ["cp949", "utf-8-sig", "utf-8"])
def test_reads_korean_csv_in_any_encoding(tmp_path, enc):
    p = tmp_path / f"s_{enc}.csv"
    pd.DataFrame({"place_id": ["BF-1"], "장소명": ["물빛 전시관(가상)"], "위도": [33.25], "경도": [126.56]}).to_csv(p, index=False, encoding=enc)
    df = read_csv_auto(p, table="places")
    assert list(df[["place_id", "name", "lat", "lng"]].iloc[0]) == ["BF-1", "물빛 전시관(가상)", 33.25, 126.56]


def test_center_columns_follow_spec_names(tmp_path):
    """센터 CSV는 명세서 영문 열 이름 그대로(latitude·longitude·bus_station_id) 들어와도 표준 이름으로 바뀐다."""
    p = tmp_path / "bus.csv"
    pd.DataFrame({"serial_number": [1], "base_date": ["20251001"], "bus_station_id": ["4050001"], "bus_station_name": ["정류소"],
                  "bus_city_type": [""], "longitude": [126.5], "latitude": [33.5]}).to_csv(p, index=False)
    df = read_csv_auto(p, table="bus_stops", dtype={"bus_station_id": str})
    assert {"stop_id", "stop_nm", "lat", "lng"} <= set(df.columns)


def test_truncated_sample_does_not_misdetect_utf8(tmp_path):
    p = tmp_path / "big.csv"
    p.write_text("이름\n" + "가나다라마바사\n" * 20000, encoding="utf-8")
    assert detect_encoding(p, sample_bytes=1001).startswith("utf-8")


def test_missing_required_column_gives_actionable_error(tmp_path):
    p = tmp_path / "bad.csv"
    pd.DataFrame({"foo": [1]}).to_csv(p, index=False)
    with pytest.raises(ValueError, match="schema.yaml"):
        read_csv_auto(p, table="places")


def test_center_data_prefers_uploaded_files(tmp_path):
    """센터 폴더에 이용자가 넣은 CSV가 있으면 그것([실데이터])을 쓰고, 없으면 sample 모드에서만 [가상]을 쓴다."""
    repo = Repository("sample", center_dir=tmp_path / "center")
    assert repo.get("weather_stations").label == "가상"
    (tmp_path / "center").mkdir()
    pd.DataFrame({"observatory_code": ["X1"], "observatory_name": ["관측소"], "longitude": [126.5], "latitude": [33.5]}).to_csv(
        tmp_path / "center" / "weather_stations_1.csv", index=False)
    repo2 = Repository("sample", center_dir=tmp_path / "center")
    ds = repo2.get("weather_stations")
    assert ds.label == "실데이터" and len(ds.df) == 1 and "제주빅데이터센터" in ds.source
    real = Repository("real", center_dir=tmp_path / "nothing")
    assert not real.has("weather_daily")


def test_photos_by_kind(tmp_path):
    """종류(대표·장애물·코스)별로 고른다. 파일이 없는 행은 건너뛰고, photos.csv가 없으면 빈 목록."""
    assert Repository(data_dir=tmp_path).photos("P001") == []                # 사진 없음 → 사진 없이 동작
    d = tmp_path / "public"
    (d / "photos" / "P001").mkdir(parents=True)
    for n in (1, 2, 3):
        (d / "photos" / "P001" / f"P001-00{n}.jpg").write_bytes(b"x")
    (d / "photos.csv").write_text(
        "place_id,kind,point_seq,caption,file\n"
        "P001,rep,1,전경,photos/P001/P001-001.jpg\n"
        "P001,access,3,계단길,photos/P001/P001-002.jpg\n"
        "P001,access,3,없는 파일,photos/P001/P001-009.jpg\n"
        "P001,route,,관람로,photos/P001/P001-003.jpg\n"
        "C01,rep,,코스,photos/C01/C01-001.jpg\n", encoding="utf-8")
    r = Repository(data_dir=tmp_path)
    assert [c for _, c in r.photos("P001")] == ["전경"] and [c for _, c in r.photos("P001", "access")] == ["계단길"]
    assert [c for _, c in r.photos("P001", "route")] == ["관람로"]
    assert r.photos("C01") == []                                            # 목록에는 있지만 파일 없음
