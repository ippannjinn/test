import { renderMarkdown } from "./md.js";

const S = { user: null, csrf: null, server: null, trusted: false, info: {}, view: "chat", convId: null, convs: [],
  attach: [], mode: "auto", streams: new Map(), prefs: {}, activeJob: null, search: "" };
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

// Line icons (24x24, stroke) in the style of the major chat apps.
const ICONS = {
  plus: "M12 5v14M5 12h14",
  send: "M12 19V5M5 12l7-7 7 7",
  stop: "M7 7h10v10H7z",
  menu: "M4 6h16M4 12h16M4 18h16",
  panel: "M3 4h18v16H3zM9 4v16",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  newchat: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  check: "M20 6 9 17l-5-5",
  refresh: "M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6",
  image: "M3 3h18v18H3zM8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM21 15l-5-5L5 21",
  file: "M14 2H6v20h12V8zM14 2v6h6",
  brain: "M12 5a3 3 0 0 0-5.9.8A3 3 0 0 0 4 11a3 3 0 0 0 2 5.2A3 3 0 0 0 12 18zM12 5a3 3 0 0 1 5.9.8A3 3 0 0 1 20 11a3 3 0 0 1-2 5.2A3 3 0 0 1 12 18z",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  logout: "M9 21H5V3h4M16 17l5-5-5-5M21 12H9",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z",
  down: "M12 5v14M5 12l7 7 7-7",
  chevron: "M6 9l6 6 6-6",
  spark: "M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  bolt: "M13 2 3 14h9l-1 8 10-12h-9z",
  gem: "M6 3h12l4 6-10 12L2 9zM2 9h20M12 21 8 9l4-6 4 6z",
  code: "M16 18l6-6-6-6M8 6l-6 6 6 6",
  globe: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20",
  music: "M9 18V5l12-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
  pen: "M12 19l7-7 3 3-7 7zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18zM2 2l7.6 7.6",
  x: "M18 6 6 18M6 6l12 12",
  paperclip: "M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5",
};
function icon(name, size = 18) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", size); svg.setAttribute("height", size);
  svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round"); svg.setAttribute("aria-hidden", "true");
  svg.classList.add("ic");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICONS[name] || ""); svg.append(p);
  return svg;
}
const iconBtn = (name, label, onclick, cls = "") => h("button", { class: `icon-btn ${cls}`, title: label, "aria-label": label, type: "button", onclick }, icon(name));

const fmtBytes = (n) => n < 1024 ? `${n}B` : n < 1048576 ? `${(n / 1024).toFixed(1)}KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)}MB` : `${(n / 1073741824).toFixed(2)}GB`;
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-";
const fmtSecs = (s) => s < 60 ? `${Math.round(s)}秒` : `${Math.floor(s / 60)}分${Math.round(s % 60)}秒`;
const store = { get: (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } }, set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } } };
let toastTimer;
function toast(msg) {
  document.querySelector(".toast")?.remove();
  const t = h("div", { class: "toast", role: "status" }, msg);
  document.body.append(t);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 3500);
}
function confirmDanger(msg) { return window.confirm(msg); }
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch { toast("コピーできませんでした"); return; }
  if (btn) { const old = btn.innerHTML; btn.replaceChildren(icon("check")); btn.classList.add("done"); setTimeout(() => { btn.innerHTML = old; btn.classList.remove("done"); }, 1400); }
  else toast("コピーしました");
}

// Popover menu (conversation "…", account menu, mode picker).
function popover(anchor, items, { align = "left", up = false } = {}) {
  closePopover();
  const menu = h("div", { class: "popover", role: "menu" }, items.map((it) => it === "-" ? h("div", { class: "sep" }) :
    h("button", { class: `pop-item ${it.danger ? "danger" : ""} ${it.active ? "active" : ""}`, role: "menuitem", type: "button",
      onclick: (e) => { e.stopPropagation(); closePopover(); it.onclick(); } },
    it.icon ? icon(it.icon, 16) : null, h("span", { class: "grow" }, h("div", {}, it.label), it.desc ? h("div", { class: "desc" }, it.desc) : null),
    it.active ? icon("check", 16) : null)));
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = align === "right" ? r.right - mw : r.left;
  left = Math.max(8, Math.min(left, innerWidth - mw - 8));
  let top = up ? r.top - mh - 6 : r.bottom + 6;
  if (top + mh > innerHeight - 8) top = r.top - mh - 6;
  menu.style.left = `${left}px`; menu.style.top = `${Math.max(8, top)}px`;
  setTimeout(() => document.addEventListener("click", closePopover, { once: true }), 0);
}
function closePopover() { document.querySelectorAll(".popover").forEach((m) => m.remove()); }

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
  S.mode = store.get("nextai.mode", "auto");
  applyPrefs();
}
function applyPrefs() {
  const t = S.prefs.theme;
  if (t === "dark" || t === "light") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
async function setTheme(t) {
  S.prefs.theme = t; applyPrefs();
  try { const d = await api("/api/account/profile", { method: "PATCH", body: { ui_prefs: { ...S.prefs, theme: t } } }); S.user = d.user; } catch { /* keep local */ }
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
  return h("div", { class: "brand" }, h("img", { src: "icon.svg", alt: "" }), h("span", {}, S.info.name || "NextAI"));
}

function authScreen(...content) {
  app.className = "";
  app.replaceChildren(h("div", { class: "auth" }, h("div", { class: "auth-glow" }),
    h("div", { class: "auth-card" }, h("div", { class: "auth-logo" }, h("img", { src: "icon.svg", alt: "" })), ...content)));
}

function showLogin() {
  for (const es of S.streams.values()) es.close();
  S.streams.clear();
  S.user = null; S.csrf = null;
  const err = h("div", { class: "error" });
  const user = h("input", { class: "input", autocomplete: "username", required: true, autocapitalize: "none", placeholder: "ユーザー名" });
  const pass = h("input", { class: "input", type: "password", autocomplete: "current-password", required: true, placeholder: "パスワード" });
  const trust = h("input", { type: "checkbox", checked: true });
  const btn = h("button", { class: "btn primary block", type: "submit" }, "ログイン");
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
    err, btn);
  authScreen(h("h1", {}, `${S.info.name || "NextAI"} にログイン`),
    h("p", { class: "muted" }, S.info.login_message || "管理者から受け取ったアカウントでログインしてください。"),
    form,
    location.hostname.endsWith(".ts.net") ? null : h("p", { class: "muted small center" }, "証明書の警告が出る場合は ", h("a", { href: "/ca.crt" }, "CA証明書"), " を端末にインストールしてください。"));
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
  authScreen(h("h1", {}, "パスワードの変更"),
    h("p", { class: "muted" }, forced ? "初回ログインのため、新しいパスワードを設定してください。" : ""),
    passwordForm(forced, () => { toast("パスワードを変更しました"); showApp(); }),
    h("button", { class: "btn ghost small", onclick: logout }, "ログアウト"));
}

async function logout(forget = false) {
  try { await api("/api/auth/logout", { method: "POST", body: { forget_device: forget === true } }); } catch { /* ignore */ }
  showLogin();
}

// ---------------------------------------------------------------- app shell
let shell;
const NAV = [["chat", "チャット", "newchat"], ["create", "画像・動画・音楽", "image"], ["files", "ファイル", "file"], ["memory", "メモリ", "brain"], ["settings", "設定", "settings"]];

function showApp() {
  app.className = "";
  const nav = h("nav", { class: "nav" });
  const convs = h("div", { class: "convs" });
  const main = h("div", { class: "view" });
  const title = h("div", { class: "hdr-title" });
  const headerRight = h("div", { class: "hdr-right" });
  const search = h("input", { class: "search-input", type: "search", placeholder: "チャットを検索", "aria-label": "チャットを検索",
    oninput: () => { S.search = search.value.trim(); renderConvs(); } });
  const layout = h("div", { class: "layout" + (store.get("nextai.sidebar", true) ? "" : " collapsed") });
  const toggle = () => {
    if (matchMedia("(max-width: 860px)").matches) layout.classList.toggle("open");
    else { layout.classList.toggle("collapsed"); store.set("nextai.sidebar", !layout.classList.contains("collapsed")); }
  };
  const meBtn = h("button", { class: "me", type: "button", onclick: (e) => { e.stopPropagation(); accountMenu(meBtn); } },
    avatarEl(), h("div", { class: "grow" }, h("div", { class: "name" }, S.user.display_name), h("div", { class: "muted small" }, S.user.role === "admin" ? "管理者" : "メンバー")),
    icon("more"));
  layout.append(
    h("aside", { class: "sidebar" },
      h("div", { class: "side-top" }, brand(), iconBtn("panel", "サイドバーを閉じる", toggle)),
      h("button", { class: "new-chat", type: "button", onclick: () => { go("chat"); newChat(); } }, icon("newchat"), h("span", {}, "新しいチャット"), h("kbd", {}, "Ctrl ⇧ O")),
      h("div", { class: "search" }, icon("search", 16), search),
      nav,
      h("div", { class: "side-label" }, "チャット履歴"),
      convs,
      meBtn),
    h("div", { class: "overlay", onclick: () => layout.classList.remove("open") }),
    h("main", { class: "main" },
      h("header", { class: "hdr" }, iconBtn("panel", "サイドバー", toggle, "hdr-toggle"), title, headerRight),
      main));
  shell = { layout, nav, convs, main, title, headerRight, search };
  app.replaceChildren(layout);
  window.onhashchange = route;
  document.onkeydown = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "O" || e.key === "o")) { e.preventDefault(); go("chat"); newChat(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === "k") { e.preventDefault(); if (layout.classList.contains("collapsed")) toggle(); search.focus(); }
    else if (e.key === "Escape" && S.activeJob && S.view === "chat") stopGeneration();
  };
  route();
}

function accountMenu(anchor) {
  const t = S.prefs.theme || "auto";
  popover(anchor, [
    { icon: "settings", label: "設定", onclick: () => go("settings") },
    "-",
    { icon: "sun", label: "ライト", active: t === "light", onclick: () => setTheme("light") },
    { icon: "moon", label: "ダーク", active: t === "dark", onclick: () => setTheme("dark") },
    { icon: "spark", label: "端末に合わせる", active: t === "auto", onclick: () => setTheme("auto") },
    "-",
    { icon: "logout", label: "ログアウト", onclick: () => logout(false) },
  ], { up: true });
}

function avatarEl() {
  if (S.user.has_avatar) return h("img", { class: "avatar", src: `/api/account/avatar?t=${Date.now()}`, alt: "" });
  return h("div", { class: "avatar" }, (S.user.display_name || "?").slice(0, 1));
}

function renderNav() {
  shell.nav.replaceChildren(...NAV.filter(([id]) => id !== "chat" && id !== "settings").map(([id, label, ic]) =>
    h("button", { class: "nav-item" + (S.view === id ? " active" : ""), type: "button", onclick: () => go(id) }, icon(ic), h("span", {}, label))));
}
function go(view, id) { location.hash = id ? `#${view}/${id}` : `#${view}`; }
function route() {
  const [view, id] = location.hash.replace(/^#/, "").split("/");
  S.view = NAV.some(([v]) => v === view) ? view : "chat";
  shell.layout.classList.remove("open");
  closePopover();
  renderNav();
  shell.title.textContent = "";
  shell.title.onclick = null;
  shell.headerRight.replaceChildren();
  loadConvs();
  if (S.view === "chat") openChat(id || null);
  else if (S.view === "create") viewCreate();
  else if (S.view === "files") viewFiles();
  else if (S.view === "memory") viewMemory();
  else viewSettings();
}

async function loadConvs() {
  try { S.convs = (await api("/api/conversations?limit=300")).conversations; } catch { return; }
  renderConvs();
}

function convGroups(list) {
  const now = new Date(); const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const groups = [["今日", day0], ["昨日", day0 - 86400], ["過去7日間", day0 - 7 * 86400], ["過去30日間", day0 - 30 * 86400], ["それ以前", -Infinity]];
  const out = groups.map(([label]) => [label, []]);
  for (const c of list) out[groups.findIndex(([, from]) => c.updated_at >= from)][1].push(c);
  return out.filter(([, items]) => items.length);
}

function renderConvs() {
  const q = S.search.toLowerCase();
  const list = q ? S.convs.filter((c) => c.title.toLowerCase().includes(q)) : S.convs;
  if (!list.length) { shell.convs.replaceChildren(h("div", { class: "muted small pad" }, q ? "見つかりません" : "会話はまだありません")); return; }
  shell.convs.replaceChildren(...convGroups(list).map(([label, items]) => h("div", { class: "conv-group" }, h("div", { class: "group-label" }, label),
    items.map((c) => {
      const more = iconBtn("more", "メニュー", (e) => {
        e.stopPropagation();
        popover(more, [
          { icon: "edit", label: "名前を変更", onclick: () => renameConv(c) },
          { icon: "trash", label: "削除", danger: true, onclick: () => deleteConv(c) },
        ], { align: "right" });
      }, "conv-more");
      return h("div", { class: "conv" + (c.id === S.convId && S.view === "chat" ? " active" : ""), role: "button", tabindex: 0,
        onclick: () => go("chat", c.id), onkeydown: (e) => { if (e.key === "Enter") go("chat", c.id); } },
      h("span", { class: "t", title: c.title }, c.title), more);
    }))));
}

async function renameConv(c) {
  const t = prompt("チャットの名前", c.title);
  if (!t || !t.trim()) return;
  try { await api(`/api/conversations/${c.id}`, { method: "PATCH", body: { title: t.trim() } }); } catch (e) { toast(e.message); return; }
  if (c.id === S.convId) shell.title.textContent = t.trim();
  loadConvs();
}
async function deleteConv(c) {
  if (!confirmDanger(`「${c.title}」を削除しますか？この操作は取り消せません。`)) return;
  try { await api(`/api/conversations/${c.id}`, { method: "DELETE" }); } catch (e) { toast(e.message); return; }
  if (S.convId === c.id) go("chat"); else loadConvs();
}

// ---------------------------------------------------------------- chat
let chat;
const MODES = [
  ["auto", "自動", "内容と混雑状況から最適なモデルと推論の深さを選びます", "spark"],
  ["fast", "速さ優先", "軽いモデルで素早く答えます", "bolt"],
  ["quality", "品質優先", "混雑していても高性能な設定で待って答えます", "gem"],
];
function newChat() { S.convId = null; S.attach = []; if (S.view === "chat") openChat(null); }

function modePill() {
  const m = MODES.find(([k]) => k === S.mode) || MODES[0];
  const b = h("button", { class: "pill", type: "button", title: "応答モード" }, icon(m[3], 16), h("span", {}, m[1]), icon("chevron", 14));
  b.onclick = (e) => {
    e.stopPropagation();
    popover(b, MODES.map(([k, label, desc, ic]) => ({ icon: ic, label, desc, active: S.mode === k,
      onclick: () => { S.mode = k; store.set("nextai.mode", k); b.replaceWith(modePill()); } })), { up: true });
  };
  return b;
}

async function openChat(convId) {
  S.convId = convId;
  S.activeJob = null;
  const inner = h("div", { class: "thread" });
  const messages = h("div", { class: "messages" }, inner);
  const ta = h("textarea", { rows: 1, placeholder: `${S.info.name || "NextAI"} にメッセージを送信`, "aria-label": "メッセージ" });
  const attached = h("div", { class: "attached" });
  const fileIn = h("input", { type: "file", multiple: true, hidden: true, onchange: () => { uploadAttachments(fileIn.files); fileIn.value = ""; } });
  const sendBtn = h("button", { class: "send", type: "button", title: "送信 (Enter)", "aria-label": "送信", onclick: () => (S.activeJob ? stopGeneration() : send()) }, icon("send", 18));
  const autosize = () => { ta.style.height = "auto"; ta.style.height = Math.min(240, ta.scrollHeight) + "px"; updateSend(); };
  ta.addEventListener("input", autosize);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && S.prefs.enter_send) { e.preventDefault(); if (!S.activeJob) send(); }
  });
  ta.addEventListener("paste", (e) => { const files = [...(e.clipboardData?.files || [])]; if (files.length) { e.preventDefault(); uploadAttachments(files); } });
  const status = h("span", { class: "composer-status" });
  const composer = h("div", { class: "composer" },
    h("div", { class: "composer-box" }, attached, ta,
      h("div", { class: "composer-row" },
        h("button", { class: "icon-btn", type: "button", title: "ファイルを添付", "aria-label": "ファイルを添付", onclick: () => fileIn.click() }, icon("plus")),
        modePill(), status, h("div", { class: "spacer" }), sendBtn)),
    fileIn,
    h("div", { class: "disclaimer" }, "AI の回答は間違っていることがあります。重要な情報は確認してください。"));
  const toBottom = h("button", { class: "to-bottom", type: "button", "aria-label": "最新のメッセージへ", onclick: () => scrollDown(true, true) }, icon("down"));
  const drop = h("div", { class: "dropzone" }, icon("paperclip", 28), h("div", {}, "ここにドロップして添付"));
  const below = h("div", { class: "below" });
  const wrap = h("div", { class: "chat" }, messages, toBottom, h("div", { class: "composer-wrap" }, composer), below, drop);
  let dragDepth = 0;
  wrap.addEventListener("dragenter", (e) => { if ([...e.dataTransfer.types].includes("Files")) { dragDepth++; wrap.classList.add("dragging"); } });
  wrap.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; wrap.classList.remove("dragging"); } });
  wrap.addEventListener("dragover", (e) => e.preventDefault());
  wrap.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; wrap.classList.remove("dragging"); if (e.dataTransfer.files.length) uploadAttachments(e.dataTransfer.files); });
  messages.addEventListener("scroll", () => toBottom.classList.toggle("show", messages.scrollHeight - messages.scrollTop - messages.clientHeight > 240));
  chat = { inner, messages, ta, attached, sendBtn, status, wrap, autosize, below };
  shell.main.replaceChildren(wrap);
  shell.headerRight.replaceChildren(iconBtn("newchat", "新しいチャット (Ctrl+Shift+O)", () => { go("chat"); newChat(); }));
  renderAttached();
  refreshStatus();
  if (!convId) { wrap.classList.add("welcome"); renderEmpty(); ta.focus(); return; }
  try {
    const d = await api(`/api/conversations/${convId}`);
    shell.title.textContent = d.conversation.title;
    shell.title.onclick = () => renameConv(d.conversation);
    inner.replaceChildren(...d.messages.map((m, i) => renderMessage(m, i === d.messages.length - 1)));
    for (const j of d.active_jobs) attachLive(j.id);
    scrollDown(true);
  } catch (e) { inner.replaceChildren(h("div", { class: "empty" }, e.message)); }
  if (!matchMedia("(pointer: coarse)").matches) ta.focus();
}

function updateSend() {
  if (!chat) return;
  const busy = !!S.activeJob;
  chat.sendBtn.replaceChildren(icon(busy ? "stop" : "send", 18));
  chat.sendBtn.title = busy ? "生成を停止 (Esc)" : "送信 (Enter)";
  chat.sendBtn.classList.toggle("busy", busy);
  chat.sendBtn.disabled = !busy && !chat.ta.value.trim();
}

async function stopGeneration() {
  const id = S.activeJob;
  if (!id) return;
  try { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const q = s.queue;
    chat.status.textContent = q.pending ? `混雑中: 待機 ${q.pending}件 (約${fmtSecs(q.est_wait_seconds)})` : (s.load_level !== "NORMAL" ? `サーバー負荷: ${s.load_level}` : "");
  } catch { /* ignore */ }
}

function renderEmpty() {
  const ideas = [
    ["pen", "文章", "丁寧なお礼のメールを書いて"],
    ["code", "コード", "Pythonで CSV を集計するスクリプトを書いて実行して"],
    ["globe", "調べもの", "最新のAIニュースを調べて要約して"],
    ["image", "画像", "夕焼けの海辺のイラストを生成して"],
    ["music", "音楽", "落ち着いたピアノのBGMを作曲して"],
    ["brain", "相談", "週末の旅行プランを一緒に考えて"],
  ];
  const hour = new Date().getHours();
  const greet = hour < 5 ? "こんばんは" : hour < 11 ? "おはようございます" : hour < 18 ? "こんにちは" : "こんばんは";
  chat.inner.replaceChildren(h("div", { class: "empty" },
    h("h1", { class: "greet" }, `${greet}、`, h("span", { class: "nb" }, `${S.user.display_name}さん`)),
    h("p", { class: "muted" }, "今日は何をお手伝いしましょうか？")));
  chat.below.replaceChildren(h("div", { class: "suggest" }, ideas.map(([ic, label, t]) => h("button", { type: "button", onclick: () => { chat.ta.value = t; chat.autosize(); chat.ta.focus(); } },
    h("span", { class: "s-ic" }, icon(ic)), h("span", { class: "s-label" }, label), h("span", { class: "s-text" }, t)))));
}

function scrollDown(force, smooth) {
  const m = chat?.messages;
  if (!m) return;
  if (force || m.scrollHeight - m.scrollTop - m.clientHeight < 160) m.scrollTo({ top: m.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

function assetEl(a) {
  const url = `/api/files/${a.id}/content`;
  if (a.mime?.startsWith("image/")) return h("a", { class: "asset-img", href: url, target: "_blank", rel: "noopener" }, h("img", { src: url, alt: a.name, loading: "lazy" }));
  if (a.mime?.startsWith("audio/")) return h("audio", { controls: true, src: url, preload: "none" });
  if (a.mime?.startsWith("video/")) return h("video", { controls: true, src: url, preload: "none", class: "vid" });
  return h("a", { class: "file-link", href: `${url}?download=1` }, icon("file", 16), a.name);
}

function metaInfo(meta) {
  const p = meta?.profile || {};
  const parts = [p.label, meta?.model_name, ...(meta?.tools || []).map((t) => `🔧 ${t}`), meta?.duration ? `${meta.duration}s` : null].filter(Boolean);
  if (!parts.length) return null;
  return h("span", { class: "meta-info", title: (p.reasons || []).join("\n") }, parts.join(" · "));
}

function mdEl(text) {
  const el = h("div", { class: "md", html: renderMarkdown(text) });
  el.querySelectorAll("button.copy").forEach((b) => b.addEventListener("click", () => copyText(b.closest(".code").querySelector("code").textContent, b)));
  return el;
}

function actionBar(...btns) { return h("div", { class: "msg-actions" }, ...btns); }

function renderMessage(m, isLast = false) {
  if (m.role === "user") {
    const atts = (m.meta?.attachments || []).map((a) => h("span", { class: "att-chip" }, icon(a.mime?.startsWith("image/") ? "image" : "file", 14), a.name));
    const copyB = iconBtn("copy", "コピー", () => copyText(m.content, copyB));
    const edit = m.id ? iconBtn("edit", "編集して再送信", () => editMessage(m)) : null;
    return h("div", { class: "msg user", "data-id": m.id || "" },
      atts.length ? h("div", { class: "atts" }, atts) : null,
      h("div", { class: "bubble" }, m.content),
      actionBar(copyB, edit));
  }
  const assets = (m.meta?.assets || []).map(assetEl);
  const copyB = iconBtn("copy", "コピー", () => copyText(m.content, copyB));
  const regen = isLast ? iconBtn("refresh", "再生成", () => regenerate()) : null;
  return h("div", { class: "msg assistant" }, h("div", { class: "ai-avatar" }, h("img", { src: "icon.svg", alt: "" })),
    h("div", { class: "body" }, mdEl(m.content), assets.length ? h("div", { class: "assets" }, assets) : null,
      actionBar(copyB, regen, metaInfo(m.meta))));
}

function editMessage(m) {
  chat.ta.value = m.content; chat.autosize(); chat.ta.focus();
  chat.editFrom = m.id;
  chat.status.textContent = "メッセージを編集中 (送信すると以降の会話は置き換わります)";
}

async function regenerate() {
  if (!S.convId || S.activeJob) return;
  try {
    const d = await api(`/api/conversations/${S.convId}/regenerate`, { method: "POST", body: { mode: S.mode } });
    const msgs = [...chat.inner.children];
    const lastUser = msgs.map((x) => x.classList.contains("user")).lastIndexOf(true);
    msgs.slice(lastUser + 1).forEach((x) => x.remove());
    attachLive(d.job.id);
    scrollDown(true);
  } catch (e) { toast(e.message); }
}

function renderAttached() {
  chat.attached.replaceChildren(...S.attach.map((f) => h("span", { class: "att-chip" },
    f.mime?.startsWith("image/") ? h("img", { src: `/api/files/${f.id}/content`, alt: "" }) : icon("file", 14), h("span", { class: "n" }, f.name),
    h("button", { class: "x", type: "button", "aria-label": "削除", onclick: () => { S.attach = S.attach.filter((x) => x.id !== f.id); renderAttached(); } }, icon("x", 14)))));
  chat.attached.hidden = !S.attach.length;
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
  if (!text || S.activeJob) return;
  chat.sendBtn.disabled = true;
  const replaceFrom = chat.editFrom;
  try {
    const d = await api(`/api/conversations/${S.convId || "new"}/messages`, { method: "POST",
      body: { content: text, attachments: S.attach.map((a) => a.id), mode: S.mode, replace_from: replaceFrom || null } });
    const isNew = !S.convId;
    chat.wrap.classList.remove("welcome");
    chat.below.replaceChildren();
    if (isNew || chat.inner.querySelector(".empty")) chat.inner.replaceChildren();
    if (replaceFrom) {
      const kids = [...chat.inner.children];
      const at = kids.findIndex((x) => x.dataset.id === replaceFrom);
      if (at >= 0) kids.slice(at).forEach((x) => x.remove());
      chat.editFrom = null;
    }
    chat.inner.querySelectorAll(".msg-actions .icon-btn[title='再生成']").forEach((b) => b.remove());
    chat.inner.append(renderMessage({ id: d.user_message_id, role: "user", content: text, meta: { attachments: S.attach } }));
    chat.ta.value = ""; chat.autosize(); chat.status.textContent = "";
    S.attach = []; renderAttached();
    S.convId = d.conversation_id;
    if (isNew) { history.replaceState(null, "", `#chat/${d.conversation_id}`); shell.title.textContent = text.replace(/\s+/g, " ").slice(0, 40); loadConvs(); }
    attachLive(d.job.id);
    scrollDown(true);
  } catch (e) { toast(e.message); }
  finally { updateSend(); }
}

const PHASE = { plan: "計画中", act: "実行中", verify: "検証中" };
function attachLive(jobId) {
  S.activeJob = jobId; updateSend();
  const statusLine = h("span", { class: "shimmer" }, "考えています…");
  const prog = h("div", { class: "progress", hidden: true }, h("div"));
  const steps = h("div", { class: "steps" });
  const stepsBox = h("details", { class: "activity", hidden: true }, h("summary", {}, icon("chevron", 14), h("span", {}, "実行ログ")), steps);
  const content = h("div", { class: "md typing" });
  const assets = h("div", { class: "assets" });
  const info = h("span", { class: "meta-info" });
  const el = h("div", { class: "msg assistant live", "data-job": jobId }, h("div", { class: "ai-avatar spin" }, h("img", { src: "icon.svg", alt: "" })),
    h("div", { class: "body" }, h("div", { class: "live-status" }, statusLine), prog, stepsBox, content, assets, actionBar(info)));
  chat.inner.append(el);
  let text = "", pending = false, nsteps = 0;
  const paint = () => {
    if (pending) return; pending = true;
    requestAnimationFrame(() => { pending = false; content.innerHTML = renderMarkdown(text); if (text) statusLine.parentElement.hidden = true; scrollDown(); });
  };
  const addStep = (s) => { nsteps++; stepsBox.hidden = false; stepsBox.querySelector("summary span").textContent = `実行ログ (${nsteps})`; steps.append(h("div", { class: "step" }, s)); };
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  S.streams.set(jobId, es);
  const on = (type, fn) => es.addEventListener(type, (ev) => { try { fn(JSON.parse(ev.data)); } catch (e) { console.error(e); } });
  on("profile", (p) => {
    info.textContent = [p.label, p.model_name].filter(Boolean).join(" · ");
    info.title = (p.reasons || []).join("\n");
    if (p.revision > 1) addStep(`⟳ プロファイル再評価: ${(p.reasons || []).slice(-1)[0] || p.label}`);
    if (p.wait_for_quality) addStep("品質優先のため混雑中でも本来の設定で順番を待っています");
  });
  on("queue", (q) => {
    if (q.started) statusLine.textContent = "考えています…";
    else if (q.model_state === "loading") statusLine.textContent = `モデルを準備しています… (待ち順位 ${q.position ?? "-"})`;
    else if (q.position) statusLine.textContent = `順番待ち ${q.position}番目 · 約${fmtSecs(q.eta_seconds || 0)}`;
  });
  on("step", (s) => { statusLine.textContent = `${PHASE[s.phase] || s.phase}… (ステップ ${s.n}/${s.max})`; statusLine.parentElement.hidden = false; });
  on("plan", (p) => addStep("📋 計画:\n" + p.text));
  on("tool_call", (t) => { addStep(`🔧 ${t.name} ${t.args ? t.args.slice(0, 120) : ""}`); statusLine.textContent = `${t.name} を実行中…`; statusLine.parentElement.hidden = false; });
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
    if (S.activeJob === jobId) { S.activeJob = null; updateSend(); }
    content.classList.remove("typing");
    el.querySelector(".ai-avatar").classList.remove("spin");
    if (d.status !== "done") {
      statusLine.parentElement.hidden = false;
      statusLine.className = d.status === "cancelled" ? "muted" : "error";
      statusLine.textContent = d.status === "cancelled" ? "停止しました" : `エラー: ${d.error || ""}`;
      el.querySelector(".msg-actions").append(iconBtn("refresh", "再生成", () => regenerate()));
      return;
    }
    if (S.view === "chat" && S.convId && chat.inner.contains(el)) {
      try {
        const conv = await api(`/api/conversations/${S.convId}`);
        const m = conv.messages.find((x) => x.job_id === jobId && x.role === "assistant");
        if (m) {
          const fresh = renderMessage(m, true);
          if (nsteps) fresh.querySelector(".body").prepend(stepsBox);
          el.replaceWith(fresh);
        }
      } catch { /* keep live view */ }
      loadConvs();
    }
  });
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) { S.streams.delete(jobId); if (S.activeJob === jobId) { S.activeJob = null; updateSend(); } } };
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
    apiKeysCard(),
    h("div", { class: "card" }, h("h3", {}, "ログアウト"),
      h("div", { class: "row" }, h("button", { class: "btn", onclick: () => logout(false) }, "ログアウト"),
        h("button", { class: "btn danger", onclick: () => logout(true) }, "ログアウトしてこの端末の信頼を解除")),
      h("p", { class: "muted small" }, "証明書の警告が出る端末では ", h("a", { href: "/ca.crt" }, "CA証明書"), " をインストールしてください。")));
}

function apiKeysCard() {
  const card = h("div", { class: "card" }, h("h3", {}, "APIキー (OpenAI互換)"), h("p", { class: "muted small" }, "読み込み中…"));
  const render = (d, fresh) => {
    const base = `${location.origin}/v1`;
    const name = h("input", { class: "input", placeholder: "キーの名前 (例: 自作スクリプト)", maxlength: 60 });
    const days = h("select", { class: "input" }, ...[30, 90, 180, 365].filter((n) => n <= d.max_days).map((n) => h("option", { value: n }, `${n}日`)));
    days.value = String(Math.min(90, d.max_days));
    const create = h("button", { class: "btn primary", onclick: async () => {
      try {
        const r = await api("/api/account/api-keys", { method: "POST", body: { name: name.value.trim() || "API", days: Number(days.value) } });
        render(await api("/api/account/api-keys"), r.key);
      } catch (e) { toast(e.message); }
    } }, "新しいキーを発行");
    const copyBtn = (text) => h("button", { class: "btn small", onclick: () => navigator.clipboard?.writeText(text).then(() => toast("コピーしました")) }, "コピー");
    const kids = [h("h3", {}, "APIキー (OpenAI互換)"),
      h("p", { class: "muted small" }, "OpenAI 互換の API として、既存のツールや自作プログラムからこの AI を使えます。model に \"auto\" を指定すると内容に合わせてモデルが自動で選ばれます。GPU の順番待ちと利用上限は Web 画面と共通です。"),
      h("div", { class: "row small" }, h("span", {}, "Base URL: "), h("code", {}, base), copyBtn(base))];
    if (!d.enabled) {
      kids.push(h("p", { class: "muted small" }, "API キーの発行は管理者により無効化されています。"));
    } else {
      if (fresh) {
        const box = h("input", { class: "input", value: fresh, readonly: true, onfocus: (e) => e.target.select() });
        kids.push(h("div", { class: "notice keybox" }, h("div", { class: "small" }, "新しいキーです。この画面を閉じると二度と表示されません。安全な場所に保存してください。"),
          h("div", { class: "row" }, box, copyBtn(fresh))));
      }
      kids.push(h("div", { class: "grid2" }, name, days), h("div", { class: "row" }, h("span", { class: "muted small" }, `有効なキーは${d.max_keys}個まで`), h("div", { class: "spacer" }), create));
    }
    kids.push(h("div", { class: "list" }, d.keys.length ? d.keys.map((k) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", { class: "title" }, k.name),
        h("div", { class: "muted small" }, `作成 ${fmtTime(k.created_at)} · 期限 ${fmtTime(k.expires_at)} · 最終利用 ${fmtTime(k.last_used_at)} · ${{ active: "有効", revoked: "失効済み", expired: "期限切れ" }[k.status]}`)),
      k.status === "active" ? h("button", { class: "btn small danger", onclick: async () => {
        if (!confirmDanger(`APIキー「${k.name}」を失効させますか？このキーを使っているプログラムは使えなくなります。`)) return;
        try { await api(`/api/account/api-keys/${k.id}`, { method: "DELETE" }); render(await api("/api/account/api-keys")); } catch (e) { toast(e.message); }
      } }, "失効") : null))
      : h("p", { class: "muted small" }, "発行済みのキーはありません")));
    card.replaceChildren(...kids);
  };
  api("/api/account/api-keys").then((d) => render(d)).catch((e) => card.replaceChildren(h("h3", {}, "APIキー (OpenAI互換)"), h("p", { class: "error" }, e.message)));
  return card;
}

boot();
