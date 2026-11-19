"""골든 질문셋 채점. eval_golden.py랑 tests/test_golden.py가 같이 씀
pytest 없이도 돌게 패키지 안에 둠 (실행 환경엔 개발용 패키지 안 깔아서)
"""
from __future__ import annotations

PROFILE_KEYS = ("mobility", "vision", "hearing", "transport")


def check(resp, exp: dict) -> list[str]:
    """틀린 항목 목록 (비면 정답)"""
    req, bad = resp.plan.request, []
    if req.intent.value != exp["intent"]:
        bad.append(f"intent={req.intent.value}")
    for k in ("region", "days", "month", "origin"):
        if k in exp and getattr(req, k) != exp[k]:
            bad.append(f"{k}={getattr(req, k)}")
    for k in PROFILE_KEYS:
        if k in exp and getattr(req.profile, k) != exp[k]:
            bad.append(f"{k}={getattr(req.profile, k)}")
    if "needs" in exp and not set(exp["needs"]) <= set(req.profile.needs):
        bad.append(f"needs={req.profile.needs}")
    if "interests" in exp and not set(exp["interests"]) <= set(req.profile.interests):
        bad.append(f"interests={req.profile.interests}")
    if "max_ride_min" in exp and req.profile.max_ride_min != exp["max_ride_min"]:
        bad.append(f"max_ride_min={req.profile.max_ride_min}")
    if "verified" in exp and not (resp.verification is not None and resp.verification.passed == exp["verified"]):
        bad.append("verified")
    chosen = next((r.payload.get("selected", []) for r in resp.results if r.agent == "explore"), [])
    if "datasets" in exp and not set(exp["datasets"]) <= set(chosen):
        bad.append(f"datasets={chosen}")
    if "no_datasets" in exp and set(exp["no_datasets"]) & set(chosen):
        bad.append(f"no_datasets={chosen}")
    if "status" in exp and not (resp.results and resp.results[-1].status == exp["status"]):
        bad.append(f"status={resp.results[-1].status if resp.results else None}")
    if "answer_contains" in exp and exp["answer_contains"] not in resp.answer:
        bad.append("answer")
    return bad
