"""평가용: 실제 LLM 답을 남기고(--record) 그걸로 키 없이 다시 돌림(--replay)
남기는 건 프롬프트 첫 줄·평가 질문·모델 답뿐 (키·개인정보 없음)
"""
from __future__ import annotations

import json
from pathlib import Path


def _key(messages: list[dict]) -> tuple[str, str]:
    system = str(messages[0].get("content", "")) if messages else ""
    user = str(messages[-1].get("content", "")) if messages else ""
    return system.strip().splitlines()[0][:80] if system.strip() else "", user


def attach(llm, path: Path) -> None:
    """실제 호출 답을 path에 한 줄씩 남김 (캐시 답은 안 남으니 캐시 끄고 쓸 것)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    real_text, real_tool = llm._openai_call, llm._openai_tool_request

    def text(messages, model):
        out = real_text(messages, model)
        head, user = _key(messages)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "text", "prompt": head, "user": user, "reply": out[0]}, ensure_ascii=False) + "\n")
        return out

    def tool(messages, tools):
        out = real_tool(messages, tools)
        head, user = _key(messages)
        msg = out[0]
        reply = msg.model_dump() if hasattr(msg, "model_dump") else str(msg)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "tool", "prompt": head, "user": user, "reply": reply}, ensure_ascii=False, default=str) + "\n")
        return out

    llm._openai_call, llm._openai_tool_request = text, tool


class Replay:
    """LLMClient(transport=Replay(path))로 씀. 같은 입력이 여러 번이면 남긴 순서대로"""

    def __init__(self, path: Path):
        self.answers: dict[tuple[str, str], list[str]] = {}
        self.missing = 0
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("kind") == "text":
                    self.answers.setdefault((r["prompt"], r["user"]), []).append(r["reply"])

    def __call__(self, messages, model):
        got = self.answers.get(_key(messages))
        if not got:
            self.missing += 1
            raise LookupError("재생할 답 없음")
        return (got.pop(0) if len(got) > 1 else got[0]), 0, 0
