"""관광공사 무장애 여행 정보 API → 제주 식당 CSV (data/public/kto_bf_restaurant.csv)
앱은 이 CSV만 읽음. KTO_KEY 필요 (공공데이터포털 15101897, 하루 1,000회 한도)

    python scripts/fetch_kto_barrierfree.py              # 식당만
    python scripts/fetch_kto_barrierfree.py --lodging    # 숙소도
    python scripts/fetch_kto_barrierfree.py --list-only  # 개수만 (--debug면 응답 원문)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from jeju_agent.config import load_settings  # noqa: E402

BASE = "https://apis.data.go.kr/B551011/KorWithService2"
OUT = ROOT / "data" / "public"
TYPES = {"restaurant": "39", "lodging": "32", "attraction": "12"}   # 관광타입 코드
JEJU_AREA = "39"          # 지역코드 제주
JEJU_LDONG = "50"         # 법정동 시도코드 제주 (areaCode가 안 먹으면 이걸로)
LIST_COLS = ["contentid", "contenttypeid", "title", "addr1", "addr2", "zipcode", "tel", "mapx", "mapy",
             "cat1", "cat2", "cat3", "lclsSystm1", "lclsSystm2", "lclsSystm3", "areacode", "sigungucode",
             "lDongRegnCd", "lDongSignguCd", "createdtime", "modifiedtime", "firstimage"]


class ApiError(RuntimeError):
    pass


def call(op: str, params: dict, key: str) -> dict:
    """공통 호출. 오류 응답(XML·JSON)은 ApiError"""
    base = {"MobileOS": "ETC", "MobileApp": "BOAZJejuAgent", "_type": "json", **params}
    q = urllib.parse.urlencode(base)
    k = key if "%" in key else urllib.parse.quote(key, safe="")   # Encoding 키는 그대로, Decoding 키는 인코딩
    url = f"{BASE}/{op}?serviceKey={k}&{q}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code}: {e.read()[:300]!r}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(f"JSON이 아닌 응답(키·승인 문제일 수 있음): {raw[:300]}")
    head = (data.get("response") or {}).get("header") or {}
    if head.get("resultCode") not in ("0000", "00", None):
        raise ApiError(f"{head.get('resultCode')} {head.get('resultMsg')}")
    return (data.get("response") or {}).get("body") or {}


def items_of(body: dict) -> list[dict]:
    it = (body.get("items") or {})
    if not it:
        return []
    it = it.get("item") if isinstance(it, dict) else it
    return it if isinstance(it, list) else [it] if it else []


def list_all(ctype: str, key: str, debug: bool = False) -> list[dict]:
    """제주 한 관광타입 전체 목록. areaCode랑 lDongRegnCd로 각각 찾아서 합침
    (코드 옮기는 중이라 한쪽만으론 일부만 나옴)
    """
    merged: dict[str, dict] = {}
    for where in ({"areaCode": JEJU_AREA}, {"lDongRegnCd": JEJU_LDONG}):
        out, page = [], 1
        try:
            while True:
                body = call("areaBasedList2", {"contentTypeId": ctype, "numOfRows": 100, "pageNo": page, "arrange": "C", **where}, key)
                if debug and page == 1:
                    print(f"    [진단] {where} 응답: totalCount={body.get('totalCount')} numOfRows={body.get('numOfRows')} "
                          f"pageNo={body.get('pageNo')} 첫 항목={json.dumps(items_of(body)[:1], ensure_ascii=False)[:400]}")
                got = items_of(body)
                out += got
                total = int(body.get("totalCount") or 0)
                if not got or len(out) >= total:
                    break
                page += 1
                time.sleep(0.1)
        except ApiError as e:
            print(f"  ! 목록 조회 실패({where}): {e}")
            continue
        print(f"    {list(where)[0]}로 {len(out)}곳")
        for i in out:
            merged.setdefault(str(i.get("contentid")), i)
    return list(merged.values())


def detail(cid: str, ctype: str, key: str, debug: bool = False) -> dict:
    body = call("detailWithTour2", {"contentId": cid}, key)
    if debug:
        print(f"    [진단] 상세 응답({cid}): {json.dumps(body, ensure_ascii=False)[:600]}")
    got = items_of(body)
    return {k: (v or "").strip() if isinstance(v, str) else v for k, v in (got[0] if got else {}).items() if k != "contentid"}


def city(addr: str) -> str:
    a = (addr or "").replace(" ", "")
    return "제주시" if "제주시" in a else "서귀포시" if "서귀포시" in a else "기타·모름"


def summarize(df: pd.DataFrame, label: str) -> None:
    print(f"\n== {label}: {len(df)}곳 ==")
    print("  시별:", df["addr1"].map(city).value_counts().to_dict())
    print("  좌표 있음:", int(pd.to_numeric(df["mapy"], errors="coerce").notna().sum()))
    if "restroom" not in df:
        print("  (상세 조회를 하지 않아 무장애 항목은 없음)")
        return
    def filled(c):
        return df[c].fillna("").astype(str).str.strip().ne("") if c in df else pd.Series(False, index=df.index)
    rest = df["restroom"].fillna("").astype(str)
    print("  무장애 항목이 하나라도 적힌 곳:", int(df[[c for c in df.columns if c in BF_COLS]].fillna("").astype(str)
                                             .apply(lambda s: s.str.strip().ne("")).any(axis=1).sum()))
    print("  '화장실' 항목 적힘:", int(filled("restroom").sum()),
          "| 그중 '장애인' 포함:", int(rest.str.contains("장애인").sum()),
          "| '없음' 포함:", int(rest.str.contains("없").sum()))
    for c, ko in (("exit", "출입통로"), ("route", "주 출입 접근로"), ("parking", "주차"), ("elevator", "엘리베이터"),
                  ("wheelchair", "휠체어 대여"), ("handicapetc", "지체장애 기타")):
        print(f"  '{ko}' 적힘:", int(filled(c).sum()))
    ex = df.loc[filled("restroom"), ["title", "restroom"]].head(8)
    if len(ex):
        print("  화장실 원문 예시:")
        for r in ex.itertuples():
            print(f"    - {r.title}: {r.restroom[:80]}")


BF_COLS = ["parking", "route", "publictransport", "ticketoffice", "promotion", "wheelchair", "exit", "elevator", "restroom",
           "auditorium", "room", "handicapetc", "braileblock", "helpdog", "guidehuman", "audioguide", "bigprint",
           "brailepromotion", "guidesystem", "blindhandicapetc", "signguide", "videoguide", "hearingroom",
           "hearinghandicapetc", "stroller", "lactationroom", "babysparechair", "infantsfamilyetc"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lodging", action="store_true", help="숙소도 상세 조회")
    ap.add_argument("--list-only", action="store_true", help="목록 개수만(상세 조회 안 함)")
    ap.add_argument("--debug", action="store_true", help="첫 응답 원문을 보여 준다(개수가 이상할 때)")
    args = ap.parse_args()
    load_settings()   # .env를 환경 변수로
    key = (os.environ.get("KTO_KEY") or "").strip()
    if not key:
        sys.exit("KTO_KEY가 비어 있습니다. .env에 공공데이터포털 '한국관광공사_무장애 여행 정보' 인증키를 넣어 주세요.")

    print("목록 개수(제주 전체):")
    lists = {}
    for name, code in TYPES.items():
        lists[name] = list_all(code, key, args.debug)
        print(f"  {name}({code}): {len(lists[name])}곳 · 시별 {pd.Series([city(i.get('addr1')) for i in lists[name]]).value_counts().to_dict() if lists[name] else {}}")
    if args.list_only or not lists["restaurant"]:
        if not lists["restaurant"]:
            print("\n식당 목록이 비었습니다. 키 승인(활용신청 직후엔 1시간쯤 걸릴 수 있음)과 KTO_KEY 값을 확인하세요.")
        return

    targets = ["restaurant"] + (["lodging"] if args.lodging else [])
    calls = sum(len(lists[t]) for t in targets)
    print(f"\n상세(무장애) 조회 {calls}회 시작 — 개발계정 하루 한도(보통 1,000회) 안인지 확인하세요.")
    for t in targets:
        rows = []
        for n, it in enumerate(lists[t], 1):
            row = {c: it.get(c, "") for c in LIST_COLS}
            try:
                row.update(detail(it["contentid"], TYPES[t], key, args.debug and n <= 2))
            except ApiError as e:
                print(f"  ! {it.get('title')}: {e}")
                if "LIMITED" in str(e).upper() or "22" == str(e)[:2]:
                    print("  호출 한도에 걸린 것 같습니다. 받은 데까지 저장합니다.")
                    rows.append(row)
                    break
            rows.append(row)
            if n % 50 == 0:
                print(f"  {t}: {n}/{len(lists[t])}")
            time.sleep(0.05)
        df = pd.DataFrame(rows)
        df["fetched"] = date.today().isoformat()
        path = OUT / f"kto_bf_{t}.csv"
        df.to_csv(path, index=False, encoding="utf-8")
        summarize(df, f"{t} → {path.name}")
    print("\n앱은 data/public/kto_bf_restaurant.csv만 읽습니다(jeju_agent/data/kto.py). 받은 뒤 pytest로 확인하세요.")


if __name__ == "__main__":
    main()
