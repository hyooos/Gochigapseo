"""커밋·제출 전 검사: API 키, 개인정보 패턴, 데이터 샘플 출력(head() 등) 찾음
python scripts/check_secrets.py (문제 있으면 종료코드 1). CI에서도 매번 돎
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "runtime", "outputs", "data", ".pytest_cache"}
TEXT_EXT = {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".bat", ".sh", ".json", ".ipynb", ".example", ".mdc", ".cfg", ".ini"}

PATTERNS = {
    "OpenAI/OpenRouter 키": re.compile(r"sk-(?:or-|proj-)?[A-Za-z0-9_\-]{20,}"),
    "Anthropic 키": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "Google API 키": re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    "키 직접 대입": re.compile(r"(?i)(api_key|serviceKey|secret)\s*=\s*['\"][A-Za-z0-9%+/=_\-]{16,}['\"]"),
    "주민등록번호": re.compile(r"\b\d{6}-[1-4]\d{6}\b"),
    "휴대전화번호": re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"),
    # 표를 그대로 찍는 줄 (df.head(), print(df.sample(5)), display(df))
    "데이터 샘플 출력": re.compile(r"^\s*\w+\.(head|tail|sample)\(\s*\d*\s*\)\s*$|print\(\s*\w+\.(head|tail|sample)\(|display\(\s*\w+\s*\)"),
}
# 검사 스크립트 자신은 패턴 예시가 있어서 일부 규칙만
SELF = {Path(__file__).resolve()}


def tracked_files() -> list[Path]:
    try:
        # -z: 파일 이름 공백·한글 그대로 받음
        out = subprocess.run(["git", "-c", "core.quotepath=off", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                             check=True).stdout.decode("utf-8")
        files = [ROOT / f for f in out.split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        # git 저장소 아닐 때(zip으로 받은 경우): .env는 원래 있어야 하니까 뺌
        files = [p for p in ROOT.rglob("*") if p.is_file() and p.name != ".env"]
    return [p for p in files if not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)
            and (p.suffix in TEXT_EXT or p.name.startswith(".env"))]


def main() -> int:
    problems = []
    for p in tracked_files():
        if p.name == ".env":
            problems.append((p, 0, ".env 파일이 저장소에 있음"))
            continue
        if p.resolve() in SELF:
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines, 1):
            for name, pat in PATTERNS.items():
                if pat.search(line):
                    problems.append((p, i, name))
    for p, i, name in problems:
        print(f"[!] {p.relative_to(ROOT)}:{i}  {name}")
    print(f"검사 완료: 문제 {len(problems)}건")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
