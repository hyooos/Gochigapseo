# 실행 방법

## 화면으로
```
run.bat
```
처음 실행하면 Python 3.12와 필요한 패키지 설치가 함께 진행되고, 끝나면 브라우저에 http://localhost:8501 이 열린다.
끝낼 때는 검은 창을 닫는다. 화면 순서별 자세한 사용법은 사용자 매뉴얼에 있다.

화면은 하나이고 위에서 아래로 쓴다.
- **맨 위**: 큰 글씨 · 고대비 · 색 구별 쉽게, 오른쪽 위 '센터 자료·설정'
- **조건 세 단계**: 누가 가나요? → 어떻게 다닐까요? → 어디로, 언제? → **시작하기**(1·2단계에서 '이대로 바로 시작'도 됨).
  고른 값을 그대로 쓰므로 질문 해석에 LLM을 쓰지 않는다. 분석 중에는 단계별 진행이 화면에 바로 표시된다.
  결과가 나온 뒤에는 '고른 조건' 한 줄과 **조건 바꾸기** 버튼만 남는다
- **결과**: 요약 → 답 → 장소·날짜별 동선(지도) → 맨 아래 '근거와 검증'(분석 과정·쓴 자료·근거표·검증 결과·분석 명세)
- **내려받기**: 보고서(.md) · 가족 공유 일정표 PNG(코스일 때) · 그래프 PNG. 긴 근거표 전체는 보고서 끝 '부록'
- **더 물어보기**: 예시 질문을 누르거나 직접 입력 → 확인 카드에서 **실행**. '위 결과에 이어서 묻기'를 켜면(기본) 위 코스를
  '둘째 날 빡빡해', '○○ 빼줘'처럼 고칠 수 있고, 끄면 새 대화
- **센터 자료·설정**: 센터 CSV(5-01, 5-02, 3-03)를 넣으면 코스에 날씨 위험·버스 정류소 거리가 더해짐
  (sample 모드는 [가상] 센터 자료가 이미 있음). LLM·카카오 연결 상태와 이번 달 LLM 예상 비용, '모두 지우기'도 여기 있다

API 키 설정은 `docs/llm-setup.md`.

## 평가·검사 (프로그램 폴더에서)
```
.venv\Scripts\python scripts\eval_golden.py          # 골든 질문 정답률(개발 55 + 보류 12)·검증 통과 → outputs\golden_report.md (키 없는 모드)
.venv\Scripts\python scripts\eval_golden.py --api    # 같은 평가를 LLM 모드로 (비용 발생)
.venv\Scripts\python scripts\eval_dialog.py          # 이어 묻기·코스 고치기 대화 평가 → outputs\dialog_report.md (--api: LLM 모드)
.venv\Scripts\python scripts\eval_extraction.py synthetic          # [가상] 시설 추출 정확도
.venv\Scripts\python scripts\eval_extraction.py template --n 30    # 실데이터: 사람이 채울 블라인드 표 → outputs\label_template.csv
.venv\Scripts\python scripts\eval_extraction.py score outputs\label_template.csv   # 채점 → outputs\extraction_eval.md
.venv\Scripts\python scripts\check_secrets.py        # 키·개인정보·데이터 샘플 출력 검사
```
단위 테스트(pytest)는 GitHub 저장소에만 있고 제출 zip에는 골든 질문셋(`tests/golden/`)만 들어 있다.

## 데이터 다시 만들기
```
.venv\Scripts\python data\down_script.py                 # 제주데이터허브 무장애 원본 받기 → data\raw (키 필요 없음, data\README.md)
.venv\Scripts\python data\build_processed.py             # 원본 정리 → data\processed
.venv\Scripts\python scripts\convert_barrierfree.py      # data\processed → 앱이 읽는 표 data\public
.venv\Scripts\python scripts\make_synthetic.py           # [가상] 센터 표(data\sample\center) 다시 만들기
.venv\Scripts\python scripts\fetch_kto_barrierfree.py    # 관광공사 무장애 식당 다시 받기 (KTO_KEY 필요)
.venv\Scripts\python scripts\fetch_visitjeju.py          # 비짓제주 사진 목록 다시 받기 (VISITJEJU_KEY 필요)
```
원본(`data\raw`)과 중간 결과(`data\processed`)는 용량 때문에 zip에 없다. 키 발급은 `docs/llm-setup.md`.

## 예시 질문
- 휠체어 타는 어머니와 서귀포에 갈 만한 곳. 장애인 화장실 꼭 있어야 해
- 그중 이 곳들로 2일 코스 짜줘 3월에
- 유모차 끌고 2박3일 코스 짜줘. 버스로 다닐 거야
- 80대 부모님과 하루 일정, 천천히 다니고 싶어
- 제주시 읍면동별 장애인 화장실 현황 (기관)
- 오늘 성산일출봉 지금 붐벼? (실시간은 정직하게 거절하는지 확인)
