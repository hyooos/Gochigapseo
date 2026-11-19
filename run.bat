@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto :run
echo 처음 실행이라 설치부터 합니다(install.bat). 끝나면 저절로 화면이 열립니다.
call install.bat auto
if errorlevel 1 (
  pause
  exit /b 1
)

:run
echo 고치갑서를 띄웁니다. 브라우저가 저절로 열립니다(http://localhost:8501).
echo 끝낼 때는 이 창을 닫으면 됩니다.
".venv\Scripts\python.exe" -m web
if errorlevel 1 pause
