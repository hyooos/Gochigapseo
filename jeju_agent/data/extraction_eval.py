"""시설 추출 정확도: 자동 값 vs 사람이 표시한 정답
label_template() → 사람이 human_state 채움 → score()
잘못된 단정(있음 ↔ 없음)은 따로 셈, 목표 0건
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from .barrierfree import FACILITIES

STATE = {1: "있음", 0: "없음", None: "정보 없음"}
STATE_IN = {"있음": 1, "없음": 0, "정보 없음": None, "정보없음": None, "언급 없음": None, "": None}
COUNTS = ("n_stairs", "n_inaccessible", "n_threshold")
LABEL_COLUMNS = ["place_id", "name", "source_file", "item", "item_ko", "auto_state", "evidence", "human_state", "note"]
FAC_LABEL = {"acc_toilet": "장애인 화장실", "acc_parking": "장애인 주차", "wheelchair_rental": "휠체어 대여",
             "stroller_rental": "유아차 대여", "ramp": "경사로", "elevator": "엘리베이터", "braille": "점자 안내",
             "audio_guide": "음성 안내"}


def _state(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return STATE[None]
    return STATE[int(v)]


def label_template(places: pd.DataFrame, n: int = 30, seed: int = 7, show_auto: bool = False,
                   items: tuple[str, ...] = FACILITIES) -> pd.DataFrame:
    """표본 n곳 × 시설 → 사람이 채울 표. show_auto=False면 자동 값 비워서 블라인드로"""
    pick = places.sample(min(n, len(places)), random_state=seed) if len(places) > n else places
    rows = []
    for _, r in pick.iterrows():
        for f in items:
            ev = r.get(f"ev_{f}")
            rows.append({"place_id": r["place_id"], "name": r["name"], "source_file": r.get("source_file"),
                         "item": f, "item_ko": FAC_LABEL.get(f, f), "auto_state": _state(r.get(f)) if show_auto else "",
                         "evidence": (ev if isinstance(ev, str) else "") if show_auto else "",
                         "human_state": "", "note": ""})
    return pd.DataFrame(rows, columns=LABEL_COLUMNS)


def truth_labels(truth_path: str | Path | dict, places: pd.DataFrame) -> pd.DataFrame:
    """합성 데이터 정답 → label_template이랑 같은 모양의 정답 표"""
    truth = truth_path if isinstance(truth_path, dict) else json.loads(Path(truth_path).read_text(encoding="utf-8"))
    ids = dict(zip(places["name"], places["place_id"], strict=False))
    rows = []
    for name, t in truth.items():
        if name not in ids:
            continue
        for k, v in t.items():
            if k in FACILITIES:
                rows.append({"place_id": ids[name], "name": name, "item": k, "human_state": _state(v)})
            elif k in COUNTS or k == "max_slope_deg":
                rows.append({"place_id": ids[name], "name": name, "item": k, "human_state": "" if v is None else str(v)})
    return pd.DataFrame(rows)


def score(places: pd.DataFrame, labels: pd.DataFrame) -> dict:
    """정답 표(human_state 채운 행)랑 지금 places 값 맞춰 봄"""
    lab = labels.copy()
    lab["human_state"] = lab["human_state"].fillna("").astype(str).str.strip()
    lab = lab[lab["human_state"] != "보류"]
    p = places.set_index("place_id")
    fac_rows, cnt_rows = [], []
    for _, r in lab.iterrows():
        if r["place_id"] not in p.index:
            continue
        row = p.loc[r["place_id"]]
        if r["item"] in FACILITIES:
            if r["human_state"] not in STATE_IN:
                continue
            fac_rows.append({"item": r["item"], "auto": _state(row.get(r["item"])),
                             "human": STATE[STATE_IN[r["human_state"]]], "name": r.get("name", "")})
        elif r["item"] in COUNTS or r["item"] == "max_slope_deg":
            if r["human_state"] == "":
                continue
            a = row.get(r["item"])
            a = None if a is None or (isinstance(a, float) and math.isnan(a)) else float(a)
            cnt_rows.append({"item": r["item"], "auto": a, "human": float(r["human_state"])})
    fac = pd.DataFrame(fac_rows, columns=["item", "auto", "human", "name"])
    per = []
    for item, g in fac.groupby("item"):
        auto_yes, true_yes = g["auto"] == "있음", g["human"] == "있음"
        wrong = ((g["auto"] == "있음") & (g["human"] != "있음")) | ((g["auto"] == "없음") & (g["human"] == "있음"))
        per.append({"item": item, "label": FAC_LABEL.get(item, item), "n": len(g),
                    "accuracy": round(float((g["auto"] == g["human"]).mean()), 3),
                    "precision_yes": round(float((auto_yes & true_yes).sum() / auto_yes.sum()), 3) if auto_yes.any() else None,
                    "recall_yes": round(float((auto_yes & true_yes).sum() / true_yes.sum()), 3) if true_yes.any() else None,
                    "missed": int(((g["auto"] == "정보 없음") & (g["human"] != "정보 없음")).sum()),
                    "wrong_claims": int(wrong.sum()),
                    "wrong_examples": g[wrong]["name"].head(3).tolist()})
    cnt = pd.DataFrame(cnt_rows, columns=["item", "auto", "human"])
    counts = []
    for item, g in cnt.groupby("item"):
        tol = 0.5 if item == "max_slope_deg" else 0
        ok = [(a is not None and abs(a - h) <= tol) or (a is None and h == 0) for a, h in zip(g["auto"], g["human"], strict=False)]
        counts.append({"item": item, "n": len(g), "match": round(sum(ok) / len(ok), 3)})
    total = int(fac["auto"].eq(fac["human"]).sum()) if len(fac) else 0
    return {"n_labels": len(fac), "overall_accuracy": round(total / len(fac), 3) if len(fac) else None,
            "wrong_claims": int(sum(x["wrong_claims"] for x in per)), "facilities": per, "counts": counts}


def report_md(res: dict, title: str) -> str:
    lines = [f"# 시설·장애물 추출 정확도 — {title}", "",
             f"- 시설 판정 {res['n_labels']}건, 전체 정확도 {res['overall_accuracy']}",
             f"- 잘못된 단정(이용자를 잘못 안내할 수 있는 오류): {res['wrong_claims']}건", "",
             "| 시설 | 건수 | 정확도 | '있음' 정밀도 | '있음' 재현율 | 놓침(자동 정보 없음) | 잘못된 단정 |", "|---|---|---|---|---|---|---|"]
    for x in res["facilities"]:
        lines.append(f"| {x['label']} | {x['n']} | {x['accuracy']} | {x['precision_yes'] if x['precision_yes'] is not None else '-'} | "
                     f"{x['recall_yes'] if x['recall_yes'] is not None else '-'} | {x['missed']} | {x['wrong_claims']} |")
    if res["counts"]:
        lines += ["", "| 장애물·경사 | 건수 | 일치율 |", "|---|---|---|"]
        lines += [f"| {x['item']} | {x['n']} | {x['match']} |" for x in res["counts"]]
    lines += ["", "정확도 = 자동 값(있음·없음·정보 없음)이 정답과 같은 비율. '있음' 정밀도 = 자동이 있음이라고 한 것 중 정답도 있음인 비율.",
              "놓침 = 원문에 언급이 있는데 자동이 '정보 없음'으로 둔 수(→ config/keywords.yaml 낱말 보강 대상)."]
    return "\n".join(lines) + "\n"
