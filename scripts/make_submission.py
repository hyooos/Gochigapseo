"""제출용 zip 만들기 (받는 쪽은 run.bat만 누르면 됨)

    python scripts/make_submission.py [--docs 폴더] [--no-env]

.env.eval의 평가용 키를 넣어 .env를 만들고, 필요한 파일만 담음 (센터 실데이터·키·샘플 출력 검사)
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_secrets import PATTERNS, TEXT_EXT  # noqa: E402

TOP = "gochigapseo"   # zip 풀면 생기는 폴더 이름 (한글이면 윈도우 압축 풀기에서 깨짐)
INCLUDE_DIRS = ("jeju_agent/", "web/", "prompts/", "config/", "ui/", "data/public/", "data/visitjeju/", "data/sample/",
                "data/geo/", "tests/golden/", "docs/images/")
INCLUDE_FILES = {
    "START_HERE.txt", "README.md", "install.bat", "run.bat", "requirements.txt", ".env.example",
    "docs/architecture.md", "docs/how-to-run.md", "docs/llm-setup.md",
    # 데이터 처리 코드. 원본은 용량·이용조건 때문에 안 넣고 받는 법만 data/README.md에
    "data/README.md", "data/dataset_dates.csv", "data/build_processed.py", "data/down_script.py", "data/hwp5md.py",
    "data/master_fixes.csv", "data/requirements.txt",
    "scripts/check_secrets.py", "scripts/convert_barrierfree.py", "scripts/fetch_kto_barrierfree.py",
    "scripts/fetch_visitjeju.py", "scripts/make_synthetic.py",
    # 검증·재현: 골든 정답률, 대화 평가, 추출 정확도, 모델 답 기록·재생
    "scripts/eval_golden.py", "scripts/eval_dialog.py", "scripts/eval_extraction.py", "scripts/llm_record.py",
}
NEVER = ("data/center/", "data/raw/", "data/processed/", "data/extracted/", "runtime/", "outputs/", ".venv/", ".git/")
# 실행 중엔 안 쓰는 키 (자료 다시 받는 스크립트용). 평가용 .env에선 비움
SCRIPT_ONLY_KEYS = ("VISITJEJU_KEY", "KTO_KEY", "JEJU_DATAHUB_KEY")


def tracked() -> list[str]:
    out = subprocess.run(["git", "-c", "core.quotepath=off", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                         check=True).stdout.decode("utf-8")
    return [f for f in out.split("\0") if f]


def selected(files: list[str]) -> list[str]:
    keep = []
    for f in files:
        if f.startswith(NEVER) or "__pycache__/" in f or f.endswith(".pyc") or Path(f).name == ".env":
            continue
        if f in INCLUDE_FILES or f.startswith(INCLUDE_DIRS):
            keep.append(f)
    return sorted(keep)


def eval_env(path: Path, template: Path | None = None) -> tuple[str, list[str]]:
    """평가용 .env: .env.example에 path 값을 채움. 스크립트 전용 키는 비움
    그래서 심사 PC .env는 '키 3개 맨 위, 나머지는 설명이랑 같이' 모양이 됨
    """
    template = template or ROOT / ".env.example"
    vals: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    notes, lines, used = [], [], set()
    for line in template.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k == "DATA_MODE" and vals.get(k, "sample") != "sample":
                # zip엔 센터 실데이터가 없어서 real이면 날씨·버스가 꺼짐 → sample로
                notes.append(f"DATA_MODE={vals[k]} → sample 로 바꿨습니다(zip에 센터 실데이터가 없어 [가상] 센터 표로 실행)")
                used.add(k)
            elif k in vals:
                v = "" if k in SCRIPT_ONLY_KEYS else vals[k]
                if k in SCRIPT_ONLY_KEYS and vals[k]:
                    notes.append(f"{k}: 실행에는 안 쓰는 키라 비웠습니다")
                line = f"{k}={v}"
                used.add(k)
        lines.append(line)
    extra = [k for k in vals if k not in used and k not in SCRIPT_ONLY_KEYS]
    if extra:
        lines += ["", "# 기타(팀장 PC 설정에서 옮김)"] + [f"{k}={vals[k]}" for k in extra]
    env = {k: (vals.get(k, "") if k in used or k in extra else "") for k in used | set(extra)}
    if vals.get("LLM_MODE", "api").strip() != "api" or not vals.get("LLM_API_KEY", "").strip():
        notes.append("⚠️ LLM_MODE=api 와 LLM_API_KEY 가 없어 심사 PC에서는 키 없는 모드로 돕니다")
    if not env.get("KAKAO_REST_KEY", "").strip():
        notes.append("KAKAO_REST_KEY 가 없어 실제 도로 이동 시간·식당 좌표 실시간 확인은 꺼집니다")
    return "\r\n".join(lines) + "\r\n", notes


def problems(root: Path, files: list[str]) -> list[str]:
    bad = []
    for f in files:
        p = root / f
        if f.endswith(".csv") and "center" in f and not f.startswith("data/sample/"):
            bad.append(f"{f}: 센터 자료로 보이는 CSV — 센터 실데이터는 넣지 않습니다")
        if p.suffix in TEXT_EXT or p.name.startswith(".env"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            if f == "scripts/check_secrets.py":   # 검사 규칙 자기 자신 (패턴 예시가 들어 있음)
                continue
            for i, line in enumerate(text.splitlines(), 1):
                bad += [f"{f}:{i}: {name}" for name, rx in PATTERNS.items() if rx.search(line)]
    return bad


def payload(root: Path, f: str) -> bytes:
    data = (root / f).read_bytes()
    if f.endswith(".bat"):   # cmd는 LF면 레이블·goto가 깨질 수 있음
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(ROOT / ".env.eval"), help="평가용 .env (기본 .env.eval)")
    ap.add_argument("--no-env", action="store_true", help="키 없이 만든다")
    ap.add_argument("--docs", help="제출 문서 폴더 — zip 안 docs/submission/ 에 넣는다")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args(argv)

    files = selected(tracked())
    missing = [f for f in INCLUDE_FILES if f not in files]
    if missing:
        print("저장소에 없는 필수 파일:", ", ".join(sorted(missing)))
        return 1
    bad = problems(ROOT, files)
    if bad:
        print("넣으면 안 되는 내용이 있습니다. 고친 뒤 다시 실행하세요:")
        print("\n".join("  " + b for b in bad))
        return 1

    notes: list[str] = []
    env_text = None
    if not a.no_env:
        env_path = Path(a.env)
        if not env_path.exists():
            print(f"평가용 설정 파일이 없습니다: {env_path}\n"
                  " .env 를 복사해 .env.eval 로 만들고, 키를 평가용으로 새로 만든 키(사용 한도 설정)로 바꾼 뒤 다시 실행하세요.\n"
                  " 키 없이 만들려면 --no-env")
            return 1
        env_text, notes = eval_env(env_path)

    docs: list[Path] = []
    if a.docs:
        d = Path(a.docs)
        docs = sorted(p for p in d.rglob("*") if p.is_file())
        odd = [p.name for p in docs if not p.name.isascii()]
        if odd:
            notes.append("한글 파일 이름은 일부 압축 프로그램에서 깨질 수 있어요(영문 이름 권장): " + ", ".join(odd[:5]))

    out = Path(a.out)
    out.mkdir(exist_ok=True)
    zpath = out / f"gochigapseo_submission_{dt.datetime.now():%Y%m%d_%H%M}.zip"
    size = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            data = payload(ROOT, f)
            size += len(data)
            z.writestr(f"{TOP}/{f}", data)
        if env_text is not None:
            z.writestr(f"{TOP}/.env", env_text.encode("utf-8"))
        for p in docs:
            z.write(p, f"{TOP}/docs/submission/{p.relative_to(Path(a.docs)).as_posix()}")
    print(f"만들었습니다: {zpath}  ({len(files) + len(docs) + (env_text is not None)}개 파일, "
          f"풀면 약 {size / 1e6:.0f}MB, zip {zpath.stat().st_size / 1e6:.0f}MB)")
    print("평가용 .env: " + ("넣음" if env_text is not None else "넣지 않음(키 없는 모드)"))
    for n in notes:
        print(" - " + n)
    print("다른 PC에서 확인: zip 풀기 → gochigapseo\\run.bat 더블클릭 (처음 한 번은 설치가 함께 진행됩니다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
