# 개발 환경 설정

## 0. 원칙
- Python은 **3.12**를 씁니다(제출 환경 기준).
- 가상환경은 `.venv`(아래 `install` 스크립트)를 기본으로 하되, conda를 써도 됩니다. 어느 쪽이든 **패키지는 `requirements` 파일로만** 설치합니다.

## 1. 준비
| 도구 | Windows | Mac |
|---|---|---|
| Git | https://git-scm.com/download/win | `xcode-select --install` |
| Python 3.12 | python.org에서 3.12.x 설치 (**"Add python.exe to PATH" 체크**) | `brew install python@3.12` 또는 python.org |

```bash
git --version
py -3.12 --version        # Windows
python3.12 --version      # Mac
```

## 2. 받기
팀장의 GitHub 초대를 **수락**한 뒤 받습니다.
```bash
git clone https://github.com/minyoung-park/BOAZ_Jeju.git
cd BOAZ_Jeju
```

## 3. 설치·실행
**.venv (기본)**
```bash
install.bat dev      # Mac: ./install.sh dev
run.bat              # Mac: ./run.sh
```
(PowerShell이면 `.\install.bat dev`, `.\run.bat`)

**conda를 쓰는 경우**
```bash
conda create -n boaz python=3.12 -y
conda activate boaz
pip install -r requirements-dev.txt
copy .env.example .env        # Mac: cp .env.example .env
python -m web                  # 화면(http://localhost:8501)을 띄우고 브라우저를 연다
```

브라우저에 고치갑서 첫 화면이 열리고, 오른쪽 위 '센터 자료·설정'에 `오프라인(키 없음)`이 보이면 성공입니다.

> 관광지 자료는 `DATA_MODE`와 상관없이 `data/public`(실데이터)을 씁니다. `DATA_MODE=sample`은 센터 CSV가 없을 때 [가상] 센터 표(날씨·버스)로 대신하는 개발용 설정입니다.
> 보고서 PNG의 한글은 저장소의 Pretendard 글꼴(`ui/fonts`)로 그려서 어느 PC에서나 같게 나옵니다.

## 4. 테스트 (PR 올리기 전에)
```bash
python -m pytest
python scripts/check_secrets.py
```
`.venv`를 쓰면 `python` 대신 `.venv\Scripts\python`(Mac: `.venv/bin/python`)으로 실행합니다.
PR을 올리면 GitHub에서도 Windows로 자동 실행됩니다.
