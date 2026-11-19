"""python -m web → 서버(127.0.0.1:8501) 띄우고 브라우저 엶. 끝낼 땐 창 닫으면 됨"""
from __future__ import annotations

import os
import threading
import time
import urllib.request
import webbrowser

import uvicorn

HOST, PORT = "127.0.0.1", int(os.environ.get("WEB_PORT", "8501"))
URL = f"http://localhost:{PORT}/"   # 카카오맵 JS 키에 등록한 도메인(http://localhost:8501)이랑 같아야 함


def _open_when_ready() -> None:
    for _ in range(120):   # 처음 실행은 자료 읽느라 몇 초 걸림
        try:
            urllib.request.urlopen(URL, timeout=1)
            webbrowser.open(URL)
            return
        except OSError:
            time.sleep(0.5)


if __name__ == "__main__":
    print(f"무장애 제주 여행 화면: {URL}  (끝낼 때는 이 창을 닫으세요)")
    if os.environ.get("WEB_NO_BROWSER") != "1":
        threading.Thread(target=_open_when_ready, daemon=True).start()
    from web import server
    server.configure()   # .env 읽어서 설정·오케스트레이터 만듦
    uvicorn.run(server.app, host=HOST, port=PORT, log_level="warning")
