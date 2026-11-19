"""추출 정확도 평가: 합성 정답과 맞추기, 잘못된 단정 세기, 블라인드 표."""
import pytest

from jeju_agent.data.extraction_eval import label_template, score, truth_labels
from jeju_agent.data.synthetic import SPECS, generate, truth

TRUTH = {sp.name: truth(sp) for sp in SPECS}


@pytest.fixture(scope="module")
def synth():
    return generate()["places"]


def test_synthetic_extraction_matches_truth(synth):
    places = synth
    res = score(places, truth_labels(TRUTH, places))
    assert res["n_labels"] >= 80 and res["wrong_claims"] == 0 and res["overall_accuracy"] == 1.0
    assert {c["item"] for c in res["counts"]} >= {"n_stairs", "max_slope_deg"}


def test_wrong_claim_is_counted(synth):
    places = synth.copy()
    truth = truth_labels(TRUTH, places)
    unknown = truth[(truth["item"] == "wheelchair_rental") & (truth["human_state"] == "정보 없음")]["place_id"].iloc[0]
    places.loc[places["place_id"] == unknown, "wheelchair_rental"] = 1     # 자료에 없는데 '있음'이라고 단정
    res = score(places, truth)
    w = next(x for x in res["facilities"] if x["item"] == "wheelchair_rental")
    assert res["wrong_claims"] == 1 and w["wrong_claims"] == 1 and w["precision_yes"] < 1


def test_blind_template_and_human_round_trip(repo):
    places = repo.places()
    t = label_template(places, n=5)
    assert len(t) == 5 * 8 and (t["auto_state"] == "").all() and (t["evidence"] == "").all()
    shown = label_template(places, n=5, show_auto=True)
    t["human_state"] = shown["auto_state"]       # 사람이 자동 값과 똑같이 표시했다고 치면 정확도 1
    t.loc[0, "human_state"] = "보류"              # 보류는 채점에서 뺀다
    res = score(places, t)
    assert res["n_labels"] == len(t) - 1 and res["overall_accuracy"] == 1.0
