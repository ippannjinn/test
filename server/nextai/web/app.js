import { renderMarkdown } from "./md.js";

const S = { user: null, csrf: null, server: null, trusted: false, info: {}, view: "chat", convId: null, convs: [],
  attach: [], mode: "auto", streams: new Map(), prefs: {} };
const app = document.getElementById("app");

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const fmtBytes = (n) => n < 1024 ? `${n}B` : n < 1048576 ? `${(n / 1024).toFixed(1)}KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)}MB` : `${(n / 1073741824).toFixed(2)}GB`;
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-";
const fmtSecs = (s) => s < 60 ? `${Math.round(s)}秒` : `${Math.floor(s / 60)}分${Math.round(s % 60)}秒`;
let toastTimer;
function toast(msg) {
  document.querySelector(".toast")?.remove();
  const t = h("div", { class: "toast", role: "status" }, msg);
  document.body.append(t);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 3500);
}
function confirmDanger(msg) { return window.confirm(msg); }

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function api(path, opts = {}, retry = true) {
  const headers = { "X-Requested-With": "nextai" };
  if (S.csrf) headers["X-CSRF-Token"] = S.csrf;
  let body;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opts.body); }
  const r = await fetch(path, { method: opts.method || "GET", headers, body, credentials: "same-origin" });
  if (r.status === 401 && retry && !path.startsWith("/api/auth/login")) {
    if (await tryRefresh()) return api(path, opts, false);
    showLogin();
    throw new ApiError(401, "unauthenticated", "ログインが必要です");
  }
  const data = (r.headers.get("content-type") || "").includes("json") ? await r.json() : {};
  if (!r.ok) {
    const e = data.error || {};
    if (e.code === "password_change_required") showPasswordChange(true);
    throw new ApiError(r.status, e.code, e.message || `エラー (HTTP ${r.status})`);
  }
  return data;
}

async function tryRefresh() {
  try {
    const r = await fetch("/api/auth/refresh", { method: "POST", headers: { "X-Requested-With": "nextai" }, credentials: "same-origin" });
    if (!r.ok) return false;
    applySession(await r.json());
    return true;
  } catch { return false; }
}

function applySession(d) {
  S.user = d.user; S.csrf = d.csrf_token; S.server = d.server; S.trusted = d.trusted_device;
  S.prefs = Object.assign({ theme: "auto", enter_send: !matchMedia("(pointer: coarse)").matches }, d.user?.ui_prefs || {});
  applyPrefs();
}
function applyPrefs() {
  const t = S.prefs.theme;
  if (t === "dark" || t === "light") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}

// ---------------------------------------------------------------- boot / auth screens
async function boot() {
  S.info = await fetch("/api/info").then((r) => r.json()).catch(() => ({}));
  document.title = S.info.name || "NextAI";
  let ok = false;
  try { applySession(await api("/api/auth/session", {}, false)); ok = true; } catch { ok = await tryRefresh(); }
  if (!ok) return showLogin();
  if (S.user.must_change_password) return showPasswordChange(true);
  showApp();
}

function brand() {
  return h("div", { class: "brand" }, h("img", { src: "icon.svg", alt: "" }), S.info.name || "NextAI");
}

function showLogin() {
  for (const es of S.streams.values()) es.close();
  S.streams.clear();
  S.user = null; S.csrf = null;
  const err = h("div", { class: "error" });
  const user = h("input", { class: "input", autocomplete: "username", required: true, autocapitalize: "none" });
  const pass = h("input", { class: "input", type: "password", autocomplete: "current-password", required: true });
  const trust = h("input", { type: "checkbox", checked: true });
  const btn = h("button", { class: "btn primary", type: "submit" }, "ログイン");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault(); err.textContent = ""; btn.disabled = true;
    try {
      const d = await api("/api/auth/login", { method: "POST", body: { username: user.value.trim(), password: pass.value, trust_device: trust.checked } }, false);
      applySession(d);
      if (d.must_change_password) showPasswordChange(true); else showApp();
    } catch (ex) { err.textContent = ex.message; btn.disabled = false; }
  } },
    h("label", { class: "field" }, h("span", {}, "ユーザー名"), user),
    h("label", { class: "field" }, h("span", {}, "パスワード"), pass),
    h("label", { class: "check" }, trust, "この端末を信頼する (次回からログイン不要)"),
    err, h("div", { class: "row" }, h("div", { class: "spacer" }), btn));
  app.className = "";
  app.replaceChildren(h("div", { class: "auth" }, h("div", { class: "card" }, brand(),
    h("p", { class: "muted small" }, S.info.login_message || "管理者から受け取ったアカウントでログインしてください。"),
    form,
    h("p", { class: "muted small" }, "証明書の警告が出る場合は ", h("a", { href: "/ca.crt" }, "CA証明書"), " を端末にインストールしてください。"))));
  user.focus();
}

function passwordForm(forced, done) {
  const err = h("div", { class: "error" });
  const cur = h("input", { class: "input", type: "password", autocomplete: "current-password", required: true });
  const n1 = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const n2 = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true });
  const others = h("input", { type: "checkbox", checked: true });
  const btn = h("button", { class: "btn primary", type: "submit" }, "パスワードを変更");
  return h("form", { onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    if (n1.value !== n2.value) { err.textContent = "新しいパスワードが一致しません"; return; }
    btn.disabled = true;
    try {
      const d = await api("/api/auth/password", { method: "POST", body: { current_password: cur.value, new_password: n1.value, revoke_other_sessions: others.checked } });
      applySession(d); done();
    } catch (ex) { err.textContent = ex.message; } finally { btn.disabled = false; }
  } },
    h("label", { class: "field" }, h("span", {}, forced ? "初期パスワード" : "現在のパスワード"), cur),
    h("label", { class: "field" }, h("span", {}, "新しいパスワード (10文字以上)"), n1),
    h("label", { class: "field" }, h("span", {}, "新しいパスワード (確認)"), n2),
    h("label", { class: "check" }, others, "他の端末のログインをすべて解除する"),
    err, h("div", { class: "row" }, h("div", { class: "spacer" }), btn));
}

function showPasswordChange(forced) {
  app.className = "";
  app.replaceChildren(h("div", { class: "auth" }, h("div", { class: "card" }, brand(),
    h("h2", {}, "パスワードの変更"),
    h("p", { class: "muted small" }, forced ? "初回ログインのため、新しいパスワードを設定してください。" : ""),
    passwordForm(forced, () => { toast("パスワードを変更しました"); showApp(); }),
    h("button", { class: "btn ghost small", onclick: logout }, "ログアウト"))));
}

async function logout(forget = false) {
  try { await api("/api/auth/logout", { method: "POST", body: { forget_device: forget === true } }); } catch { /* ignore */ }
  showLogin();
}

// ---------------------------------------------------------------- app shell
let shell;
function showApp() {
  app.className = "";
  const nav = h("nav", { class: "nav" });
  const convs = h("div", { class: "convs" });
  const main = h("div", { class: "view" });
  const title = h("div", { class: "grow title" }, "");
  const layout = h("div", { class: "layout" },
    h("aside", { class: "sidebar" },
      h("div", { class: "top" }, brand(), h("button", { class: "btn primary", onclick: () => { go("chat"); newChat(); } }, "＋ 新しいチャット")),
      nav, convs,
      h("div", { class: "me", onclick: () => go("settings") }, avatarEl(), h("div", { class: "grow" }, h("div", {}, S.user.display_name), h("div", { class: "muted small" }, S.user.username)))),
    h("div", { class: "overlay", onclick: () => layout.classList.remove("open") }),
    h("main", { class: "main" },
      h("div", { class: "topbar" }, h("button", { class: "btn ghost", "aria-label": "menu", onclick: () => layout.classList.add("open") }, "☰"), title,
        h("button", { class: "btn ghost", onclick: () => { go("chat"); newChat(); } }, "＋")),
      main));
  shell = { layout, nav, convs, main, title };
  app.replaceChildren(layout);
  window.onhashchange = route;
  route();
}

function avatarEl() {
  if (S.user.has_avatar) return h("img", { class: "avatar", src: `/api/account/avatar?t=${Date.now()}`, alt: "" });
  return h("div", { class: "avatar" }, (S.user.display_name || "?").slice(0, 1));
}

const NAV = [["chat", "チャット"], ["create", "画像・動画・音楽"], ["files", "ファイル"], ["memory", "メモリ"], ["settings", "設定"]];
function renderNav() {
  shell.nav.replaceChildren(...NAV.map(([id, label]) => h("button", { class: S.view === id ? "active" : "", onclick: () => go(id) }, label)));
}
function go(view, id) { location.hash = id ? `#${view}/${id}` : `#${view}`; }
function route() {
  const [view, id] = location.hash.replace(/^#/, "").split("/");
  S.view = NAV.some(([v]) => v === view) ? view : "chat";
  shell.layout.classList.remove("open");
  renderNav();
  shell.title.textContent = NAV.find(([v]) => v === S.view)[1];
  loadConvs();
  if (S.view === "chat") openChat(id || null);
  else if (S.view === "create") viewCreate();
  else if (S.view === "files") viewFiles();
  else if (S.view === "memory") viewMemory();
  else viewSettings();
}

async function loadConvs() {
  try { S.convs = (await api("/api/conversations")).conversations; } catch { return; }
  shell.convs.replaceChildren(
    ...(S.convs.length ? S.convs.map((c) => h("div", { class: "conv" + (c.id === S.convId && S.view === "chat" ? " active" : ""), onclick: () => go("chat", c.id) },
      h("span", { class: "t", title: c.title }, c.title),
      h("button", { class: "x", title: "削除", onclick: async (e) => {
        e.stopPropagation();
        if (!confirmDanger(`「${c.title}」を削除しますか？`)) return;
        await api(`/api/conversations/${c.id}`, { method: "DELETE" });
        if (S.convId === c.id) go("chat"); else loadConvs();
      } }, "×"))) : [h("div", { class: "muted small" }, "会話はまだありません")]));
}

// ---------------------------------------------------------------- chat
let chat;
function newChat() { S.convId = null; S.attach = []; if (S.view === "chat") openChat(null); }

async function openChat(convId) {
  S.convId = convId;
  const inner = h("div", { class: "inner" });
  const messages = h("div", { class: "messages" }, inner);
  const ta = h("textarea", { rows: 1, placeholder: "メッセージを入力", "aria-label": "message" });
  const attached = h("div", { class: "attached" });
  const fileIn = h("input", { type: "file", multiple: true, hidden: true, onchange: () => uploadAttachments(fileIn.files) });
  const mode = h("select", { "aria-label": "mode", onchange: () => { S.mode = mode.value; } },
    h("option", { value: "auto" }, "自動"), h("option", { value: "fast" }, "速さ優先"), h("option", { value: "quality" }, "品質優先 (待ってでも高品質)"));
  mode.value = S.mode;
  const sendBtn = h("button", { class: "btn primary", onclick: () => send() }, "送信");
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(220, ta.scrollHeight) + "px"; });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && S.prefs.enter_send) { e.preventDefault(); send(); }
  });
  const status = h("span", { class: "muted small" });
  const composer = h("div", { class: "composer" }, h("div", { class: "inner" }, attached,
    h("div", { class: "box" }, h("button", { class: "btn ghost", title: "ファイルを添付", onclick: () => fileIn.click() }, "📎"), ta, sendBtn),
    h("div", { class: "tools" }, mode, status, h("div", { class: "spacer" }), h("span", { class: "muted small" }, S.prefs.enter_send ? "Enterで送信 / Shift+Enterで改行" : "")),
    fileIn));
  chat = { inner, messages, ta, attached, sendBtn, status };
  shell.main.replaceChildren(h("div", { class: "chat" }, messages, composer));
  renderAttached();
  refreshStatus();
  if (!convId) { renderEmpty(); ta.focus(); return; }
  try {
    const d = await api(`/api/conversations/${convId}`);
    shell.title.textContent = d.conversation.title;
    inner.replaceChildren(...d.messages.map(renderMessage));
    for (const j of d.active_jobs) attachLive(j.id);
    scrollDown(true);
  } catch (e) { inner.replaceChildren(h("div", { class: "empty" }, e.message)); }
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const q = s.queue;
    chat.status.textContent = q.pending ? `混雑状況: 待機 ${q.pending}件 (目安 ${fmtSecs(q.est_wait_seconds)})` : (s.load_level !== "NORMAL" ? `サーバー負荷: ${s.load_level}` : "");
  } catch { /* ignore */ }
}

function renderEmpty() {
  const ideas = ["週末の旅行プランを考えて", "Pythonで CSV を集計するスクリプトを書いて実行して", "最新のAIニュースを調べて要約して",
    "夕焼けの海辺のイラストを生成して", "この文章を英語に翻訳して: ", "落ち着いたピアノのBGMを作曲して"];
  chat.inner.replaceChildren(h("div", { class: "empty" }, h("h1", {}, `こんにちは、${S.user.display_name}さん`),
    h("div", {}, "普通に話しかけるだけで、AIが最適なモデル・ツール・推論の深さを自動で選びます。"),
    h("div", { class: "suggest" }, ideas.map((t) => h("button", { onclick: () => { chat.ta.value = t; chat.ta.focus(); } }, t)))));
}

function scrollDown(force) {
  const m = chat?.messages;
  if (!m) return;
  if (force || m.scrollHeight - m.scrollTop - m.clientHeight < 160) m.scrollTop = m.scrollHeight;
}

function assetEl(a) {
  const url = `/api/files/${a.id}/content`;
  if (a.mime?.startsWith("image/")) return h("a", { href: url, target: "_blank", rel: "noopener" }, h("img", { src: url, alt: a.name, loading: "lazy" }));
  if (a.mime?.startsWith("audio/")) return h("audio", { controls: true, src: url, preload: "none" });
  if (a.mime?.startsWith("video/")) return h("video", { controls: true, src: url, preload: "none", class: "vid" });
  return h("a", { class: "file-link", href: `${url}?download=1` }, "⬇ ", a.name);
}

function metaChips(meta) {
  const p = meta?.profile || {};
  const chips = [];
  if (p.label) chips.push(h("span", { class: "chip accent", title: (p.reasons || []).join("\n") }, p.label));
  if (meta?.model_name) chips.push(h("span", { class: "chip" }, meta.model_name));
  for (const t of meta?.tools || []) chips.push(h("span", { class: "chip" }, "🔧 " + t));
  if (meta?.duration) chips.push(h("span", { class: "chip" }, `${meta.duration}s`));
  return chips.length ? h("div", { class: "meta" }, chips) : null;
}

function mdEl(text) {
  const el = h("div", { class: "md", html: renderMarkdown(text) });
  el.querySelectorAll("button.copy").forEach((b) => b.addEventListener("click", () => {
    navigator.clipboard?.writeText(b.closest(".code").querySelector("code").textContent).then(() => toast("コピーしました"));
  }));
  return el;
}

function renderMessage(m) {
  if (m.role === "user") {
    const atts = (m.meta?.attachments || []).map((a) => h("span", { class: "chip" }, "📎 " + a.name));
    return h("div", { class: "msg user" }, h("div", { class: "bubble" }, m.content, atts.length ? h("div", { class: "meta" }, atts) : null));
  }
  const assets = (m.meta?.assets || []).map(assetEl);
  return h("div", { class: "msg assistant" }, h("div", { class: "bubble" }, mdEl(m.content),
    assets.length ? h("div", { class: "assets" }, assets) : null, metaChips(m.meta)));
}

function renderAttached() {
  chat.attached.replaceChildren(...S.attach.map((f) => h("span", { class: "chip" }, "📎 " + f.name,
    h("button", { class: "btn ghost small", onclick: () => { S.attach = S.attach.filter((x) => x.id !== f.id); renderAttached(); } }, "×"))));
}

async function uploadAttachments(files) {
  for (const f of files) {
    const form = new FormData();
    form.append("file", f);
    chat.status.textContent = `${f.name} をアップロード中…`;
    try { const d = await api("/api/files", { method: "POST", form }); S.attach.push(d.file); }
    catch (e) { toast(`${f.name}: ${e.message}`); }
  }
  chat.status.textContent = "";
  renderAttached();
}

async function send() {
  const text = chat.ta.value.trim();
  if (!text) return;
  chat.sendBtn.disabled = true;
  try {
    const d = await api(`/api/conversations/${S.convId || "new"}/messages`, { method: "POST", body: { content: text, attachments: S.attach.map((a) => a.id), mode: S.mode } });
    const isNew = !S.convId;
    if (!S.convId || chat.inner.querySelector(".empty")) chat.inner.replaceChildren();
    chat.inner.append(renderMessage({ role: "user", content: text, meta: { attachments: S.attach } }));
    chat.ta.value = ""; chat.ta.style.height = "auto";
    S.attach = []; renderAttached();
    S.convId = d.conversation_id;
    if (isNew) { history.replaceState(null, "", `#chat/${d.conversation_id}`); loadConvs(); }
    attachLive(d.job.id);
    scrollDown(true);
  } catch (e) { toast(e.message); }
  finally { chat.sendBtn.disabled = false; }
}

const PHASE = { plan: "計画中", act: "実行中", verify: "検証中" };
function attachLive(jobId) {
  const statusLine = h("div", {}, "キューに登録しました…");
  const prog = h("div", { class: "progress", hidden: true }, h("div"));
  const steps = h("div", { class: "steps" });
  const content = h("div", { class: "md typing" });
  const chips = h("div", { class: "meta" });
  const assets = h("div", { class: "assets" });
  const cancel = h("button", { class: "btn small", onclick: async () => { try { await api(`/api/jobs/${jobId}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); } } }, "停止");
  const live = h("div", { class: "live" }, h("div", { class: "row" }, statusLine, h("div", { class: "spacer" }), cancel), prog, steps);
  const el = h("div", { class: "msg assistant", "data-job": jobId }, h("div", { class: "bubble" }, live, content, assets, chips));
  chat.inner.append(el);
  let text = "", pending = false;
  const paint = () => {
    if (pending) return; pending = true;
    requestAnimationFrame(() => { pending = false; content.innerHTML = renderMarkdown(text); scrollDown(); });
  };
  const addStep = (s) => { steps.append(h("div", { class: "step" }, s)); steps.scrollTop = steps.scrollHeight; };
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  S.streams.set(jobId, es);
  const on = (type, fn) => es.addEventListener(type, (ev) => { try { fn(JSON.parse(ev.data)); } catch (e) { console.error(e); } });
  on("profile", (p) => {
    chips.replaceChildren(h("span", { class: "chip accent", title: (p.reasons || []).join("\n") }, p.label),
      p.model_name ? h("span", { class: "chip" }, p.model_name) : null,
      ...(p.tools || []).map((t) => h("span", { class: "chip" }, "🔧 " + t)));
    if (p.revision > 1) addStep(`⟳ プロファイル再評価: ${(p.reasons || []).slice(-1)[0] || p.label}`);
    if (p.wait_for_quality) addStep("品質優先のため混雑中でも本来の設定で順番を待っています");
  });
  on("queue", (q) => {
    if (q.started) statusLine.textContent = "実行中…";
    else if (q.model_state === "loading") statusLine.textContent = `モデルを読み込み中… (待ち順位 ${q.position ?? "-"})`;
    else if (q.position) statusLine.textContent = `待ち順位 ${q.position} / 推定待ち時間 ${fmtSecs(q.eta_seconds || 0)}`;
  });
  on("step", (s) => { statusLine.textContent = `${PHASE[s.phase] || s.phase} (ステップ ${s.n}/${s.max})`; });
  on("plan", (p) => addStep("📋 計画:\n" + p.text));
  on("tool_call", (t) => addStep(`🔧 ${t.name} ${t.args ? t.args.slice(0, 120) : ""}`));
  on("tool_result", (t) => addStep(`${t.ok ? "✓" : "✗"} ${t.name}: ${(t.summary || "").slice(0, 160)}`));
  on("verify", (v) => addStep(v.result === "pass" ? "✓ 検証OK" : "✗ 検証で問題を検出 → 修正中"));
  on("notice", (n) => addStep("ℹ " + n.message));
  on("reset", (r) => { if (r.moved) addStep("… " + r.moved.slice(0, 300)); text = ""; paint(); });
  on("snapshot", (s) => { text = s.text || ""; paint(); });
  on("delta", (d) => { text += d.text; paint(); });
  on("progress", (p) => { prog.hidden = false; prog.firstChild.style.width = `${Math.round((p.value || 0) * 100)}%`; if (p.message) statusLine.textContent = p.message; });
  on("asset", (a) => { assets.append(assetEl({ id: a.file_id, name: a.name, mime: a.mime })); scrollDown(); });
  on("error", (e) => addStep("⚠ " + (e.message || "エラー")));
  on("done", async (d) => {
    es.close(); S.streams.delete(jobId);
    content.classList.remove("typing");
    if (d.status !== "done") {
      live.replaceChildren(h("div", { class: d.status === "cancelled" ? "muted" : "error" }, d.status === "cancelled" ? "停止しました" : `エラー: ${d.error || ""}`), steps);
      return;
    }
    if (S.view === "chat" && S.convId && chat.inner.contains(el)) {
      try {
        const conv = await api(`/api/conversations/${S.convId}`);
        const m = conv.messages.find((x) => x.job_id === jobId && x.role === "assistant");
        if (m) { const fresh = renderMessage(m); if (steps.childElementCount) fresh.querySelector(".bubble").prepend(h("details", { class: "live" }, h("summary", {}, "実行ログ"), steps)); el.replaceWith(fresh); }
      } catch { /* keep live view */ }
      loadConvs();
    }
  });
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) S.streams.delete(jobId); };
}

// ---------------------------------------------------------------- create (image / video / music)
let createKind = "image";
async function viewCreate() {
  const wrap = h("div", { class: "view-inner" });
  shell.main.replaceChildren(wrap);
  let caps;
  try { caps = await api("/api/generate/capabilities"); } catch (e) { wrap.append(h("p", { class: "error" }, e.message)); return; }
  const tabs = h("div", { class: "tabs" }, [["image", "画像"], ["video", "動画"], ["music", "音楽"]].map(([k, l]) =>
    h("button", { class: createKind === k ? "active" : "", onclick: () => { createKind = k; viewCreate(); } }, l)));
  const c = caps[createKind];
  const prompt = h("textarea", { class: "input", rows: 3, placeholder: { image: "例: 桜並木を歩く猫、水彩画風", video: "例: 波が打ち寄せる砂浜、夕暮れ", music: "例: 落ち着いたローファイ・ピアノ" }[createKind] });
  const params = h("div", { class: "grid2" });
  const fields = {};
  const sel = (key, label, opts, val) => { fields[key] = h("select", { class: "input" }, opts.map(([v, t]) => h("option", { value: v }, t))); fields[key].value = String(val); params.append(h("label", { class: "field" }, h("span", {}, label), fields[key])); };
  if (createKind === "image") {
    const max = c.limits.max_side;
    sel("size", "サイズ", [[512, "512×512 (速い)"], [768, "768×768"], [1024, "1024×1024"]].filter(([v]) => v <= max), Math.min(768, max));
  } else if (createKind === "video") {
    sel("frames", "長さ", [[17, "約1秒"], [33, "約2秒"], [49, "約3秒"]].filter(([v]) => v <= c.limits.max_frames), 17);
    sel("width", "解像度", [[480, "480×272 (速い)"], [832, "832×480"]].filter(([v]) => v <= c.limits.max_side), 480);
  } else {
    sel("seconds", "長さ", [[8, "8秒"], [15, "15秒"], [30, "30秒"]].filter(([v]) => v <= c.limits.max_seconds), 8);
  }
  const mode = h("select", { class: "input" }, h("option", { value: "auto" }, "自動"), h("option", { value: "fast" }, "速さ優先"), h("option", { value: "quality" }, "品質優先"));
  params.append(h("label", { class: "field" }, h("span", {}, "モード"), mode));
  const out = h("div");
  const btn = h("button", { class: "btn primary", disabled: !c.available, onclick: async () => {
    if (!prompt.value.trim()) return;
    const p = {};
    if (createKind === "image") { p.width = +fields.size.value; p.height = +fields.size.value; }
    if (createKind === "video") { p.frames = +fields.frames.value; p.width = +fields.width.value; p.height = Math.round(+fields.width.value * 480 / 832 / 16) * 16; }
    if (createKind === "music") p.seconds = +fields.seconds.value;
    btn.disabled = true;
    try {
      const d = await api(`/api/generate/${createKind}`, { method: "POST", body: { prompt: prompt.value.trim(), params: p, mode: mode.value } });
      trackGeneration(d.job.id, out, () => { btn.disabled = false; loadGallery(gallery); });
    } catch (e) { toast(e.message); btn.disabled = false; }
  } }, "生成する");
  const gallery = h("div", { class: "gallery" });
  wrap.append(h("h2", {}, "画像・動画・音楽の生成"), tabs,
    h("div", { class: "card" },
      c.available ? null : h("p", { class: "error" }, "この生成機能は現在利用できません (モデル未インストール)"),
      h("div", { class: "notice" }, c.notice, c.models?.[0] ? ` モデル: ${c.models[0].name} (${c.models[0].license})` : ""),
      h("label", { class: "field" }, h("span", {}, "内容 (日本語でOK)"), prompt), params,
      h("div", { class: "row" }, h("span", { class: "muted small" }, `1回あたり生成クォータ ${c.cost} 消費`), h("div", { class: "spacer" }), btn), out),
    h("h3", {}, "これまでの生成物"), gallery);
  loadGallery(gallery);
}

function trackGeneration(jobId, out, done) {
  const line = h("div", { class: "muted small" }, "キューに登録しました…");
  const prog = h("div", { class: "progress" }, h("div"));
  const res = h("div", { class: "assets" });
  out.replaceChildren(h("div", { class: "live" }, line, prog), res);
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  const on = (t, fn) => es.addEventListener(t, (ev) => fn(JSON.parse(ev.data)));
  on("queue", (q) => { line.textContent = q.started ? "生成中…" : q.model_state === "loading" ? "モデル準備中…" : `待ち順位 ${q.position ?? "-"} / 推定 ${fmtSecs(q.eta_seconds || 0)}`; });
  on("progress", (p) => { prog.firstChild.style.width = `${Math.round((p.value || 0) * 100)}%`; if (p.message) line.textContent = p.message; });
  on("tool_result", (t) => { if (t.name === "prompt_refine") line.textContent = `プロンプト: ${t.summary}`; });
  on("asset", (a) => res.append(assetEl({ id: a.file_id, name: a.name, mime: a.mime })));
  on("done", (d) => { es.close(); line.textContent = d.status === "done" ? "完了しました" : `失敗: ${d.error || d.status}`; line.className = d.status === "done" ? "muted small" : "error"; done(); });
}

async function loadGallery(el) {
  try {
    const files = (await api("/api/files?kind=generated&limit=60")).files;
    el.replaceChildren(...files.map((f) => h("div", { class: "g" },
      f.mime.startsWith("image/") ? h("a", { href: `/api/files/${f.id}/content`, target: "_blank", rel: "noopener" }, h("img", { src: `/api/files/${f.id}/content`, loading: "lazy", alt: f.name }))
        : f.mime.startsWith("audio/") ? h("audio", { controls: true, src: `/api/files/${f.id}/content`, preload: "none" })
          : h("a", { class: "file-link", href: `/api/files/${f.id}/content?download=1` }, "⬇ ", f.name),
      h("div", { class: "cap", title: f.meta?.prompt || f.name }, f.meta?.prompt || f.name))));
    if (!files.length) el.replaceChildren(h("p", { class: "muted" }, "まだありません"));
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------- files
async function viewFiles() {
  const wrap = h("div", { class: "view-inner" });
  shell.main.replaceChildren(wrap);
  const input = h("input", { type: "file", multiple: true, hidden: true, onchange: async () => {
    for (const f of input.files) {
      const form = new FormData(); form.append("file", f);
      try { await api("/api/files", { method: "POST", form }); } catch (e) { toast(`${f.name}: ${e.message}`); }
    }
    viewFiles();
  } });
  let d;
  try { d = await api("/api/files?limit=500"); } catch (e) { wrap.append(h("p", { class: "error" }, e.message)); return; }
  const pct = Math.min(100, (d.used_mb / Math.max(1, d.quota_mb)) * 100);
  wrap.append(h("div", { class: "row" }, h("h2", {}, "ファイル"), h("div", { class: "spacer" }), h("button", { class: "btn primary", onclick: () => input.click() }, "アップロード"), input),
    h("div", { class: "card" }, h("div", { class: "row small" }, `使用量 ${d.used_mb}MB / ${d.quota_mb}MB`), h("div", { class: "meter" }, h("div", {})),
      h("p", { class: "muted small" }, "PDF・Word・Excel・PowerPoint・テキスト・コード・画像を解析できます。チャットの📎からも添付できます。")),
    h("h3", {}, "一覧"),
    h("div", { class: "list" }, d.files.length ? d.files.map((f) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", { class: "title" }, f.name), h("div", { class: "muted small" }, `${fmtBytes(f.size)} · ${f.kind === "generated" ? "生成物" : "アップロード"} · ${fmtTime(f.created_at)}`)),
      h("button", { class: "btn small", onclick: () => { S.attach = [f]; go("chat"); } }, "チャットで使う"),
      h("a", { class: "btn small", href: `/api/files/${f.id}/content?download=1` }, "保存"),
      h("button", { class: "btn small danger", onclick: async () => { if (!confirmDanger(`${f.name} を削除しますか？`)) return; await api(`/api/files/${f.id}`, { method: "DELETE" }); viewFiles(); } }, "削除")))
      : h("p", { class: "muted" }, "ファイルはありません")));
  wrap.querySelector(".meter > div").style.width = `${pct}%`;
}

// ---------------------------------------------------------------- memory
async function viewMemory() {
  const wrap = h("div", { class: "view-inner" });
  shell.main.replaceChildren(wrap);
  const ta = h("textarea", { class: "input", rows: 2, placeholder: "例: 私はPythonが得意で、回答は簡潔な方が好き" });
  const add = h("button", { class: "btn primary", onclick: async () => {
    if (!ta.value.trim()) return;
    try { await api("/api/memory", { method: "POST", body: { content: ta.value.trim() } }); viewMemory(); } catch (e) { toast(e.message); }
  } }, "追加");
  let mems = [];
  try { mems = (await api("/api/memory")).memories; } catch (e) { toast(e.message); }
  wrap.append(h("h2", {}, "長期メモリ"),
    h("p", { class: "muted small" }, "AIがあなたについて覚えている情報です。会話中に「覚えておいて」と言うと追加されます。内容はあなた専用で、他の利用者からは見えません。"),
    h("div", { class: "card" }, ta, h("div", { class: "row" }, h("div", { class: "spacer" }), add)),
    h("h3", {}, `記憶 (${mems.length})`),
    h("div", { class: "list" }, mems.map((m) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", {}, m.pinned ? "📌 " : "", m.content), h("div", { class: "muted small" }, `${m.source === "agent" ? "AIが保存" : "手動"} · ${fmtTime(m.updated_at)}`)),
      h("button", { class: "btn small", onclick: async () => { await api(`/api/memory/${m.id}`, { method: "PATCH", body: { pinned: !m.pinned } }); viewMemory(); } }, m.pinned ? "固定解除" : "固定"),
      h("button", { class: "btn small", onclick: async () => {
        const v = prompt("内容を編集", m.content);
        if (v && v.trim()) { await api(`/api/memory/${m.id}`, { method: "PATCH", body: { content: v.trim() } }); viewMemory(); }
      } }, "編集"),
      h("button", { class: "btn small danger", onclick: async () => { await api(`/api/memory/${m.id}`, { method: "DELETE" }); viewMemory(); } }, "削除")))));
}

// ---------------------------------------------------------------- settings
async function viewSettings() {
  const wrap = h("div", { class: "view-inner" });
  shell.main.replaceChildren(wrap);
  let prof, devs, sess;
  try {
    [prof, devs, sess] = await Promise.all([api("/api/account/profile"), api("/api/account/devices"), api("/api/account/sessions")]);
  } catch (e) { wrap.append(h("p", { class: "error" }, e.message)); return; }
  const u = prof.user, us = prof.usage;
  const dn = h("input", { class: "input", value: u.display_name, maxlength: 64 });
  const bio = h("textarea", { class: "input", rows: 2, maxlength: 1000 }, u.bio || "");
  const theme = h("select", { class: "input" }, h("option", { value: "auto" }, "端末に合わせる"), h("option", { value: "light" }, "ライト"), h("option", { value: "dark" }, "ダーク"));
  theme.value = S.prefs.theme || "auto";
  const enter = h("input", { type: "checkbox", checked: !!S.prefs.enter_send });
  const avatarIn = h("input", { type: "file", accept: "image/*", hidden: true, onchange: async () => {
    const form = new FormData(); form.append("file", avatarIn.files[0]);
    try { await api("/api/account/avatar", { method: "POST", form }); S.user.has_avatar = true; toast("アイコンを更新しました"); showApp(); } catch (e) { toast(e.message); }
  } });
  const save = h("button", { class: "btn primary", onclick: async () => {
    try {
      const d = await api("/api/account/profile", { method: "PATCH", body: { display_name: dn.value.trim(), bio: bio.value, ui_prefs: { ...S.prefs, theme: theme.value, enter_send: enter.checked } } });
      S.user = d.user; S.prefs = { ...S.prefs, ...d.user.ui_prefs }; applyPrefs(); toast("保存しました");
    } catch (e) { toast(e.message); }
  } }, "保存");
  const meter = (used, total) => { const bar = h("div"); bar.style.width = `${Math.min(100, used / Math.max(1, total) * 100)}%`; return h("div", { class: "meter" }, bar); };
  wrap.append(h("h2", {}, "設定"),
    h("div", { class: "card" }, h("h3", {}, "プロフィール"),
      h("div", { class: "row" }, avatarEl(), h("button", { class: "btn small", onclick: () => avatarIn.click() }, "アイコンを変更"), avatarIn,
        u.has_avatar ? h("button", { class: "btn small", onclick: async () => { await api("/api/account/avatar", { method: "DELETE" }); S.user.has_avatar = false; showApp(); } }, "削除") : null),
      h("label", { class: "field" }, h("span", {}, "表示名"), dn),
      h("label", { class: "field" }, h("span", {}, "プロフィール"), bio),
      h("div", { class: "grid2" }, h("label", { class: "field" }, h("span", {}, "テーマ"), theme),
        h("label", { class: "check" }, enter, "Enterキーで送信する")),
      h("div", { class: "row" }, h("span", { class: "muted small" }, `ユーザー名: ${u.username} / ロール: ${u.role === "admin" ? "管理者" : "メンバー"}`), h("div", { class: "spacer" }), save)),
    h("div", { class: "card" }, h("h3", {}, "利用状況"),
      h("div", { class: "small" }, `ストレージ ${us.storage_used_mb}MB / ${us.storage_quota_mb}MB`), meter(us.storage_used_mb, us.storage_quota_mb),
      h("div", { class: "small" }, `本日の生成 ${us.generation_used_today} / ${us.generation_quota_daily}`), meter(us.generation_used_today, us.generation_quota_daily),
      h("p", { class: "muted small" }, `同時実行上限 ${us.concurrent_jobs} · 本日のリクエスト ${us.jobs_today}件 (クォータの変更は管理者に依頼してください)`)),
    h("div", { class: "card" }, h("h3", {}, "パスワード変更"), passwordForm(false, () => toast("パスワードを変更しました"))),
    h("div", { class: "card" }, h("h3", {}, "信頼済み端末"),
      h("p", { class: "muted small" }, "信頼済み端末では、期限内は再ログインなしで利用できます。紛失した端末はここで解除してください。"),
      h("div", { class: "list" }, devs.devices.length ? devs.devices.map((d) => h("div", { class: "item" },
        h("div", { class: "grow" }, h("div", { class: "title" }, d.name, d.current ? " (この端末)" : ""),
          h("div", { class: "muted small" }, `登録 ${fmtTime(d.created_at)} · 最終利用 ${fmtTime(d.last_used_at)} · ${{ active: "有効", revoked: "解除済み", expired: "期限切れ" }[d.status]}`)),
        d.status === "active" ? h("button", { class: "btn small", onclick: async () => { const n = prompt("端末名", d.name); if (n) { await api(`/api/account/devices/${d.id}`, { method: "PATCH", body: { name: n } }); viewSettings(); } } }, "名前変更") : null,
        d.status === "active" ? h("button", { class: "btn small danger", onclick: async () => { if (!confirmDanger(`「${d.name}」の信頼を解除しますか？`)) return; await api(`/api/account/devices/${d.id}`, { method: "DELETE" }); if (d.current) showLogin(); else viewSettings(); } }, "解除") : null))
        : h("p", { class: "muted small" }, "信頼済み端末はありません"))),
    h("div", { class: "card" }, h("h3", {}, "ログイン中のセッション"),
      h("div", { class: "list" }, sess.sessions.map((s) => h("div", { class: "item" },
        h("div", { class: "grow" }, h("div", { class: "title" }, (s.user_agent || "不明").slice(0, 80), s.current ? " (現在)" : ""),
          h("div", { class: "muted small" }, `${s.ip || ""} · 最終アクセス ${fmtTime(s.last_seen_at)}`)),
        s.current ? null : h("button", { class: "btn small danger", onclick: async () => { await api(`/api/account/sessions/${s.id}`, { method: "DELETE" }); viewSettings(); } }, "終了")))),
      h("div", { class: "row" }, h("div", { class: "spacer" }), h("button", { class: "btn small", onclick: async () => { const r = await api("/api/account/sessions/revoke-others", { method: "POST" }); toast(`${r.revoked}件のセッションを終了しました`); viewSettings(); } }, "他のセッションをすべて終了"))),
    h("div", { class: "card" }, h("h3", {}, "ログアウト"),
      h("div", { class: "row" }, h("button", { class: "btn", onclick: () => logout(false) }, "ログアウト"),
        h("button", { class: "btn danger", onclick: () => logout(true) }, "ログアウトしてこの端末の信頼を解除")),
      h("p", { class: "muted small" }, "証明書の警告が出る端末では ", h("a", { href: "/ca.crt" }, "CA証明書"), " をインストールしてください。")));
}

boot();
