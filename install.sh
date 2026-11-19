#!/usr/bin/env bash
# Mac/Linux 개발용 설치 스크립트 (센터 PC는 install.bat 사용)
set -euo pipefail
cd "$(dirname "$0")"
PY=""
for c in python3.12 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)'; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "Python 3.12 가 없습니다. docs/dev-setup.md 를 보세요."; exit 1; fi
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt
if [ "${1:-}" = "dev" ]; then .venv/bin/python -m pip install -r requirements-dev.txt; fi
[ -f .env ] || cp .env.example .env
[ -f data/sample/center/weather_daily.csv ] || .venv/bin/python scripts/make_synthetic.py
echo "설치 완료. ./run.sh 로 실행하세요."
