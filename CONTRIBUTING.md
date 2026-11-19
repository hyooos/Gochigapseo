# 함께 작업하는 법

## 역할

| 담당 | 고치는 파일 |
|---|---|
| 팀장 | 화면(`app.py`, `ui/`), 오케스트레이터, LLM, 데이터, 보고서, 문서 |
| 팀원 A — 접근성 진단 | `jeju_agent/agents/access.py`, `prompts/agents/access.md`, `config/access_rules.yaml`의 slope·barriers·score |
| 팀원 B — 코스 설계 | `jeju_agent/agents/course.py`, `prompts/agents/course.md`, `config/access_rules.yaml`의 course·weather |
| 팀원 C — 현황 분석 | `jeju_agent/agents/status.py`, `prompts/agents/status.md` |
| (데이터 담당) | `scripts/convert_barrierfree.py` 실행, `config/keywords.yaml` 낱말 보강 |

- 누가 어느 에이전트를 맡을지는 구조 회의에서 정한다 ([docs/transition.md](docs/transition.md) 참고).
- 각 에이전트 파일 맨 위에 **할 일 목록**이 있습니다. 기본 버전은 이미 돌아가니 거기서부터 고도화하면 됩니다.
- 기능을 고치면 테스트(`tests/test_access.py`·`test_course.py`·`test_status.py`)와 골든 질문(`tests/golden/questions.yaml`)도 같이 추가합니다.
- 판정 기준 숫자는 코드가 아니라 `config/access_rules.yaml`에서 바꿉니다.

## 브랜치

- `dev`: 개발 기본 브랜치. 여기서 내 브랜치를 만들고, 여기로 PR합니다.
- `main`: 최종 제출용. 건드리지 않습니다.
- `dev`·`main`에는 직접 push하지 않고 PR로 합칩니다.

## 작업 흐름

**1. 최신 dev에서 내 브랜치 만들기**

```bat
git checkout dev
git pull
git checkout -b 이름/작업내용
```

예: `yunseo/access-keywords`

**2. 수정하고 확인하기** — 실행·테스트 방법은 [docs/how-to-run.md](docs/how-to-run.md)

**3. 올리기**

```bat
git add -A
git commit -m "무엇을 바꿨는지 한 줄"
git push -u origin 이름/작업내용
```

**4. PR 만들기** — GitHub의 **Compare & pull request** → 위쪽이 `base: dev`인지 확인 → **Create pull request**

**5. 합치기** — 자동 테스트가 ✅이면 팀장 또는 다른 팀원이 확인하고 **Merge**. ❌이면 고쳐서 같은 브랜치에 다시 push합니다.

**6. 다음 작업** — 다시 1번부터 (`git checkout dev` → `git pull`)

## 지킬 것

- `requirements.txt`에 없는 패키지를 설치할 때는 먼저 말하기 (센터 PC에는 기획서에 적은 것만 설치됨).
- `.env`(API 키)와 `df.head()` 같은 데이터 출력은 커밋하지 않기.
- **이용자 조건(장애·건강 정보)은 실행 기록(trace)·로그·파일에 남기지 않기** (`tests/test_privacy.py`가 검사).
- '정보 없음'을 '없음'으로 말하지 않기. '적합'도 '방문 전 확인 필요'를 함께 쓰기.
- 다른 사람 담당 파일을 고칠 때는 말하기.
- 센터 실행 환경(운영진 답변)은 [AGENTS.md](AGENTS.md)에 있음.
