import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_secrets", Path(__file__).parents[1] / "scripts" / "check_secrets.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)


def hits(line: str) -> set[str]:
    return {name for name, pat in cs.PATTERNS.items() if pat.search(line)}


def test_detects_keys_and_samples():
    # 가짜 키는 문자열을 이어 붙여 만든다(이 파일 자체가 검사에 걸리지 않도록)
    assert "OpenAI/OpenRouter 키" in hits("key = '" + "sk-" + "or-" + "a" * 32 + "'")
    assert "Google API 키" in hits("AIza" + "B" * 35)
    assert "데이터 샘플 출력" in hits("df.head()")
    assert "데이터 샘플 출력" in hits("print(df." + "sample(5))")


def test_allows_normal_code():
    assert not hits("worst = tab.sort_values('x').head(10)")
    assert not hits("LLM_API_KEY=")


def test_repository_is_clean():
    assert cs.main() == 0


def test_tracked_files_keep_spaces_and_korean_names():
    """git ls-files 결과를 공백으로 자르거나 따옴표(\\353…)로 받으면 윈도우에서 경로가 깨진다(11/14 실패 재발 방지)."""
    files = cs.tracked_files()
    assert files and not any('"' in str(p) or "\\353" in str(p) for p in files)
