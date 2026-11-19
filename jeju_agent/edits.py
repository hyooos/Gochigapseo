"""수정 대화: 직전 코스를 말로 고치기, 장소 하나 물어보기 (규칙만, LLM 호출 0)

  "둘째 날 빡빡해" → 1곳 줄임 / "2일차 세 곳으로" → 장소 수 / "2일차 다른 곳으로" → 그날만 다시
  "너무 빡빡해" → 전부 여유 / "○○ 빼줘"·"다시 넣어줘" / "엄마도 같이 가" → 인원
  "○○ 화장실 있어?" → 그 장소 시설 상태

이 7가지만 (늘리지 않음). 애매하면 코스 그대로 두고 되물음(kind="ask")
"""
from __future__ import annotations

import re

import pandas as pd

ORDINAL = {"첫째": 1, "첫 날": 1, "첫날": 1, "둘째": 2, "셋째": 3, "넷째": 4, "다섯째": 5, "여섯째": 6, "일곱째": 7, "마지막": -1}
LIGHTER = ("빡빡", "빽빽", "힘들", "무리", "너무 많", "벅차", "줄여", "줄이", "피곤")   # '볼 게 많아'는 아님
ALL_DAYS = ("모든 날", "모든날", "전부", "전체", "매일", "날마다", "다 줄", "너무 빡빡", "일정이 빡빡", "일정 전체")
# 끝맺음까지 봄: '빼줘·빼 주세요·빼고·빼자'(빼다), '다시 넣어'(넣다), '살려 줘'(살려고는 아님)
# '포함·제외'는 부탁하는 끝맺음일 때만 (포함해 줘 O, 포함해서·제외하고 X)
_ASKED = r"\s*(?:해|시켜|하)\s*(?:줘|주|라|$|[.!?~])"
EXCLUDE_RE = re.compile(r"빼\s*(?:줘|주|고|자|면|버려|라)|제외" + _ASKED)
EXCLUDE_WEAK = ("말고", "안 갈래", "안갈래", "가기 싫", "싫어")      # 장소 이름이 같이 맞을 때만 빼기로 봄 ('버스 말고 택시로')
INCLUDE_RE = re.compile(r"(?:다시|도로)\s*넣|넣어\s*(?:줘|주|줄)|빼지\s*마|포함" + _ASKED + r"|살려\s*(?:줘|주)")
JOIN = ("같이 가", "같이가", "함께 가", "함께가", "도 가", "도 갈", "도 와", "도 온", "합류", "같이 여행")
WHO = {"부모님": 2, "엄마": 1, "어머니": 1, "어머님": 1, "아빠": 1, "아버지": 1, "아버님": 1, "할머니": 1, "할아버지": 1,
       "남편": 1, "아내": 1, "와이프": 1, "친구": 1, "동생": 1, "언니": 1, "누나": 1, "형": 1, "오빠": 1, "아들": 1, "딸": 1,
       "아이": 1, "애들": 2, "손주": 1, "장모": 1, "장인": 1, "시어머니": 1, "시아버지": 1}
CONDITION_RE = re.compile(r"버스|대중교통|택시|렌터카|렌트카|자차|\d\s*박|\d\s*일(?!차|째)|하루|이틀|사흘|나흘|\d{1,3}\s*분|\d\s*시간|만\s*원|\d\s*원")
FAC_WORDS = {"acc_toilet": ("화장실",), "acc_parking": ("주차",), "ramp": ("경사로",), "elevator": ("엘리베이터", "승강기"),
             "wheelchair_rental": ("휠체어 대여", "휠체어 빌", "휠체어대여"), "stroller_rental": ("유모차 대여", "유아차 대여", "유모차 빌"),
             "braille": ("점자",), "audio_guide": ("음성 안내", "음성안내", "오디오 가이드")}
# '1일차', '둘째 날'은 일수 아님. 일수는 '2일 코스·1박 2일'처럼 말할 때만
DAY_REF_RE = re.compile(r"\d{1,2}\s*(?:일차|일째|번째\s*날|째\s*날|째날)|(?:첫째|둘째|셋째|넷째|다섯째|첫|마지막)\s*날")
KO_NUM = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4, "다섯": 5}
COUNT_RE = re.compile(r"(\d|한|하나|두|둘|세|셋|네|넷|다섯)\s*(?:곳|군데|개|장소)")
MORE = ("한 곳 더", "한곳 더", "하나 더", "더 넣", "더 가", "늘려", "늘리")
SWAP = ("다른 곳", "다른곳", "다른 데", "다른데로", "딴 곳", "딴 데", "새로운 곳", "새 장소", "통째로", "다른 코스", "다른 장소",
        "다른 일정", "다른 데로")
REDO = ("바꿔", "바꾸", "바꿀", "다시 짜", "다시짜", "이상해", "이상하", "마음에 안", "맘에 안", "별로", "고쳐", "수정")
MERGE = ("합쳐", "합치", "하나로", "한 날로", "몰아")
MAX_PER_DAY = 5
ASKING = ("있어", "있나", "있니", "있을까", "있는지", "있습", "있죠", "돼", "되나", "될까", "되는지", "가능", "어때", "어떄", "?")
_SUFFIX = re.compile(r"\(.*?\)|\s+")
# 장소 이름처럼 끝나는 말 (자료에 없는 장소 물어볼 때 잡으려고). 넓히면 정상 질문을 막으니 조심
PLACE_END = re.compile(r"([가-힣A-Za-z0-9]{2,20}?(?:봉|산|오름|폭포|해변|해수욕장|포구|항|등대|굴|동굴|박물관|미술관|기념관|전시관|"
                       r"공원|정원|수목원|휴양림|숲길|올레길|목장|랜드|파크|섬|바위|습지|곶자왈))"
                       r"(?:에서는|에서|에는|에도|에|엔|은|는|이|가|도|의|까지)?$")


def _norm(s: str) -> str:
    return _SUFFIX.sub("", str(s))


def _lcs(a: str, b: str) -> str:
    """가장 긴 공통 연속 글자"""
    best, end, prev = 0, 0, [0] * (len(b) + 1)
    for i, ca in enumerate(a, 1):
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, end = cur[j], i
        prev = cur
    return a[end - best:end]


def match_places(q: str, places: pd.DataFrame, within: list[str] | None = None) -> list[dict]:
    """질문에 이름 나온 장소 (전체 or 핵심 3글자 이상). 여러 곳 맞으면 같이 돌려줌 → 되묻기"""
    qn = _norm(q)
    pools = [places[places["place_id"].isin(within)], places] if within else [places]
    for pool in pools:
        names = [(pid, name, _norm(name)) for pid, name in zip(pool["place_id"], pool["name"])]
        scored = []
        for pid, name, n in names:
            if not n:
                continue
            m = n if n in qn else _lcs(n, qn)
            k = len(m)
            if k >= 3 and (k == len(n) or k >= max(3, (len(n) + 1) // 2)):
                scored.append((k / len(n), k, pid, name, m))
        if scored:
            _, _, pid, name, m = max(scored, key=lambda s: s[:2])
            same = [(p, nm) for p, nm, n in names if m in n]
            return [{"place_id": p, "name": nm, "full": False} for p, nm in same] if len(same) > 1 and m != _norm(name) \
                else [{"place_id": pid, "name": name, "full": m == _norm(name)}]
    return []


def parse_day(q: str, days: int) -> int | None:
    m = re.search(r"(\d{1,2})\s*(?:일차|일째|번째\s*날|째\s*날|째날)", q)
    if m:
        return int(m.group(1))
    for w, d in ORDINAL.items():
        if w in q and ("날" in q or "일차" in q):
            return days if d == -1 else d
    return None


def ask(message: str, choices: list[str] | None = None, day: int | None = None) -> dict:
    """코스 안 바꾸고 되물음. choices는 화면에서 바로 누르는 답 (그대로 다음 질문이 됨)
    day: 어느 날 묻는 중인지 (다음 말 '세 곳으로'를 'N일차 세 곳으로'로 읽음)
    """
    out = {"kind": "ask", "message": message, "choices": (choices or [])[:8]}
    if day is not None:
        out["day"] = day
    return out


def place_like(q: str) -> tuple[str, str] | None:
    """장소 하나 물어보는 말인데 장소 이름처럼 생긴 말이 있으면 (그 말, 시설). 자료에 있는지는 안 봄
    예: '성산일출봉 화장실 있어?' → ('성산일출봉', 'acc_toilet')
    """
    fac = next((k for k, ws in FAC_WORDS.items() if any(w in q for w in ws)), None)
    if not fac or not any(w in q for w in ASKING) or EXCLUDE_RE.search(q) or INCLUDE_RE.search(q):
        return None
    for tok in re.findall(r"[가-힣A-Za-z0-9]+", q):
        m = PLACE_END.match(tok)
        if m and len(m.group(1)) >= 3:
            return m.group(1), fac
    return None


def _who(q: str) -> str | None:
    """사람 말 바로 뒤에 조사가 붙을 때만 ('제주도 가'의 '도 가'는 사람 아님)"""
    alt = "|".join(sorted(WHO, key=len, reverse=True))
    m = re.search(rf"({alt})(?:님)?\s*(?:도|이랑|랑|하고|까지|와|과)", q)
    return m.group(1) if m else None


def parse_edit(q: str, memory: dict, places: pd.DataFrame) -> dict | None:
    """수정·장소 질문이면 뭘 바꿀지 돌려줌. 아니면 None
    애매하면 kind="ask", 장소 이름이 일부만 맞으면 confirm=True
    """
    last = memory.get("last_request")
    course = memory.get("last_intent") == "course" and last is not None
    it = memory.get("last_itinerary") or []
    it_ids = [p["place_id"] for p in it]
    names = [p["name"] for p in it]
    fac = next((k for k, ws in FAC_WORDS.items() if any(w in q for w in ws)), None)
    excl, incl = bool(EXCLUDE_RE.search(q)), bool(INCLUDE_RE.search(q))

    # 장소 하나 물어보기: 장소 이름 + 시설 + 묻는 말 (코스 없어도 됨)
    if fac and any(w in q for w in ASKING) and not excl and not incl:
        hit = match_places(q, places, it_ids or memory.get("last_place_ids") or None)
        if len(hit) == 1:
            return {"kind": "place_q", "place_id": hit[0]["place_id"], "name": hit[0]["name"], "facility": fac,
                    "confirm": not hit[0]["full"]}
        if len(hit) > 1:
            return ask(f"이름이 비슷한 곳이 여러 곳이에요: {', '.join(h['name'] for h in hit[:4])}. 어느 곳인가요?",
                       [f"{h['name']} {FAC_WORDS[fac][0]} 있어?" for h in hit[:4]])
    if not course:
        return None
    days = last.days or 1
    who = _who(q) if any(w in q for w in JOIN) else None
    hit = match_places(q, places, it_ids) if (excl or incl or any(w in q for w in EXCLUDE_WEAK)) else []
    if has_day_ref(q) and parse_count(q) is not None and not (excl or incl or who):
        return _day_edit(q, days, it)   # '2일차 두 곳으로 줄여줘'는 1곳 줄이기 말고 2곳으로
    acts = []
    if incl:
        acts.append("include")
    if excl or (hit and any(w in q for w in EXCLUDE_WEAK)):
        acts.append("exclude")
    if who:
        acts.append("join")
    if any(w in q for w in LIGHTER) and not who:   # '같이 가는데 … 힘들어'의 '힘들'은 동행인 상태
        acts.append("lighter")
    if not acts:
        return _day_edit(q, days, it)
    if len(acts) > 1:
        return ask("여러 가지를 한 번에 말씀하셔서 코스는 그대로 두었어요. 하나씩 말씀해 주세요.")
    act = acts[0]
    if act == "lighter" and CONDITION_RE.search(q) and parse_day(q, days) is None:
        return None   # '2일로 줄여줘'는 일수 바꾸기 → 이어 묻기로
    if act != "join" and CONDITION_RE.search(q):
        return ask("코스 고치기와 조건 바꾸기(이동 방법·일수 등)를 한 번에 말씀하셔서 코스는 그대로 두었어요. 하나씩 말씀해 주세요.")

    if act == "include":
        gone = list((memory.get("edits") or {}).get("exclude", []))
        gone_names = (memory.get("edits") or {}).get("exclude_names", {})
        if not gone:
            return ask("지금 뺀 장소가 없어요.")
        h = [x for x in match_places(q, places, gone) if x["place_id"] in gone]
        if len(h) == 1:
            return {"kind": "include", "place_id": h[0]["place_id"], "name": h[0]["name"], "confirm": not h[0]["full"]}
        return ask("뺀 장소 중 어느 곳을 다시 넣을까요?", [f"{gone_names.get(g, g)} 다시 넣어줘" for g in gone])
    if act == "exclude":
        if len(hit) == 1:
            return {"kind": "exclude", "place_id": hit[0]["place_id"], "name": hit[0]["name"],
                    "in_course": hit[0]["place_id"] in set(it_ids), "confirm": not hit[0]["full"]}
        if len(hit) > 1:
            return ask(f"이름이 비슷한 곳이 여러 곳이에요: {', '.join(h['name'] for h in hit[:4])}. 어느 곳을 뺄까요?",
                       [f"{h['name']} 빼줘" for h in hit[:4]])
        return ask("어느 장소를 뺄까요? 지금 코스의 장소 이름으로 말씀해 주세요.", [f"{n} 빼줘" for n in names])
    if act == "join":
        k = re.search(r"(\d{1,2})\s*명", q)
        return {"kind": "join", "who": who, "n": int(k.group(1)) if k else WHO[who]}
    day = parse_day(q, days)
    if day is not None and not 1 <= day <= days:
        return ask(f"지금 코스는 {days}일이라 {day}일차가 없어요. 어느 날을 줄일까요?",
                   [f"{d}일차 줄여줘" for d in range(1, days + 1)] + ["모든 날 줄여줘"])
    if day is not None:
        return {"kind": "lighter", "day": day}
    if any(w in q for w in ALL_DAYS) or days == 1:
        return {"kind": "lighter_all"}
    return ask("어느 날을 줄일까요? 모든 날을 여유 있게 할 수도 있어요.",
               [f"{d}일차 줄여줘" for d in range(1, days + 1)] + ["모든 날 줄여줘"])


def has_day_ref(q: str) -> bool:
    return bool(DAY_REF_RE.search(q))


def parse_count(q: str) -> int | None:
    """'세 곳·3곳·두 군데' → 장소 수"""
    m = COUNT_RE.search(q)
    if not m:
        return None
    w = m.group(1)
    return int(w) if w.isdigit() else KO_NUM[w]


def day_choices(day: int, it: list[dict], days: int) -> list[str]:
    """'N일차를 어떻게 바꿀까요?'에 누르는 답. 전부 이 규칙이 읽는 문장이어야 함"""
    names = [p["name"] for p in it if p["day"] == day]
    out = [f"{day}일차 다른 곳으로 바꿔줘"]
    if len(names) > 1:
        out.append(f"{day}일차 빡빡해")
    if len(names) < MAX_PER_DAY:
        out.append(f"{day}일차 {len(names) + 1}곳으로 해줘")
    return out + [f"{n} 빼줘" for n in names]


def _day_edit(q: str, days: int, it: list[dict]) -> dict | None:
    """날 가리키는 수정: 'N일차 M곳으로' · 'N일차 다른 곳으로' · 'N일차 바꿔줘'(되묻기) · 'N일차랑 M일차 합쳐줘'(되묻기)
    날 말 안 했으면 None
    """
    if not has_day_ref(q):
        return None
    if CONDITION_RE.search(q):   # '2일차는 버스로' 같은 조건 바꾸기는 여기서 안 다룸
        return None
    day = parse_day(q, days)
    if day is None:
        return None
    if not 1 <= day <= days:
        return ask(f"지금 코스는 {days}일이라 {day}일차가 없어요. 어느 날을 바꿀까요?",
                   [f"{d}일차 바꿔줘" for d in range(1, days + 1)])
    days_named = sorted({int(x) for x in re.findall(r"(\d{1,2})\s*일차", q)})
    if any(w in q for w in MERGE) or len(days_named) > 1:
        return ask("날을 그대로 합치지는 않았어요. 일수를 줄여 다시 짤까요? 지금 조건(이동·필요한 것)은 그대로 이어받아요.",
                   [f"{days - 1}일 코스로 다시 짜줘"] if days > 1 else [])
    n_now = sum(1 for p in it if p["day"] == day)
    n = parse_count(q)
    if n is None and any(w in q for w in MORE):
        n = n_now + 1
    if n is not None:
        if not 1 <= n <= MAX_PER_DAY:
            return ask(f"하루에는 1~{MAX_PER_DAY}곳까지 넣을 수 있어요. 몇 곳으로 할까요?",
                       [f"{day}일차 {k}곳으로 해줘" for k in range(1, MAX_PER_DAY + 1) if k != n_now])
        if n == n_now:   # 이미 그 수면 다시 안 짬 (장소만 바뀌면 안 됨)
            return ask(f"{day}일차는 지금도 {n}곳이에요. 어떻게 바꿀까요?",
                       [c for c in day_choices(day, it, days) if c != f"{day}일차 {n}곳으로 해줘"], day=day)
        return {"kind": "day_count", "day": day, "n": n, "before": n_now}
    if any(w in q for w in SWAP):
        return {"kind": "day_swap", "day": day, "names": [p["name"] for p in it if p["day"] == day]}
    if any(w in q for w in REDO):
        return ask(f"{day}일차를 어떻게 바꿀까요? 다른 날은 그대로 둘게요.", day_choices(day, it, days), day=day)
    return None


def keep_days(edit: dict, last_itinerary: list[dict], days: int) -> dict[int, list[str]]:
    """한 날만 고치면 나머지 날 장소 그대로 {일차: [place_id]}. 아니면 {} (전부 다시)"""
    k = edit["kind"]
    if k in ("lighter", "day_count", "day_swap"):
        target = edit["day"]
    elif k == "exclude" and edit.get("in_course"):
        target = next((p["day"] for p in last_itinerary if p["place_id"] == edit["place_id"]), None)
    else:
        return {}
    if target is None:
        return {}
    return {d: [p["place_id"] for p in last_itinerary if p["day"] == d] for d in range(1, days + 1) if d != target}


def apply_edit(edit: dict, prev: dict | None, last_itinerary: list[dict]) -> dict:
    """쌓인 수정에 이번 수정 더함. {"exclude", "exclude_names", "day_caps", "pace"}
    하루 장소 수는 직전 코스 그날 수에서 1곳 줄임 (최소 1곳)
    """
    out = {"exclude": [], "exclude_names": {}, "day_caps": {}, "pace": None}
    if prev:
        out = {"exclude": list(prev.get("exclude", [])), "exclude_names": dict(prev.get("exclude_names", {})),
               "day_caps": {int(k): v for k, v in prev.get("day_caps", {}).items()}, "pace": prev.get("pace")}
    if edit["kind"] == "exclude":
        out["exclude"] = list(dict.fromkeys(out["exclude"] + [edit["place_id"]]))
        out["exclude_names"][edit["place_id"]] = edit["name"]
    elif edit["kind"] == "include":
        out["exclude"] = [x for x in out["exclude"] if x != edit["place_id"]]
        out["exclude_names"].pop(edit["place_id"], None)
    elif edit["kind"] == "lighter":
        n = sum(1 for p in last_itinerary if p["day"] == edit["day"])
        out["day_caps"][edit["day"]] = max(1, (n or 1) - 1)
    elif edit["kind"] == "lighter_all":
        out["pace"] = "여유"
    elif edit["kind"] == "day_count":
        out["day_caps"][edit["day"]] = edit["n"]
    elif edit["kind"] == "day_swap":   # 그날 장소는 '뺀 장소'가 됨 ('다시 넣어줘'로 살릴 수 있게)
        for p in last_itinerary:
            if p["day"] == edit["day"]:
                out["exclude"] = list(dict.fromkeys(out["exclude"] + [p["place_id"]]))
                out["exclude_names"][p["place_id"]] = p["name"]
    return out
