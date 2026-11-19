"""제출 zip 묶음(scripts/make_submission.py): 넣을 것·뺄 것·검사."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import make_submission as ms  # noqa: E402


def test_selection_keeps_runtime_and_drops_dev_and_private_files():
    files = ["jeju_agent/orchestrator.py", "web/server.py", "prompts/dialog.md", "data/public/places.csv",
             "data/sample/center/weather_daily.csv", "tests/golden/questions.yaml", "run.bat", "README.md",
             "tests/test_course.py", ".github/workflows/ci.yml", "CONTRIBUTING.md", "requirements-dev.txt",
             "data/center/card.csv", "data/raw/x.csv", "runtime/llm_cache.sqlite", "outputs/golden_report.md",
             ".env", "scripts/ask.py", "scripts/make_submission.py", "jeju_agent/__pycache__/x.pyc"]
    got = ms.selected(files)
    assert got == sorted(["jeju_agent/orchestrator.py", "web/server.py", "prompts/dialog.md", "data/public/places.csv",
                          "data/sample/center/weather_daily.csv", "tests/golden/questions.yaml", "run.bat", "README.md"])


def test_eval_env_fills_template_and_blanks_script_only_keys(tmp_path):
    tpl = tmp_path / ".env.example"
    tpl.write_text("# ★ 키\nLLM_API_KEY=\nKAKAO_REST_KEY=\n# 설정\nLLM_MODEL=openai/gpt-5-mini\nVISITJEJU_KEY=\n", encoding="utf-8")
    p = tmp_path / ".env.eval"
    p.write_text("LLM_MODE=api\nLLM_API_KEY=abc\nVISITJEJU_KEY=zzz\nKAKAO_REST_KEY=k\nLLM_MODEL=openai/gpt-4.1-mini\n", encoding="utf-8")
    text, notes = ms.eval_env(p, tpl)
    assert text.startswith("# ★ 키\r\nLLM_API_KEY=abc\r\nKAKAO_REST_KEY=k\r\n")       # 설명과 순서는 예시 파일 그대로
    assert "LLM_MODEL=openai/gpt-4.1-mini" in text and "VISITJEJU_KEY=\r\n" in text and "zzz" not in text
    assert "LLM_MODE=api" in text                                                          # 예시에 없는 값은 끝에 옮김
    assert any("VISITJEJU_KEY" in n for n in notes)


def test_eval_env_keeps_sample_data_mode(tmp_path):
    tpl = tmp_path / ".env.example"
    tpl.write_text("DATA_MODE=sample\n", encoding="utf-8")
    p = tmp_path / ".env.eval"
    p.write_text("DATA_MODE=real\n", encoding="utf-8")
    text, notes = ms.eval_env(p, tpl)
    assert "DATA_MODE=sample" in text and "real" not in text.split("DATA_MODE=")[1][:6] and any("sample" in n for n in notes)


def test_checks_block_center_csv_and_keys(tmp_path):
    (tmp_path / "data" / "x").mkdir(parents=True)
    (tmp_path / "data" / "x" / "center_card.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text('KEY = "sk-' + "a" * 30 + '"\n', encoding="utf-8")
    bad = ms.problems(tmp_path, ["data/x/center_card.csv", "a.py"])
    assert any("센터" in b for b in bad) and any("키" in b for b in bad)


def test_bat_files_get_crlf(tmp_path):
    (tmp_path / "run.bat").write_bytes(b"@echo off\n:run\r\necho hi\n")
    assert ms.payload(tmp_path, "run.bat") == b"@echo off\r\n:run\r\necho hi\r\n"


def test_required_files_exist_in_repo():
    assert not [f for f in ms.INCLUDE_FILES if not (ROOT / f).exists()]
