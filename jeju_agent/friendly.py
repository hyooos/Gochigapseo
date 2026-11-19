"""화면에 보이는 말. 내부 낱말은 그대로 두고 화면에 낼 때만 부드럽게 바꿈 (원문 인용·장소 이름은 안 바꿈)"""
from __future__ import annotations

import re
from urllib.parse import quote

# ── 입력 버튼 이름 (contracts 값은 그대로, 보이는 글자만) ──
# 묶음마다 맨 앞에 '해당 없음'(NONE)을 기본으로 눌러 둠. 다른 거 누르면 꺼지고, 다시 누르면 나머지가 꺼짐
NONE = "none"
NONE_UI = {NONE: "해당 없음"}
MOBILITY_UI = {**NONE_UI, "wheelchair_manual": "수동 휠체어", "wheelchair_power": "전동 휠체어", "walker": "지팡이·보행기",
               "senior": "오래 걷기 힘들어요", "stroller": "유아차와 함께"}
VISION_UI = {**NONE_UI, "low": "잘 안 보여요", "blind": "보이지 않아요"}
HEARING_UI = {**NONE_UI, "hard": "잘 안 들려요", "deaf": "들리지 않아요"}
COGNITION_UI = {**NONE_UI, "intellectual": "발달·지적장애"}
NEED_UI = {**NONE_UI, "acc_toilet": "휠체어 화장실", "acc_parking": "전용 주차구역", "rest_often": "자주 쉬어야 해요",
           "indoor_pref": "실내가 좋아요"}
TRANSPORT_UI = {"car": "자가용·렌터카", "taxi": "택시·콜택시", "bus": "버스"}
INTEREST_UI = {"sea": "바다·해안", "forest": "숲길·오름·공원", "museum": "박물관·전시", "village": "마을·올레길"}

# 여러 개 고르면 제일 조심해야 하는 조건(기준 엄격한 순)으로 넘김
MOBILITY_STRICT = ("wheelchair_manual", "wheelchair_power", "walker", "stroller", "senior")
VISION_STRICT = ("blind", "low")
HEARING_STRICT = ("deaf", "hard")


def merge_conditions(mobility: list[str], vision: list[str], hearing: list[str], needs: list[str],
                     cognition: list[str] | None = None) -> dict:
    """버튼 여러 개 → 계약 값. 빠지는 조건은 필요 사항으로 남김
    (예: 전동 휠체어 + 오래 걷기 힘듦 → 전동 휠체어 기준 + '자주 쉬어야 함' 더해서 여유 속도)
    """
    mob = next((m for m in MOBILITY_STRICT if m in mobility), "none")
    needs = [n for n in needs if n != NONE]
    if "senior" in mobility and mob != "senior" and "rest_often" not in needs:
        needs.append("rest_often")
    return {"mobility": mob, "vision": next((v for v in VISION_STRICT if v in vision), "none"),
            "hearing": next((h for h in HEARING_STRICT if h in hearing), "none"),
            "cognition": "intellectual" if "intellectual" in (cognition or []) else "none", "needs": needs}


def exclusive_none(new: list[str] | None, old: list[str] | None) -> list[str]:
    """'해당 없음'이랑 다른 버튼 같이 못 고르게. 전부 끄면 '해당 없음'으로 돌아감"""
    new, old = list(new or []), list(old or [])
    added = [x for x in new if x not in old]
    if NONE in added:
        return [NONE]
    if added and NONE in new:
        new = [x for x in new if x != NONE]
    return new or [NONE]


# ── 화면 낱말 ──
# 긴 것부터. '무장애'는 문구 단위로만 바꿈 (장소 이름 '…무장애길'은 그대로)
_SOFT = [
    ("장애인 화장실", "휠체어 화장실"), ("장애인화장실", "휠체어 화장실"),
    ("장애인 전용 주차구역", "전용 주차구역"), ("장애인전용주차구역", "전용 주차구역"), ("장애인 주차구역", "전용 주차구역"),
    ("장애인전용주차", "전용 주차구역"), ("장애인 주차", "전용 주차구역"), ("장애인 전용", "전용"),
    ("무장애 관광지 조사 자료", "관광지 접근성 조사 자료"), ("무장애 관광 인프라", "관광지 접근성"),
    ("무장애 관광지", "다니기 편한 관광지"), ("무장애 조사 자료", "접근성 조사 자료"), ("무장애 조사", "접근성 조사"),
    ("무장애 여행", "편한 여행"), ("근거표", "자료표"),
]
_QUOTE = re.compile(r"(“[^”]*”)")


def soften(text: str | None) -> str:
    """에이전트 문장 → 화면 문장. 원문 인용(“…”)은 그대로"""
    if not text:
        return ""
    out = []
    for part in _QUOTE.split(text):
        if not part.startswith("“"):
            for a, b in _SOFT:
                part = part.replace(a, b)
        out.append(part)
    return "".join(out)


def clean_name(name: str) -> str:
    """지도 검색용 이름 ('(가상)' 뗌)"""
    return re.sub(r"\s*\(가상\)\s*", "", str(name)).strip()


def search_links(name: str) -> dict[str, str]:
    """후기·평점·사진·맛집은 자료에 없어서 지도 검색으로 넘김"""
    q = clean_name(name)
    return {"photos": f"https://map.naver.com/p/search/{quote(q)}",
            "kakao": f"https://map.kakao.com/?q={quote(q)}",
            "food": f"https://map.naver.com/p/search/{quote(q + ' 근처 맛집')}",
            "stay": f"https://map.naver.com/p/search/{quote(q + ' 근처 숙소')}"}
