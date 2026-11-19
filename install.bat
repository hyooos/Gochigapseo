@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo  고치갑서 - 무장애 제주 여행 에이전트 설치
echo ==========================================
echo  이 창은 저절로 진행됩니다. 처음에는 3~10분 걸릴 수 있어요.
echo.

echo [1/4] 파이썬 3.12 확인
call :findpy
if defined PY goto :venv

echo   파이썬 3.12가 없어 자동으로 설치합니다(이 사용자 계정에만, 관리자 권한 필요 없음).
where winget >nul 2>&1
if errorlevel 1 goto :pydownload
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpy
if defined PY goto :venv

:pydownload
echo   python.org에서 설치 파일을 받아 설치합니다.
set "PYEXE=%TEMP%\python-3.12.10-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%PYEXE%'"
if exist "%PYEXE%" "%PYEXE%" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Shortcuts=0
call :findpy
if defined PY goto :venv
echo.
echo 파이썬 3.12를 자동으로 설치하지 못했습니다.
echo https://www.python.org/downloads/release/python-31210/ 에서 "Windows installer (64-bit)"를 설치한 뒤
echo 이 파일을 다시 실행하세요.
goto :fail

:venv
echo   사용할 파이썬: %PY%
echo [2/4] 가상환경 만들기 - .venv (이 폴더 안에만 설치, PC의 다른 프로그램에 영향 없음)
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto :fail

echo [3/4] 패키지 설치 - requirements.txt
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail
if /i "%~1"=="dev" ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-dev.txt
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, pandas, matplotlib, openai, yaml" >nul 2>&1
if errorlevel 1 goto :fail

echo [4/4] 설정 파일 준비
if not exist ".env" copy ".env.example" ".env" >nul
if not exist "data\sample\center\weather_daily.csv" ".venv\Scripts\python.exe" scripts\make_synthetic.py
findstr /r /c:"^LLM_API_KEY=..*" ".env" >nul 2>&1
if errorlevel 1 echo   .env 에 LLM 키가 없어 키 없는 모드로 실행됩니다. 키 넣는 법: docs\llm-setup.md

echo.
echo 설치 완료. run.bat 을 더블클릭하세요.
if "%~1"=="" pause
exit /b 0

:fail
echo.
echo 설치 중 오류가 났습니다. 인터넷 연결을 확인하고 다시 실행해 보세요.
echo 그래도 안 되면 이 창을 캡처해 담당자에게 보내 주세요.
if "%~1"=="" pause
exit /b 1

rem ── 파이썬 3.12 찾기: py 런처 → PATH의 python → 사용자 설치 폴더 → 전체 설치 폴더 ──
:findpy
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if defined PY exit /b 0
python --version 2>nul | findstr /b /c:"Python 3.12" >nul && set "PY=python"
if defined PY exit /b 0
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY exit /b 0
if exist "%ProgramFiles%\Python312\python.exe" set PY="%ProgramFiles%\Python312\python.exe"
exit /b 0
