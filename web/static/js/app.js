// 화면 동작. 계산·판단은 서버(에이전트)가 하고, 여기서는 입력 모으기·진행 표시·창 열기만 한다.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const root = document.documentElement;
  const post = (url, data) => fetch(url, { method: "POST", body: data instanceof FormData ? data : new URLSearchParams(data || {}) });

  // ── 막 분석을 끝낸 화면이면 차례로 나타나게(새로 고침·토글로 다시 그릴 때는 움직이지 않는다) ──
  let justRan = false;
  try { justRan = sessionStorage.getItem("just-ran") === "1"; sessionStorage.removeItem("just-ran"); } catch (e) { /* 없음 */ }
  if (justRan) {
    // 새 대화 답이면 그 답만, 위 결과가 바뀌었으면(맞춤 여행·대화로 고친 코스) 화면 전체를 차례로
    const target = document.getElementById((location.hash || "").slice(1));
    const qa = target?.closest(".qa");
    const scope = qa && !qa.classList.contains("moved") ? qa : document.querySelector("main");
    scope.classList.add("just-ran");
    $$(".result > *, .day, .plist > li, .compare", scope).forEach((el, k) => el.style.setProperty("--i", Math.min(k, 8)));
    for (const day of $$(".day", scope)) $$(".flow > li", day).forEach((el, k) => el.style.setProperty("--i", Math.min(k + 2, 14)));
    $$(".jeju .stops .stop", scope).forEach((el, k) => el.style.setProperty("--i", k));
    $$(".jeju .shops > g", scope).forEach((el, k) => el.style.setProperty("--i", k + 2));   // .pop 이 --i 를 물려받는다
    setTimeout(() => scope.classList.remove("just-ran"), 3500);   // 끝나면 표시를 지워 다음 동작에 영향이 없게
  }

  // ── 처음으로(로고): 결과가 있으면 한 번 묻는다(내려받기 전에 지워지지 않게) ──
  document.addEventListener("click", e => {
    const a = e.target.closest("[data-new]");
    if (a && document.body.dataset.hasResult === "1" && !confirm("지금 결과와 대화를 지우고 처음부터 시작할까요?\n(필요하면 먼저 일정표·보고서를 내려받으세요)")) e.preventDefault();
  });

  // ── 보기 설정(큰 글씨·고대비·색 구별): 이 브라우저에만 기억 ──
  const VIEW_MSG = {
    bigtext: ["글씨를 크게 했어요.", "글씨를 원래 크기로 했어요."],
    contrast: ["고대비로 바꿨어요.", "고대비를 껐어요."],
    cvd: ["화면 색을 파랑·주황 계열로 바꿨어요. 적록색약도 구별하기 쉬운 색이에요.", "화면 색을 원래대로 했어요."],
  };
  let msgTimer;
  const viewMsg = text => {
    const m = $("#viewMsg");
    if (!m) return;
    m.textContent = text; m.hidden = false;
    clearTimeout(msgTimer); msgTimer = setTimeout(() => { m.hidden = true; }, 3500);
  };
  try { localStorage.removeItem("view:big"); } catch (e) { /* 예전 키 */ }
  for (const b of $$("[data-view]")) {
    const k = b.dataset.view;
    let on = false;
    try { on = localStorage.getItem("view:" + k) === "1"; } catch (e) { /* 저장소를 못 쓰면 기본값 */ }
    root.classList.toggle(k, on); b.setAttribute("aria-pressed", String(on));
    b.addEventListener("click", () => {
      const now = !root.classList.contains(k);
      root.classList.toggle(k, now); b.setAttribute("aria-pressed", String(now));
      try { localStorage.setItem("view:" + k, now ? "1" : "0"); } catch (e) { /* 화면은 바뀐다 */ }
      if (k === "cvd") document.cookie = "cvd=" + (now ? "1" : "0") + "; path=/; SameSite=Strict";   // 그래프 색도 맞춘다
      if (VIEW_MSG[k]) viewMsg(VIEW_MSG[k][now ? 0 : 1]);
    });
  }

  // ── 분석 진행 표시: 에이전트가 실제로 남긴 단계를 실시간으로 ──
  function follow(job, hash) {
    const box = $("#busy"), list = $("#busySteps"), now = $("#busyNow");
    list.innerHTML = ""; now.textContent = "에이전트가 분석하고 있어요"; box.hidden = false;
    const es = new EventSource("/api/events/" + job);
    es.onmessage = e => {
      const s = JSON.parse(e.data);
      if (s.kind === "now") { now.textContent = s.text; return; }
      const li = document.createElement("li"); li.textContent = s.text; list.append(li);
      list.scrollTop = list.scrollHeight;
    };
    es.addEventListener("done", () => {
      es.close();
      try { sessionStorage.setItem("just-ran", "1"); } catch (x) { /* 움직임 없이 그냥 보인다 */ }
      // '조건 바꾸기'(?edit=1)·기록 보기(?v=)에서 돌렸으면 주소의 ?를 떼고 새 결과로 감 (그대로 새로 고치면 입력 화면만 다시 보임)
      if (location.search) location.replace(location.pathname + "#" + hash);
      else { location.hash = hash; location.reload(); }
    });
    es.addEventListener("error", e => {
      es.close();
      let msg = "분석 중 문제가 생겼어요.";
      try { const d = JSON.parse(e.data || "{}"); if (d.error) msg += " (" + d.error + ")"; } catch (x) { /* 연결 끊김 */ }
      now.textContent = msg + " 화면을 새로 고쳐 다시 해 주세요.";
      const b = document.createElement("button"); b.className = "btn"; b.textContent = "닫기"; b.onclick = () => { box.hidden = true; };
      list.after(b);
    });
  }

  // ── 3단계 입력 ──
  const wiz = $("#wizard");
  if (wiz) {
    const steps = $$(".wstep", wiz), tabs = $$("[data-go]", wiz);
    const show = i => {
      const prev = +(wiz.dataset.step || 0);
      wiz.dataset.step = i;
      steps.forEach((s, k) => {
        s.hidden = k !== i;
        s.classList.remove("go-next", "go-prev");
        if (k === i && i !== prev) { void s.offsetWidth; s.classList.add(i > prev ? "go-next" : "go-prev"); }   // 밀어내기
      });
      tabs.forEach((t, k) => t.setAttribute("aria-selected", String(k === i)));
      $("[data-prev]", wiz).disabled = i === 0;
      $("[data-next]", wiz).hidden = i === steps.length - 1;
      $("[data-now]", wiz).hidden = i === steps.length - 1;
      $("[data-start]", wiz).hidden = i !== steps.length - 1;
    };
    tabs.forEach(t => t.addEventListener("click", () => show(+t.dataset.go)));
    $("[data-prev]", wiz).addEventListener("click", () => show(Math.max(0, +wiz.dataset.step - 1)));
    $("[data-next]", wiz).addEventListener("click", () => show(Math.min(steps.length - 1, +wiz.dataset.step + 1)));
    show(0);
    // '해당 없음'과 다른 것을 함께 고르지 못하게, 모두 끄면 '해당 없음'으로
    for (const g of $$(".opts[data-none]", wiz)) {
      g.addEventListener("change", e => {
        const none = $("input[value=none]", g);
        if (e.target === none && none.checked) $$("input", g).forEach(x => { if (x !== none) x.checked = false; });
        else if (e.target.checked) none.checked = false;
        if (!$$("input:checked", g).length) none.checked = true;
      });
    }
    const what = () => ($("input[name=what]:checked", wiz) || {}).value || "course";
    const syncWhat = () => $$("[data-when=course]", wiz).forEach(x => { x.hidden = what() !== "course"; });
    wiz.addEventListener("change", syncWhat); syncWhat();
    wiz.addEventListener("submit", async e => {
      e.preventDefault();
      const fd = new FormData(wiz), v = {};
      for (const name of ["mob", "vis", "hear", "cog", "needs", "int", "extras"]) v[name] = fd.getAll(name).filter(x => x);
      for (const name of ["trans", "ride", "origin", "region", "start", "days", "what", "extra", "people"]) v[name] = fd.get(name) || "";
      const start = $("[data-start]", wiz);
      start.disabled = true;
      try {
        const r = await post("/api/trip", { f: JSON.stringify(v) });
        const d = await r.json().catch(() => ({}));   // 500이면 JSON이 아님
        if (!d.job) throw new Error(d.error || "");
        follow(d.job, "result-trip");
      } catch (err) {   // 서버 오류면 아무 반응 없어서 안 넘어가는 것처럼 보였음
        const msg = $("#tripMsg") || Object.assign(document.createElement("p"), { id: "tripMsg", className: "sub err", role: "alert" });
        msg.textContent = "분석을 시작하지 못했어요. 프로그램 창(검은 창)이 켜져 있는지 보고 다시 눌러 주세요." + (err.message ? " (" + err.message + ")" : "");
        start.closest(".wnav, .actions, div")?.after(msg);
      } finally {
        start.disabled = false;
      }
    });
  }

  // ── 대화 ──
  // 누르는 즉시 입력칸에 그 말을 넣고 '알아듣는 중' 화면을 띄운다(LLM이 질문을 읽는 몇 초 동안 아무 반응이 없던 문제)
  let asking = false;
  async function ask(q, confirmed, btn) {
    if (asking) return;
    asking = true;
    const input = $("#askInput");
    if (input && q) input.value = q;
    btn?.classList.add("asking");
    const box = $("#busy");
    if (box) { $("#busySteps").innerHTML = ""; $("#busyNow").textContent = confirmed ? "분석을 시작해요" : "‘" + q + "’ — 질문을 알아듣고 있어요"; box.hidden = false; }
    try {
      const r = await post("/api/ask", { q: q || "", confirmed: confirmed ? 1 : 0 });
      const d = await r.json();
      if (d.job) { follow(d.job, "chat-last"); return; }
      if (d.confirm) { location.hash = "confirm"; location.reload(); return; }
      throw new Error(d.error || "");
    } catch (err) {
      if (box) box.hidden = true;
      btn?.classList.remove("asking");
      asking = false;
      const msg = $("#askMsg") || Object.assign(document.createElement("p"), { id: "askMsg", className: "sub err" });
      msg.textContent = "질문을 보내지 못했어요. " + (err.message || "잠시 뒤 다시 눌러 주세요.");
      $("#askForm")?.after(msg);
    }
  }
  const form = $("#askForm");
  if (form) form.addEventListener("submit", e => { e.preventDefault(); const q = $("#askInput").value.trim(); if (q) ask(q, false, e.submitter); });
  document.addEventListener("click", e => {
    const a = e.target.closest("[data-ask]");
    if (a) { e.preventDefault(); ask(a.dataset.ask, false, a); }
  });
  if (location.hash === "#confirm") {   // 확인 카드로 와서 눈에 띄게, '이대로 분석하기'에 바로 손이 가게
    const c = $("#confirm");
    if (c) { c.classList.add("flash"); $("#confirmRun")?.focus({ preventScroll: true }); }
  }
  document.addEventListener("click", async e => {
    const a = e.target.closest("[data-again]");
    if (!a) return;
    await post("/api/again", { i: a.dataset.again }); location.hash = "chat-last"; location.reload();
  });
  for (const d of $$("details.mdprev")) {   // 보고서 미리 보기: 처음 펼칠 때만 불러온다
    d.addEventListener("toggle", async () => {
      if (!d.open || d.dataset.loaded) return;
      d.dataset.loaded = "1";
      $("pre.md", d).textContent = await (await fetch(d.dataset.src)).text();
    });
  }
  $("#confirmRun")?.addEventListener("click", () => ask("", true));
  $("#confirmCancel")?.addEventListener("click", async () => { await post("/api/cancel"); location.reload(); });
  $("#linkTrip")?.addEventListener("change", async e => { await post("/api/link", { on: e.target.checked ? 1 : 0 }); location.reload(); });
  $("#cmpClose")?.addEventListener("click", async () => { await post("/api/compare/close"); $(".compare")?.remove(); });

  // ── 장소 자세히 보기(창) ──
  const dlg = $("#placeDlg");
  document.addEventListener("click", async e => {
    const b = e.target.closest("[data-open]");
    if (!b || !dlg) return;
    $("#placeBody").innerHTML = '<p class="loading">장소 정보를 불러오고 있어요…</p>';
    dlg.showModal();
    const r = await fetch(`/place/${b.dataset.key}/${encodeURIComponent(b.dataset.open)}`);
    $("#placeBody").innerHTML = await r.text();
    $$("#placeBody > .dlg > *").forEach((el, k) => el.style.setProperty("--i", Math.min(k, 10)));
    dlg.scrollTop = 0;
  });
  const closeDlg = () => {   // 살짝 작아지며 닫기
    if (!dlg.open || dlg.classList.contains("closing")) return;
    dlg.classList.add("closing");
    setTimeout(() => { dlg.classList.remove("closing"); dlg.close(); }, 170);
  };
  dlg?.addEventListener("click", e => { if (e.target === dlg) closeDlg(); });   // 창 바깥을 누르면 닫기
  dlg?.addEventListener("cancel", e => { e.preventDefault(); closeDlg(); });     // Esc
  dlg?.querySelector(".dlg-bar")?.addEventListener("submit", e => { e.preventDefault(); closeDlg(); });

  // ── 그림 지도 확대·이동: viewBox를 바꾸고 --z(배율)를 알려 번호 점·선·글씨는 화면에서 같은 크기로 ──
  const W = 1000, H = 560, MAXZ = 24;
  for (const box of $$(".mapart")) {
    const svg = $("svg.jeju", box);
    if (!svg) continue;
    const start = svg.getAttribute("viewBox").split(/\s+/).map(Number);
    let vb = start.slice();
    const set = v => {
      const w = Math.min(W * 1.15, Math.max(W / MAXZ, v[2])), h = w * H / W;
      const x = Math.min(W * 1.05 - w, Math.max(-W * 0.05, v[0])), y = Math.min(H * 1.05 - h, Math.max(-H * 0.05, v[1]));
      vb = [x, y, w, h];
      svg.setAttribute("viewBox", vb.map(n => n.toFixed(1)).join(" "));
      svg.style.setProperty("--z", (W / w).toFixed(3));
    };
    const toSvg = (cx, cy) => { const r = svg.getBoundingClientRect(); return [vb[0] + (cx - r.left) / r.width * vb[2], vb[1] + (cy - r.top) / r.height * vb[3]]; };
    const zoomAt = (k, px, py) => {   // (px, py) 자리를 그대로 두고 k배
      const w = vb[2] / k, h = vb[3] / k;
      set([px - (px - vb[0]) * (w / vb[2]), py - (py - vb[1]) * (h / vb[3]), w, h]);
    };
    svg.addEventListener("wheel", e => {
      e.preventDefault();
      const [px, py] = toSvg(e.clientX, e.clientY);
      zoomAt(e.deltaY < 0 ? 1.25 : 0.8, px, py);
    }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", e => { drag = { x: e.clientX, y: e.clientY, vb: vb.slice(), moved: false }; });
    svg.addEventListener("pointermove", e => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      if (!drag.moved) { drag.moved = true; svg.setPointerCapture(e.pointerId); svg.classList.add("dragging"); }
      const r = svg.getBoundingClientRect();
      set([drag.vb[0] - dx / r.width * drag.vb[2], drag.vb[1] - dy / r.height * drag.vb[3], drag.vb[2], drag.vb[3]]);
    });
    const end = () => {
      if (drag?.moved) { svg.dataset.dragged = "1"; setTimeout(() => delete svg.dataset.dragged, 0); }
      drag = null; svg.classList.remove("dragging");
    };
    svg.addEventListener("pointerup", end); svg.addEventListener("pointercancel", end);
    for (const b of $$("[data-zoom]", box)) {
      b.addEventListener("click", () => {
        const c = [vb[0] + vb[2] / 2, vb[1] + vb[3] / 2];
        if (b.dataset.zoom === "in") zoomAt(1.5, ...c);
        else if (b.dataset.zoom === "out") zoomAt(1 / 1.5, ...c);
        else set(start.slice());
      });
    }
  }

  // ── 지도 번호 ↔ 카드 (결과마다 따로) ──
  for (const res of $$(".result")) {
    const card = n => $(`.place[data-num="${n}"]`, res);
    const dot = n => $(`.jeju .stop[data-num="${n}"]`, res);
    for (const g of $$(".jeju .stop", res)) {
      const go = () => {
        if (g.closest("svg")?.dataset.dragged) return;   // 끌어서 옮긴 것은 누른 것으로 보지 않는다
        const c = card(g.dataset.num);
        if (c) { c.scrollIntoView({ behavior: "smooth", block: "center" }); c.classList.add("on"); setTimeout(() => c.classList.remove("on"), 1600); }
      };
      g.addEventListener("click", go);
      g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    }
    for (const c of $$(".place[data-num]", res)) {
      c.addEventListener("mouseenter", () => dot(c.dataset.num)?.classList.add("on"));
      c.addEventListener("mouseleave", () => dot(c.dataset.num)?.classList.remove("on"));
    }
  }

  // ── 지도 종류(카카오 지도 / 그림 지도): 카카오 키가 있으면 카카오가 기본(화면에 보일 때 불러온다) ──
  for (const f of $$(".mapkakao:not([hidden])")) if (!f.srcdoc) f.srcdoc = f.dataset.srcdoc;
  // 카카오 지도 안 번호 점을 누르면(iframe → postMessage) 옆 일정의 그 장소 카드로 이동
  window.addEventListener("message", e => {
    const n = e.data && e.data.kakaoStop;
    if (!n) return;
    const box = [...$$(".mapkakao")].find(f => f.contentWindow === e.source);
    const c = box && $(`.place[data-num="${n}"]`, box.closest(".result") || document);
    if (c) { c.scrollIntoView({ behavior: "smooth", block: "center" }); c.classList.add("on"); setTimeout(() => c.classList.remove("on"), 1600); }
  });
  for (const t of $$("[data-maptab]")) {
    t.addEventListener("click", () => {
      const box = t.closest(".mapbox");
      const kakao = t.dataset.maptab === "kakao";
      $$("[data-maptab]", box).forEach(b => b.setAttribute("aria-selected", String(b === t)));
      $(".mapart", box).hidden = kakao;
      const f = $(".mapkakao", box);
      f.hidden = !kakao;
      if (kakao && !f.srcdoc) f.srcdoc = f.dataset.srcdoc;
    });
  }

  // ── 말로 묻기(선택 기능): 녹음 → 서버가 음성 인식 → 입력칸에 채움(바로 보내지 않고 확인하게) ──
  const mic = $("#micBtn");
  if (mic) {
    let rec = null, chunks = [];
    mic.addEventListener("click", async () => {
      if (rec && rec.state === "recording") { rec.stop(); return; }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        rec = new MediaRecorder(stream); chunks = [];
        rec.ondataavailable = e => chunks.push(e.data);
        rec.onstop = async () => {
          stream.getTracks().forEach(t => t.stop());
          mic.textContent = "말로 묻기"; mic.setAttribute("aria-pressed", "false");
          $("#micMsg").textContent = "듣고 있어요…";
          const fd = new FormData(); fd.append("audio", new Blob(chunks, { type: rec.mimeType || "audio/webm" }), "voice.webm");
          const d = await (await post("/api/stt", fd)).json();
          if (d.text) { $("#askInput").value = d.text; $("#micMsg").textContent = "들은 내용을 확인하고 ‘묻기’를 누르세요."; $("#askInput").focus(); }
          else $("#micMsg").textContent = d.error || "음성 인식에 실패했어요.";
        };
        rec.start(); mic.textContent = "말 끝나면 다시 누르기"; mic.setAttribute("aria-pressed", "true");
      } catch (e) { $("#micMsg").textContent = "마이크를 쓸 수 없어요. 브라우저의 마이크 권한을 확인하세요."; }
    });
  }

  // ── 읽어 주기: 브라우저 내장 한국어 음성(외부로 보내지 않음) ──
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-tts]");
    if (!b) return;
    const sp = window.speechSynthesis;
    if (!sp) { b.textContent = "이 브라우저는 읽어 주기를 지원하지 않아요"; return; }
    if (sp.speaking) { sp.cancel(); b.textContent = "읽어 주기"; return; }
    const el = $(b.dataset.tts);
    const t = (el?.innerText || "").replace(/\(자료표[^)]*\)/g, "").replace(/→/g, ",").replace("읽어 주기", "");
    const u = new SpeechSynthesisUtterance(t); u.lang = "ko-KR"; u.rate = 0.95;
    u.onend = () => { b.textContent = "읽어 주기"; };
    sp.speak(u); b.textContent = "멈추기";
  });

  // ── 센터 CSV 넣기(설정 화면) ──
  for (const f of $$("form.upload")) {
    f.addEventListener("submit", async e => {
      e.preventDefault();
      const r = await post("/api/center/" + f.dataset.table, new FormData(f));
      const d = await r.json();
      $("#upMsg").textContent = d.error || `${d.ok}개 넣었어요.` + (d.errors?.length ? " 넣지 못한 파일: " + d.errors.join(" / ") : "");
      if (d.ok) setTimeout(() => location.reload(), 900);
    });
  }
})();
