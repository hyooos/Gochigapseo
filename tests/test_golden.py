"""골든 질문셋 회귀 테스트 (오프라인 모드 기준). 실패한 문항은 규칙 또는 기대값을 다시 검토한다."""
from pathlib import Path

import pytest
import yaml

from jeju_agent.evaluation import check

CASES = yaml.safe_load((Path(__file__).parent / "golden" / "questions.yaml").read_text(encoding="utf-8"))


@pytest.mark.golden
@pytest.mark.parametrize("case", CASES, ids=[c["q"][:20] for c in CASES])
def test_golden(orch, case):
    assert check(orch.ask(case["q"]), case["expect"]) == []


KEYS = {"intent", "region", "days", "month", "mobility", "vision", "hearing", "transport", "needs", "status", "answer_contains",
        "origin", "max_ride_min", "interests", "verified", "datasets", "no_datasets"}


def test_holdout_is_well_formed_and_separate():
    """보류 세트는 정답 여부를 pytest에서 강제하지 않는다(맞추려고 규칙을 고치면 보류의 뜻이 없어짐). 형식과 분리만 검사."""
    hold = yaml.safe_load((Path(__file__).parent / "golden" / "holdout.yaml").read_text(encoding="utf-8"))
    assert len(hold) >= 10 and len(CASES) >= 50
    assert not {c["q"] for c in hold} & {c["q"] for c in CASES}
    for c in hold:
        assert set(c["expect"]) <= KEYS and "intent" in c["expect"], c["q"]
