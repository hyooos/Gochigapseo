"""scripts/ask.py 출력 표: 한글이 섞여도 열이 맞는지."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("ask", Path(__file__).resolve().parents[1] / "scripts" / "ask.py")
ask = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ask)  # type: ignore[union-attr]


def test_table_aligns_korean():
    out = ask.table(["장소", "지점 수", "비 온 날 비율"], [["물빛 전시관(가상)", 9, 0.35], ["항구", 10, None]]).splitlines()
    assert len({ask.width(line) for line in out}) == 1          # 모든 줄 폭이 같다 = 열이 맞는다
    assert out[2].startswith("  물빛 전시관(가상)") and out[3].endswith("      ")  # 글자는 왼쪽, 빈 숫자칸은 공백
