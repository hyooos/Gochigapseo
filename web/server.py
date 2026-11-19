"""웹 서버 (FastAPI, 127.0.0.1에서만). run.bat이 띄우고 브라우저 엶
이용자 조건은 메모리(세션)에만 두고 파일·로그엔 안 남김. '모두 지우기'로 바로 지움
"""
from __future__ import annotations

import asyncio
import base64
from functools import lru_cache
import json
import queue
import secrets
import shutil
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jeju_agent import progress
from jeju_agent.config import DATA_DIR, OUTPUT_DIR, Settings, get_settings
from jeju_agent.contracts import RIDE_OPTIONS, TRANSPORT_KO, Intent, OrchestratorResponse, Plan, TravelerProfile
from jeju_agent.data import CENTER_INFO, CENTER_TABLES, DataNotReady, read_csv_auto
from jeju_agent.data.regions import EMD
from jeju_agent.data.visitjeju import PHOTO_HOST as VJ_PHOTO_HOST
from jeju_agent.friendly import (COGNITION_UI, HEARING_UI, INTEREST_UI, MOBILITY_UI, NEED_UI, TRANSPORT_UI, VISION_UI,
                                 merge_conditions, search_links, soften)
from jeju_agent.agents.base import uses
from jeju_agent.compare import changes
from jeju_agent.orchestrator import Orchestrator
from jeju_agent.provenance import footer, tag
from jeju_agent.report.builder import build_markdown
from jeju_agent.report.charts import DAY_COLORS, VERDICT_COLORS, VERDICT_COLORS_CVD, report_charts, save_png, setup_korean_font
from jeju_agent.report.share import itinerary_png
from jeju_agent.report.spec import analysis_spec
from jeju_agent.data.nearby_online import place_info
from jeju_agent.ui_helpers import icon_svg, kakao_map_html, photo_data_uri

from . import views

WEB = Path(__file__).parent
KST = timezone(timedelta(hours=9))   # zoneinfo는 Windows에서 tzdata 따로 필요해서 고정 오프셋 씀
EXAMPLES = ["휠체어 타는 어머니와 서귀포에 갈 만한 곳 추천해줘. 장애인 화장실 꼭 있어야 해",
            "유모차 끌고 2박3일 코스 짜줘. 버스로 다닐 거야",
            "그중 장애인 화장실 있는 곳만 보여줘",
            "제주시 읍면동별 무장애 관광지 현황 보여줘",
            "보고서로 정리해줘"]

TAGLINE = "몸이 불편해도 제주도 여행은 편하게!"
APP_NAME = "고치갑서"
setup_korean_font()
# 설정·오케스트레이터는 import 때 말고 서버 시작할 때(configure) 만듦
# import 때 만들면 .env 키가 캐시에 먼저 박혀서 테스트가 실제 LLM을 부름
s: Settings = None  # type: ignore[assignment]
orch: Orchestrator = None  # type: ignore[assignment]


def configure(settings: Settings | None = None, orchestrator: Orchestrator | None = None) -> None:
    """설정이랑 오케스트레이터 정함. 서버 시작 때 한 번, 테스트는 오프라인 설정으로 직접 부름"""
    global s, orch, APP_NAME
    s = settings or get_settings()
    orch = orchestrator or Orchestrator(s)
    APP_NAME = "고치갑서" if s.app_name in ("", "BOAZ Jeju Agent") else s.app_name   # 예전 .env 가칭이면 서비스 이름으로
    app.title = APP_NAME
    SESSIONS.clear()
    JOBS.clear()
    _CHART_CACHE.clear()
    nearby_info.cache_clear()


@asynccontextmanager
async def lifespan(_app):
    if orch is None:   # 테스트가 미리 configure 했으면 그대로
        configure()
    yield


app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def ready(request: Request, call_next):
    """lifespan 안 거치고 요청 와도(TestClient 등) 설정을 그때 읽어서 뜨게 함
    import 때는 안 읽음 (pytest 수집 중에 .env가 굳어버림)
    """
    if orch is None:
        configure()
    return await call_next(request)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
# css·js 고치면 주소가 바뀌게 (브라우저가 예전 app.js를 캐시해서 버튼이 안 먹는 것 방지)
STATIC_VER = str(max(int(p.stat().st_mtime) for p in (WEB / "static").rglob("*.*") if p.suffix in (".css", ".js")))
tpl = Jinja2Templates(directory=WEB / "templates")
tpl.env.globals["icon"] = icon_svg   # 식당·숙소 흰 그림


# ── 세션·작업 ──
@dataclass
class Session:
    memory: dict = field(default_factory=dict)        # 맞춤 여행 + (이어 묻기 켠) 대화가 같이 쓰는 기억
    solo: dict = field(default_factory=dict)          # 이어 묻기 끈 대화만의 기억
    link: bool = True
    trip: OrchestratorResponse | None = None
    versions: list[dict] = field(default_factory=list)  # 큰 결과 기록 [{resp, label, at}]. 마지막이 지금 결과, /new에서 지움
    history: list[OrchestratorResponse] = field(default_factory=list)
    moved: set[int] = field(default_factory=set)      # 위 큰 결과로 옮긴 대화 답(id)
    pending: Plan | None = None
    form: dict = field(default_factory=dict)          # 마지막 입력값 (다시 고칠 때 채워 둠)
    cache: dict = field(default_factory=dict)         # (응답 id, 종류) → 만든 파일 바이트

    def mem(self) -> dict:
        return self.memory if self.link else self.solo

    def keep(self, resp: OrchestratorResponse, label: str) -> None:
        """새 큰 결과를 기록 끝에 더하고 지금 결과로"""
        self.versions.append({"resp": resp, "label": label, "at": datetime.now(KST).strftime("%H:%M")})
        self.trip = resp


@dataclass
class Job:
    q: queue.Queue = field(default_factory=queue.Queue)
    error: str | None = None


SESSIONS: dict[str, Session] = {}
JOBS: dict[str, Job] = {}


def sess(request: Request) -> Session:
    sid = request.cookies.get("sid") or ""
    return SESSIONS.setdefault(sid, Session()) if sid else Session()


def with_sid(request: Request, resp: Response) -> Response:
    if not request.cookies.get("sid"):
        sid = secrets.token_urlsafe(16)
        SESSIONS[sid] = Session()
        resp.set_cookie("sid", sid, httponly=True, samesite="strict")
    return resp


def start(plan: Plan, memory: dict, done) -> str:
    """분석은 다른 스레드에서, 단계 알림은 큐로. 끝나면 done(resp)로 세션에 넣음"""
    job_id = secrets.token_urlsafe(8)
    job = JOBS[job_id] = Job()

    def work() -> None:
        try:
            with progress.listen(lambda step: job.q.put({"kind": step.kind, "text": soften(step.text)})):
                resp = orch.execute(plan, memory)
            job.q.put({"kind": "now", "text": "보고서용 그래프를 만들고 있어요"})
            charts("", resp, False)   # 결과 열 때 안 기다리게 미리 그려 둠
            done(resp)
        except Exception as ex:   # 화면에 이유 보여 주고 멈춤 (메시지에 이용자 조건 없음)
            job.error = f"{type(ex).__name__}: {ex}"
        job.q.put(None)

    threading.Thread(target=work, daemon=True).start()
    return job_id


# ── 화면 값 ──
def photo_of(place_id: str) -> dict | None:
    pics = orch.repo.photos(place_id, "rep") or orch.repo.photos(place_id, "route")
    if pics:
        return {"url": f"/photo/{place_id}/rep/0", "credit": "조사 사진" if not str(place_id).startswith("C") else "코스 중 사진"}
    vj = orch.repo.visitjeju_photo(place_id)
    if vj and vj.get("image", "").startswith(VJ_PHOTO_HOST):
        return {"url": vj["image"], "credit": "비짓제주 사진"}
    return None


def photo_list(place_id: str, kind: str) -> list[dict]:
    return [{"url": f"/photo/{place_id}/{kind}/{i}", "caption": soften(cap or "")} for i, (_, cap) in enumerate(orch.repo.photos(place_id, kind))]


def labels_now() -> set[str]:
    out = {orch.repo.label}
    for info in orch.repo.center_status().values():
        if info["files"]:
            out.add(info["label"])
    return out


def is_story(resp: OrchestratorResponse) -> bool:
    """위 큰 결과를 바꿀 답인지 (코스·장소 찾기)"""
    p = resp.results[-1] if resp.results else None
    if resp.plan.request.intent in (Intent.REPORT, Intent.UNSUPPORTED):   # '보고서로 정리해줘'는 직전 결과 다시 담는 거라 새 코스 아님
        return False
    return p is not None and ((p.agent == "course" and bool(p.payload.get("itinerary"))) or
                              (p.agent == "access" and bool(p.payload.get("verdicts")) and not p.payload.get("place_q")))


def wants_report(resp: OrchestratorResponse) -> bool:
    p = resp.results[-1] if resp.results else None
    return p is not None and (resp.plan.request.intent == Intent.REPORT or p.agent == "status")


def result_vm(key: str, resp: OrchestratorResponse) -> dict:
    p = resp.results[-1] if resp.results else None
    photo = photo_of if uses(resp.plan.request, "bf_photos") else (lambda _: None)
    if resp.plan.request.intent == Intent.REPORT or p is None:
        vm = views.text(resp)
    elif p.agent == "course" and p.payload.get("itinerary"):
        vm = views.course(resp, photo)
    elif p.agent == "access" and p.payload.get("verdicts"):
        vm = views.access(resp, photo)
    elif p.agent == "status":
        vm = views.status(resp, [f"/download/{key}/chart/{i}.png" for i, _ in enumerate(charts(key, resp, False))])
    else:
        vm = views.text(resp)
    vm["key"] = key
    vm["kakao"] = kakao(resp) if vm["kind"] in ("course", "access") else None
    vm["head"] = views.header(resp)
    vm["evidence"] = views.evidence(resp, analysis_spec(resp, s.public_view()) if resp.results else [])
    # 맞춤 여행은 항상, 대화 답은 기관 현황·보고서 요청일 때만 내려받기 붙임
    vm["downloads"] = downloads(key, resp) if key == "trip" or key.startswith("ver") or wants_report(resp) else []
    return vm


def kakao(resp: OrchestratorResponse) -> str | None:
    """카카오 지도. JS 키 있을 때만, iframe srcdoc용 HTML
    카카오 개발자 사이트에 http://localhost:8501 등록해야 뜸
    """
    if not s.kakao_js_key or not resp.results:
        return None
    p = resp.results[-1].payload
    if p.get("itinerary"):
        pts = [{**x, "order": n, "color": DAY_COLORS[(x["day"] - 1) % len(DAY_COLORS)],
                "label": f"{x['day']}일차 {x['order']}번째 · {x['verdict']}"} for n, x in enumerate(p["itinerary"], 1)]
        shops = [{"lat": float(e["lat"]), "lng": float(e["lng"]), "kind": e["kind"],
                  "name": f"{e['slot']} {e['name']}", "label": f"{e['day']}일차 · 장애인 화장실 {(e.get('facilities') or [{}])[0].get('state', '정보 없음')}"}
                 for e in p.get("extras", []) if e.get("lat") is not None]
    else:
        shown = p.get("shown_ids", [])
        vs = {v["place_id"]: v for v in p.get("verdicts", [])}
        pts = [{"name": f"{n}. {vs[i]['name']}", "lat": vs[i]["lat"], "lng": vs[i]["lng"], "color": VERDICT_COLORS[vs[i]["verdict"]], "color_cvd": VERDICT_COLORS_CVD[vs[i]["verdict"]],
                "label": f"{vs[i]['verdict']} {vs[i]['score']}점"} for n, i in enumerate(shown[:8], 1) if i in vs and vs[i].get("lat") is not None]
        shops = []
    return kakao_map_html(pts, s.kakao_js_key, height=520, extras=shops) if pts else None


@lru_cache(maxsize=256)
def nearby_info(lat: float, lng: float) -> dict:
    """장소 주변 사진·볼거리·식당·숙소 (카카오 키 있으면 카카오, 없으면 OSM Photon). 좌표만 보냄, 인터넷 없으면 빈 값"""
    if not s.place_info_online:
        return {}
    try:
        return place_info(lat, lng, kakao_key=s.kakao_rest_key)
    except Exception:   # 인터넷 안 되는 PC에서도 창은 열려야 함
        return {}


def downloads(key: str, resp: OrchestratorResponse) -> list[dict]:
    if not resp.results:
        return []
    out = []
    if any(r.agent == "course" and r.payload.get("itinerary") for r in resp.results):
        out.append({"label": "가족에게 보낼 일정표 (PNG)", "href": f"/download/{key}/itinerary.png", "primary": True, "chart": False})
    out.append({"label": "보고서 (.md)", "href": f"/download/{key}/report.md", "primary": not out, "chart": False})
    n = len(charts(key, resp, False))
    out += [{"label": f"그래프 {i + 1}", "href": f"/download/{key}/chart/{i}.png", "chart": True} for i in range(n)]
    return out


def find(session: Session, key: str) -> OrchestratorResponse | None:
    if key == "trip":
        return session.trip
    if key.startswith("ver"):   # 기록에서 고른 이전 결과
        i = int(key[3:] or 0)
        return session.versions[i]["resp"] if 0 <= i < len(session.versions) else None
    if key.startswith("chat"):
        i = int(key[4:] or 0)
        return session.history[i] if 0 <= i < len(session.history) else None
    return None


_CHART_CACHE: dict[int, list[tuple[str, bytes]]] = {}


def charts(key: str, resp: OrchestratorResponse, cvd: bool) -> list[tuple[str, bytes]]:
    """그래프는 응답마다 한 번만 그림"""
    k = (id(resp) << 1) | int(cvd)
    if k not in _CHART_CACHE:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        out = []
        for name, fig in report_charts(resp, cvd=cvd):
            png = save_png(fig, OUTPUT_DIR / f"{name}_{stamp}.png", {lab for r in resp.results for lab in r.data_labels})
            out.append((png.name, png.read_bytes()))
        _CHART_CACHE[k] = out
    return _CHART_CACHE[k]


def page(request: Request, name: str, **ctx: Any) -> Response:
    ss = sess(request)
    base = {"app_name": APP_NAME, "tagline": TAGLINE, "stt": s.stt_enabled, "has_result": ss.trip is not None or bool(ss.history), "static": "/static", "ver": STATIC_VER, "footer": footer(labels_now()), "llm": s.public_view()}
    return with_sid(request, tpl.TemplateResponse(request, name, {**base, **ctx}))


# ── 화면 ──
@app.get("/", response_class=HTMLResponse)
def index(request: Request, edit: int = 0, v: int | None = None):
    """v: 기록에서 이전 결과 보기 (읽기만). 없으면 지금 결과"""
    ss = sess(request)
    vers = ss.versions
    now_i = len(vers) - 1
    view_i = v if v is not None and 0 <= v < now_i else now_i
    old = view_i != now_i
    trip = None
    if ss.trip is not None:
        resp = vers[view_i]["resp"] if vers else ss.trip
        trip = result_vm(f"ver{view_i}" if old else "trip", resp)
        ch = changes(vers[view_i - 1]["resp"], resp) if vers and view_i > 0 else None
        if ch:   # 바뀐 점 + 새로 들어온 장소·바뀐 날 표시
            trip["changes"] = {**ch, "label": vers[view_i]["label"], "no": view_i + 1, "prev": view_i - 1}
            for d in trip.get("days", []):
                d["changed"] = d["day"] in ch["changed_days"]
                for f in d["flow"]:
                    if f.get("type") == "place":
                        f["new"] = f["place_id"] in ch["new_ids"]
    history = [{"i": i, "no": i + 1, "label": x["label"], "at": x["at"], "sum": version_sum(x["resp"]),
                "href": "/" if i == now_i else f"/?v={i}", "on": i == view_i} for i, x in enumerate(vers)] if len(vers) > 1 else []
    chat = []
    for i, r in enumerate(ss.history):
        chat.append({"i": i, "moved": id(r) in ss.moved, "said": r.plan.request.params.get("said") or r.plan.request.question,
                     "summary": soften(r.results[-1].summary) if r.results else "",
                     "vm": result_vm(f"chat{i}", r) if i == len(ss.history) - 1 and id(r) not in ss.moved else None,
                     "short": views.explain_html(r.answer.split("\n- 주의")[0][:400])})
    last_vm = chat[-1]["vm"] if chat and chat[-1]["vm"] else None
    # 더 물어보기 버튼: 지금 결과에 이어 물을 말. 이어 묻기 껐거나 결과 없으면 일반 예시
    src = last_vm or trip
    chips = (src or {}).get("follow_ups") if ss.link and src else None
    last_req = ss.memory.get("last_request") if ss.link else None
    return page(request, "index.html", trip=trip, chat=chat, edit=(bool(edit) or ss.trip is None) and not old,
                wiz=wizard(ss.form), link=ss.link, chips=chips or EXAMPLES, chips_from_result=bool(chips),
                history=history, old=old, evs=evidence_list(trip, None if old else last_vm),
                linked=(f"{last_req.question} — {', '.join(soften(x) for x in last_req.profile.describe()) or '조건 없음'}"
                        if last_req is not None else None),
                pending=({"q": ss.pending.request.params.get("said") or ss.pending.request.question,
                          "understood": soften(ss.pending.understood), "steps": ss.pending.steps} if ss.pending else None))


def evidence_list(trip: dict | None, last_vm: dict | None) -> list[dict]:
    """맨 아래 '근거와 검증'에 모을 결과 (큰 결과, 마지막 대화 답)"""
    out = []
    for vm, title in ((trip, "위 결과"), (last_vm, "마지막 대화 답")):
        if vm and (vm["evidence"].get("tables") or vm["evidence"].get("checks") or vm["head"].get("center")):
            out.append({**vm, "ev_title": title})
    return out


def version_sum(resp: OrchestratorResponse) -> str:
    """기록 탭 요약 (예: '제주시 · 2일 · 6곳')"""
    req = resp.plan.request
    main = resp.results[-1] if resp.results else None
    if main is not None and main.agent == "course" and main.payload.get("itinerary"):
        return f"{req.region or '제주 전체'} · {req.days or 1}일 · {len(main.payload['itinerary'])}곳"
    if main is not None and main.payload.get("shown_ids"):
        return f"{req.region or '제주 전체'} · {len(main.payload['shown_ids'])}곳"
    return req.region or "제주 전체"


def wizard(form: dict) -> dict:
    try:
        emds = sorted(set(orch.repo.places()["emd"].dropna().astype(str)))
    except DataNotReady:
        emds = []
    today = datetime.now(KST).date()
    return {"mobility": MOBILITY_UI, "vision": VISION_UI, "hearing": HEARING_UI, "cognition": COGNITION_UI, "needs": NEED_UI,
            "transport": TRANSPORT_UI, "interests": INTEREST_UI, "rides": [0, *RIDE_OPTIONS],
            "regions": ["제주 전체", "제주시", "서귀포시", *emds], "origins": ["모름", "제주국제공항", *sorted(EMD)],
            "extras": {"lunch": "점심 식당", "dinner": "저녁 식당", "lodging": "숙소"},
            "today": today.isoformat(), "v": form}


@app.get("/favicon.ico")
def favicon():
    return Response((WEB / "static" / "img" / "favicon.png").read_bytes(), media_type="image/png")


@app.get("/new")
def new(request: Request):
    """처음으로: 이 브라우저의 결과·대화·조건 지움"""
    sid = request.cookies.get("sid") or ""
    if sid in SESSIONS:
        SESSIONS[sid] = Session()
    return RedirectResponse("/", status_code=303)


@app.get("/place/{key}/{place_id}", response_class=HTMLResponse)
def place(request: Request, key: str, place_id: str):
    resp = find(sess(request), key)
    d = views.place_detail(resp, place_id, photo_list) if resp is not None else None
    if d is None:
        return HTMLResponse("<p>이 장소 정보를 찾지 못했어요. 결과를 다시 만들어 주세요.</p>", status_code=404)
    d["links"] = search_links(d["card"]["name"])
    d["card"]["photo"] = photo_of(place_id)
    xy = next(((v["lat"], v["lng"]) for r in resp.results for v in r.payload.get("verdicts", []) + r.payload.get("itinerary", [])
               if v.get("place_id") == place_id and v.get("lat") is not None), None)
    info = nearby_info(round(float(xy[0]), 4), round(float(xy[1]), 4)) if xy else {}
    around = info.get("around") or {}
    d["online"] = {"sights": (info.get("sights") or [])[:4], "food": around.get("먹을 곳", [])[:5],
                   "stay": around.get("숙소", [])[:3], "failed": bool(around.get("failed")),
                   "kakao": around.get("source") == "kakao", "photos": (info.get("photos") or [])[:4]} if info else None
    return tpl.TemplateResponse(request, "place.html", {"d": d})


@app.get("/photo/{place_id}/{kind}/{i}")
def photo(place_id: str, kind: str, i: int, w: int = 640):
    pics = orch.repo.photos(place_id, kind)
    if not (0 <= i < len(pics)):
        return Response(status_code=404)
    uri = photo_data_uri(str(pics[i][0]), min(max(w, 120), 1200), ratio=0.75 if kind == "rep" else None)
    return Response(base64.b64decode(uri.split(",", 1)[1]), media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    stat = orch.repo.center_status()
    return page(request, "settings.html", center=[{"t": t, "no": CENTER_INFO[t][0], "name": CENTER_INFO[t][1], **stat[t],
                                                   "label": tag(stat[t]["label"]) if stat[t]["label"] else "없음"} for t in CENTER_TABLES],
                cost=f"${orch.llm.month_cost_usd():.3f}" if s.public_view()["llm_enabled"] else None,
                budget=s.llm_monthly_budget_usd)


# ── API ──
def _list(v: str | None) -> list[str]:
    return [x for x in (v or "").split(",") if x]


@app.post("/api/trip")
def run_trip(request: Request, f: str = Form(...)):
    """3단계 입력 → 코스·장소 찾기. f: 입력값 JSON"""
    ss = sess(request)
    try:
        return _run_trip(ss, json.loads(f))
    except (ValueError, TypeError) as ex:   # pydantic ValidationError도 ValueError
        return JSONResponse({"error": f"입력값을 읽지 못했어요: {type(ex).__name__}"}, status_code=400)


def _run_trip(ss: Session, v: dict) -> JSONResponse:
    ss.form = v
    prof = dict(**merge_conditions(v.get("mob", []), v.get("vis", []), v.get("hear", []), v.get("needs", []), v.get("cog", [])),
                transport=v.get("trans", "car"), companions=int(v.get("people") or 1), max_ride_min=int(v.get("ride") or 0) or None,
                interests=list(v.get("int", [])))
    extra = (v.get("extra") or "").strip()   # 직접 적은 말: plan_form이 읽어서 조건에 더함 (버튼 값 우선)
    course = v.get("what", "course") == "course"
    region, days, origin = v.get("region") or "제주 전체", int(v.get("days") or 2), v.get("origin") or "모름"
    form = {"profile": TravelerProfile(**prof), "days": days, "region": region,
            "intent": (Intent.COURSE if course else Intent.ACCESS).value, "origin": None if origin == "모름" else origin}
    if course:
        start_date = v.get("start") or datetime.now(KST).date().isoformat()
        date.fromisoformat(start_date)
        form |= {"start_date": start_date, "extras": [x for x in v.get("extras", []) if x in ("lunch", "dinner", "lodging")]}
    plan = orch.plan_form(f"{region} {days}일 여행 코스" if course else f"{region}에서 갈 만한 곳", form, extra=extra)

    def done(resp: OrchestratorResponse) -> None:
        ss.keep(resp, "조건 바꿔 다시 분석" if ss.versions else "처음 결과")

    return JSONResponse({"job": start(plan, ss.memory, done)})


@app.post("/api/ask")
def ask(request: Request, q: str = Form(""), confirmed: int = Form(0)):
    """대화. 고칠 게 분명한 말·보고서·답 못하는 질문은 바로 실행, 새 분석은 확인 카드 거쳐서 실행"""
    ss = sess(request)
    if confirmed and ss.pending is not None:
        plan = ss.pending
    elif not q.strip():
        return JSONResponse({"error": "질문을 적어 주세요."}, status_code=400)
    else:
        plan = orch.plan(q.strip(), ss.mem(), ask_profile=True)
        edit = plan.request.params.get("edit") or {}
        run = plan.request.intent in (Intent.UNSUPPORTED, Intent.REPORT) or (bool(edit) and not edit.get("confirm"))
        if not run:
            ss.pending = plan
            return JSONResponse({"confirm": soften(plan.understood), "steps": plan.steps})
    ss.pending = None

    def done(resp: OrchestratorResponse) -> None:
        ss.history.append(resp)
        if ss.trip is not None and ss.link and is_story(resp):
            said = resp.plan.request.params.get("said") or resp.plan.request.question
            ss.keep(resp, f"‘{said[:16]}{'…' if len(said) > 16 else ''}’ 반영")
            ss.moved.add(id(resp))

    return JSONResponse({"job": start(plan, ss.mem(), done)})


@app.post("/api/stt")
async def stt(audio: UploadFile = File(...)):
    """음성 → 글자 (STT_MODE=api일 때만). 외부 API로 가고 저장 안 함"""
    if not s.stt_enabled:
        return JSONResponse({"error": "음성 입력이 꺼져 있어요(.env의 STT_MODE)."}, status_code=400)
    text, rec = orch.llm.transcribe(await audio.read(), filename=audio.filename or "voice.webm")
    return JSONResponse({"text": text} if text else {"error": f"음성 인식에 실패했어요({rec.error}). 글자로 입력해 주세요."})


@app.post("/api/again")
def again(request: Request, i: int = Form(...)):
    """지난 답을 맨 아래로 옮겨서 다시 그림"""
    ss = sess(request)
    if 0 <= i < len(ss.history):
        ss.history.append(ss.history.pop(i))
    return JSONResponse({"ok": True})


@app.post("/api/cancel")
def cancel(request: Request):
    sess(request).pending = None
    return JSONResponse({"ok": True})


@app.post("/api/link")
def link(request: Request, on: int = Form(1)):
    sess(request).link = bool(on)
    return JSONResponse({"ok": True})


@app.post("/api/compare/close")
def compare_close(request: Request):
    """'바꾸기 전과 비교' 닫기. 바뀐 점은 위에 계속 보이고 이전 결과는 기록 탭에서 봄"""
    return JSONResponse({"ok": True})


@app.get("/api/events/{job_id}")
async def events(job_id: str):
    """분석 단계 알림(SSE). 마지막에 done 또는 error"""
    job = JOBS.get(job_id)
    if job is None:
        return Response(status_code=404)

    async def gen():
        while True:
            try:
                item = await asyncio.to_thread(job.q.get, True, 0.5)
            except queue.Empty:
                yield ": keep\n\n"
                continue
            if item is None:
                yield f"event: {'error' if job.error else 'done'}\ndata: {json.dumps({'error': job.error}, ensure_ascii=False)}\n\n"
                JOBS.pop(job_id, None)
                return
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/reset")
def reset(request: Request):
    """세션 기록, 내려받기 파일, 넣은 센터 CSV, LLM 캐시 전부 지움"""
    for d in (OUTPUT_DIR, DATA_DIR / "center"):
        shutil.rmtree(d, ignore_errors=True)
    orch.llm.clear_cache()
    orch.repo.clear_cache(center_only=True)
    SESSIONS.pop(request.cookies.get("sid") or "", None)
    _CHART_CACHE.clear()
    return RedirectResponse("/", status_code=303)


@app.post("/api/center/{table}")
async def center_upload(table: str, files: list[UploadFile] = File(...)):
    if table not in CENTER_TABLES:
        return JSONResponse({"error": "모르는 표입니다."}, status_code=400)
    dest = DATA_DIR / "center"
    dest.mkdir(parents=True, exist_ok=True)
    ok, errors = 0, []
    for i, f in enumerate(files):
        tmp = dest / f"_check_{table}.csv"
        tmp.write_bytes(await f.read())
        try:
            read_csv_auto(tmp, table=table)
        except Exception as ex:   # 열 이름이 명세랑 다르면 저장 안 함
            errors.append(f"{f.filename}: {ex}")
            tmp.unlink(missing_ok=True)
            continue
        tmp.rename(dest / f"{table}_{datetime.now():%H%M%S}_{i}.csv")
        ok += 1
    orch.repo.clear_cache(center_only=True)
    return JSONResponse({"ok": ok, "errors": errors})


@app.post("/api/center-clear")
def center_clear():
    shutil.rmtree(DATA_DIR / "center", ignore_errors=True)
    orch.repo.clear_cache(center_only=True)
    return RedirectResponse("/settings", status_code=303)


# ── 내려받기 ──
def _share_photos(resp: OrchestratorResponse, course) -> dict:
    if not uses(resp.plan.request, "bf_photos"):
        return {}
    out = {}
    for x in course.payload.get("itinerary", []):
        pics = orch.repo.photos(x["place_id"], "rep") or orch.repo.photos(x["place_id"], "route")
        if pics:
            out[x["place_id"]] = pics[0][0]
    return out


@app.get("/download/{key}/{name}")
def download(request: Request, key: str, name: str):
    ss = sess(request)
    resp = find(ss, key)
    if resp is None:
        return Response("결과가 없어요. 화면을 새로 고쳐 주세요.", status_code=404, media_type="text/plain; charset=utf-8")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    ck = (id(resp), name)
    if name == "report.md":
        if ck not in ss.cache:
            ss.cache[ck] = build_markdown(resp, s.public_view()).encode("utf-8")
        if request.query_params.get("view") == "1":   # 보고서 미리 보기
            return Response(ss.cache[ck], media_type="text/plain; charset=utf-8")
        return Response(ss.cache[ck], media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=gochigapseo_report_{stamp}.md"})
    if name == "itinerary.png":
        course = next((r for r in resp.results if r.agent == "course" and r.payload.get("itinerary")), None)
        if course is None:
            return Response(status_code=404)
        if ck not in ss.cache:
            ss.cache[ck] = itinerary_png(course, resp.plan.request.month, _share_photos(resp, course), resp.plan.request.start_date,
                                         TRANSPORT_KO.get(resp.plan.request.profile.transport))
        inline = request.query_params.get("view") == "1"
        return Response(ss.cache[ck], media_type="image/png",
                        headers={"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=jeju_itinerary_{stamp}.png"})
    return Response(status_code=404)


@app.get("/download/{key}/chart/{i}.png")
def chart(request: Request, key: str, i: int, dl: int = 0):
    resp = find(sess(request), key)
    figs = charts(key, resp, request.cookies.get("cvd") == "1") if resp is not None else []
    if not (0 <= i < len(figs)):
        return Response(status_code=404)
    name, png = figs[i]
    return Response(png, media_type="image/png", headers={"Content-Disposition": f"{'attachment' if dl else 'inline'}; filename={name}"})
