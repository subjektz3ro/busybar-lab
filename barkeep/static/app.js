/* barkeep frontend — a pure client of /api/*. The server is the source of
   truth; this file only renders it and relays clicks. */

const $ = (id) => document.getElementById(id);

let selectedApp = sessionStorage.getItem("barkeep.selected") || null;
let activeTab = "logs";
let lastState = null;
let configLoadedFor = null;   // which app the form currently shows
let configDirty = false;      // has the operator typed since it loaded?
let configLoadSeq = 0;        // fences responses from prior selections/reloads
let configEditSeq = 0;        // protects edits made while a request is in flight
let logsSeq = 0;              // discards responses for an app you left

/* ---------- api ---------- */

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  const body = await resp.json().catch(() => ({}));
  if (resp.status === 401) {
    // The server is token-protected and this browser has no cookie yet.
    // Ask once, exchange it for an httpOnly cookie, and retry — the preview
    // panes are <img> tags and cannot carry a header.
    const token = window.prompt("barkeep token:");
    if (token) {
      const opened = await fetch("/api/session", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (opened.ok) return api(path, options);
    }
    throw new Error("authentication required");
  }
  if (!resp.ok) throw new Error(body.error || `${resp.status} on ${path}`);
  return body;
}

const post = (path, body) => api(path, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

/* ---------- rendering ---------- */

function lampClass(app) {
  if (app.crash_looping) return "lamp lamp--bad";
  if (app.status === "running") return "lamp lamp--ok";
  if (app.status === "backoff") return "lamp lamp--amber";
  return "lamp lamp--off";
}

function statusWord(app) {
  if (app.crash_looping) return "crash-looping";
  return app.status;
}

function fmtUptime(s) {
  if (s == null) return "";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function statLine(app) {
  const bits = [statusWord(app)];
  if (app.uptime_s != null) bits.push(`up ${fmtUptime(app.uptime_s)}`);
  if (app.restarts) bits.push(`${app.restarts} restarts`);
  return bits.join(" · ");
}

const lastHTML = new Map();

function swap(box, html) {
  // Rewriting innerHTML every 2s detached the very button you were pressing
  // (a poll landing between mousedown and mouseup swallowed the click) and
  // dropped keyboard focus twice a minute. Uptime is quantized, so identical
  // markup is the common case — skip the write entirely.
  if (lastHTML.get(box.id) === html) return;
  lastHTML.set(box.id, html);
  const focused = box.contains(document.activeElement) ? document.activeElement : null;
  const key = focused
    ? (focused.dataset.fg ?? focused.dataset.toggle ?? focused.dataset.select)
    : null;
  box.innerHTML = html;
  if (key !== null && key !== undefined) {
    const sel = `[data-fg="${CSS.escape(key)}"],[data-toggle="${CSS.escape(key)}"],`
              + `[data-select="${CSS.escape(key)}"]`;
    const again = box.querySelector(sel);
    if (again) again.focus({ preventScroll: true });
  }
}

function render(state) {
  lastState = state;
  $("bar-host").textContent = state.bar_host;
  setLamp($("conn-lamp"), "ok");
  $("conn-label").textContent = "LINK";

  const fgs = state.apps.filter((a) => a.kind === "foreground");
  const cards = fgs.map((a) => `
    <button class="card ${state.foreground === a.name ? "active" : ""}"
            role="radio" aria-checked="${state.foreground === a.name}"
            data-fg="${esc(a.name)}" data-select="${esc(a.name)}">
      <div class="name">${esc(a.name)}</div>
      <div class="desc">${esc(a.description)}</div>
      <div class="stat"><span class="${lampClass(a)}"></span>${statLine(a)}</div>
    </button>`);
  cards.push(`
    <button class="card card--standby ${state.foreground === null ? "active" : ""}"
            role="radio" aria-checked="${state.foreground === null}" data-fg="">
      <div class="name">STANDBY</div>
      <div class="desc">bar falls back to its built-in apps</div>
      <div class="stat">${state.switching ? "switching…" : "&nbsp;"}</div>
    </button>`);
  swap($("foreground-cards"), cards.join(""));

  const bgs = state.apps.filter((a) => a.kind === "background");
  swap($("background-list"), bgs.length ? bgs.map((a) => `
    <div class="row">
      <div class="grow" data-select="${esc(a.name)}" style="cursor:pointer">
        <div class="name">${esc(a.name)}</div>
        <div class="desc">${esc(a.description)}</div>
        <div class="stat"><span class="${lampClass(a)}"></span>${statLine(a)}</div>
      </div>
      <button class="switch" role="switch"
              aria-checked="${a.status !== "stopped"}"
              aria-label="toggle ${esc(a.name)}"
              data-toggle="${esc(a.name)}" data-enabled="${a.status !== "stopped"}"></button>
    </div>`).join("")
    : `<p class="ghost-note">no background apps registered yet — add one to apps.toml</p>`);

  if (!selectedApp || !state.apps.some((a) => a.name === selectedApp)) {
    selectedApp = state.foreground || (state.apps[0] && state.apps[0].name) || null;
  }
  renderInspector(state);
}

function setLamp(el, kind) {
  el.className = `lamp lamp--${kind}`;
}

function renderInspector(state) {
  const insp = $("inspector");
  const app = state.apps.find((a) => a.name === selectedApp);
  if (!app) { insp.hidden = true; return; }
  insp.hidden = false;
  $("insp-name").textContent = app.name;
  const chip = $("insp-status");
  chip.textContent = statusWord(app);
  chip.className = "status-chip " +
    (app.crash_looping ? "crash" : app.status === "running" ? "running"
      : app.status === "backoff" ? "backoff" : "");
  const meta = [];
  if (app.uptime_s != null) meta.push(`up ${fmtUptime(app.uptime_s)}`);
  if (app.pid) meta.push(`pid ${app.pid}`);
  if (app.restarts) meta.push(`${app.restarts} restarts`);
  meta.push(app.kind);
  $("insp-meta").textContent = meta.join(" · ");
}

/* ---------- previews ---------- */

function pollPreviews() {
  let anyOk = false, done = 0;
  for (const [id, display] of [["preview-front", 0], ["preview-back", 1]]) {
    const img = $(id);
    const probe = new Image();
    const settle = (ok) => {
      img.closest(".glass").classList.toggle("offline", !ok);
      anyOk = anyOk || ok;
      if (++done === 2) {
        setLamp($("live-lamp"), anyOk ? "ok" : "off");
        $("live-label").textContent = anyOk ? "LIVE" : "FEED";
        $("device-note").textContent = anyOk ? "" : "bar unreachable — previews paused";
      }
    };
    probe.onload = () => { img.src = probe.src; settle(true); };
    probe.onerror = () => settle(false);
    probe.src = `/api/preview/${display}?t=${Date.now()}`;
  }
}

/* ---------- logs ---------- */

async function pollLogs() {
  if (!selectedApp || activeTab !== "logs" || $("inspector").hidden) return;
  // A response that arrives after you switch apps must not paint one app's
  // output under another's header.
  const app = selectedApp, seq = ++logsSeq;
  try {
    const body = await api(`/api/apps/${encodeURIComponent(app)}/logs?lines=300`);
    if (seq !== logsSeq || app !== selectedApp || activeTab !== "logs") return;
    const pane = $("log-pane");
    const stick = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 8;
    pane.replaceChildren(...body.lines.map((l) => {
      const div = document.createElement("div");
      if (l.startsWith("[barkeep]")) div.className = "sys";
      div.textContent = l;
      return div;
    }));
    if (!body.lines.length) pane.textContent = "(no output yet)";
    if (stick) pane.scrollTop = pane.scrollHeight;
  } catch (e) {
    if (seq !== logsSeq || app !== selectedApp) return;
    $("log-pane").textContent = `logs unavailable: ${e.message}`;
  }
}

/* ---------- config ---------- */

function numericBounds(k) {
  // Browser constraints are editing hints only; the PUT route remains the
  // authoritative validator for direct API callers and tampered responses.
  if (k.type !== "number") return "";
  const minimum = typeof k.minimum === "number" && Number.isFinite(k.minimum)
    ? ` min="${escapeAttr(k.minimum)}"` : "";
  const maximum = typeof k.maximum === "number" && Number.isFinite(k.maximum)
    ? ` max="${escapeAttr(k.maximum)}"` : "";
  return minimum + maximum;
}

function fieldControl(k) {
  // data-initial is the baseline saveConfig() diffs against, stored on the
  // control itself so it survives every re-render. Without it a save would
  // promote every inherited value into a per-app override, permanently
  // shadowing the shared .env.
  if (k.type === "multiselect") {
    const on = new Set((k.value || k.default).split(",").map((s) => s.trim()).filter(Boolean));
    const canonical = k.choices.filter((c) => on.has(c)).join(",");
    return `<div class="chips" data-key="${escapeAttr(k.name)}"
                 data-value="${escapeAttr(canonical)}"
                 data-initial="${escapeAttr(canonical)}"
                 data-choices="${escapeAttr(k.choices.join(","))}">
      ${k.choices.map((c) => `
        <button type="button" role="checkbox" aria-checked="${on.has(c)}"
                class="chip ${on.has(c) ? "on" : ""}" data-pick="${escapeAttr(c)}">
          ${esc(c)}</button>`).join("")}
    </div>`;
  }
  if (k.choices && k.choices.length) {
    const current = k.value || k.default;
    return `<div class="seg" role="radiogroup" aria-label="${escapeAttr(k.name)}"
                 data-key="${escapeAttr(k.name)}" data-value="${escapeAttr(current)}"
                 data-initial="${escapeAttr(current)}">
      ${k.choices.map((c) => `
        <button type="button" role="radio" aria-checked="${c === current}"
                class="${c === current ? "on" : ""}" data-choice="${escapeAttr(c)}">
          ${esc(c)}</button>`).join("")}
    </div>`;
  }
  const type = k.type === "number" ? "number" : k.type === "email" ? "email" : "text";
  const step = k.type === "number" ? ` step="any"` : "";
  const bounds = numericBounds(k);
  return `<input id="cfg-${escapeAttr(k.name)}" name="${escapeAttr(k.name)}"
                 type="${type}"${step}${bounds}
                 value="${escapeAttr(k.value)}" data-initial="${escapeAttr(k.value)}"
                 placeholder="${escapeAttr(k.default)}" autocomplete="off">`;
}

async function loadConfig() {
  if (!selectedApp) return;
  const app = selectedApp, seq = ++configLoadSeq, edits = configEditSeq;
  // Re-clicking the CONFIG tab or the app you're already on used to silently
  // wipe everything typed so far.
  if (configDirty && configLoadedFor === selectedApp) return;
  if (configDirty && configLoadedFor !== selectedApp) toast("unsaved config discarded");
  if (configLoadedFor !== app) {
    configLoadedFor = null;
    configDirty = false;
    $("config-form").textContent = "loading config…";
  }
  const current = () => seq === configLoadSeq && app === selectedApp
    && edits === configEditSeq;
  try {
    const body = await api(`/api/apps/${encodeURIComponent(app)}/config`);
    if (!current()) return;
    $("config-status").textContent = "";
    $("config-form").innerHTML = body.keys.map((k) => `
      <div class="cfg-field">
        <label for="cfg-${esc(k.name)}">${esc(k.name)}
          <span class="hint">${esc(k.description)} · <span class="src">${esc(k.source)}</span></span></label>
        ${fieldControl(k)}
      </div>`).join("")
      || `<p class="ghost-note">this app declares no config keys</p>`;
    configLoadedFor = app;
    configDirty = false;
  } catch (e) {
    if (!current()) return;
    const p = document.createElement("p");
    p.className = "ghost-note";
    p.textContent = `config unavailable: ${e.message}`;
    $("config-form").replaceChildren(p);
  }
}

// Everything from the server lands in innerHTML: app names and descriptions
// come from apps.toml, config values from files this UI itself writes. An
// ordinary "<" in a description would silently eat the rest of the row.
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
const escapeAttr = esc;

function markConfigEdited() {
  ++configEditSeq;
  refreshConfigDirty();
}

function refreshConfigDirty() {
  const form = $("config-form");
  configDirty = [...form.querySelectorAll("input")]
    .some(input => input.value !== input.dataset.initial
      || (input.validity && input.validity.badInput))
    || [...form.querySelectorAll(".seg"), ...form.querySelectorAll(".chips")]
      .some(control => control.dataset.value !== control.dataset.initial);
}

function acceptSavedBaseline(keys, submitted) {
  // Edits made during the save stay on screen, but must now be compared with
  // what the server saved. In particular, reverting to the *old* baseline
  // during an in-flight save is still an unsaved change.
  const saved = new Map(keys.map(key => [key.name, key.value]));
  const form = $("config-form");
  for (const input of form.querySelectorAll("input")) {
    if (!Object.hasOwn(submitted, input.name) || !saved.has(input.name)) continue;
    const value = saved.get(input.name);
    if (input.value === submitted[input.name]) input.value = value;
    input.dataset.initial = value;
  }
  for (const control of [...form.querySelectorAll(".seg"), ...form.querySelectorAll(".chips")]) {
    const key = control.dataset.key;
    if (Object.hasOwn(submitted, key) && saved.has(key))
      control.dataset.initial = saved.get(key);
  }
  refreshConfigDirty();
}

async function saveConfig(restart) {
  const app = selectedApp, edits = configEditSeq, loaded = configLoadSeq;
  if (!app || configLoadedFor !== app) {
    $("config-status").textContent = "wait for this app's config to load";
    return;
  }
  const current = () => app === selectedApp && loaded === configLoadSeq
    && edits === configEditSeq;
  const values = {};
  for (const input of $("config-form").querySelectorAll("input")) {
    if (input.validity && input.validity.badInput) {
      // The browser can't parse it, so it would submit as "" — which reads as
      // "clear this key" and would restart the app on the inherited value
      // while the toast claimed success.
      $("config-status").textContent = `${input.name}: not a valid ${input.type}`;
      input.focus();
      return;
    }
    if (input.value !== input.dataset.initial) values[input.name] = input.value;
  }
  for (const seg of $("config-form").querySelectorAll(".seg")) {
    if (seg.dataset.value !== seg.dataset.initial)
      values[seg.dataset.key] = seg.dataset.value;
  }
  for (const chips of $("config-form").querySelectorAll(".chips")) {
    if (!chips.dataset.value) {
      // The server rejects this too; catching it here saves a round trip
      // and points at the offending field.
      $("config-status").textContent = `${chips.dataset.key}: select at least one`;
      return;
    }
    if (chips.dataset.value !== chips.dataset.initial)
      values[chips.dataset.key] = chips.dataset.value;
  }
  if (!Object.keys(values).length) {
    $("config-status").textContent = "no changes";
    // An empty diff means the form matches its baseline, so there is nothing
    // left to protect — leaving the flag set would make loadConfig refuse to
    // ever refresh this app again.
    configDirty = false;
    if (restart) {
      try {
        await post(`/api/apps/${encodeURIComponent(app)}/restart`);
        toast(`${app} restarting`);
        await pollState();
      } catch (e) {
        if (current()) $("config-status").textContent = e.message;
      }
    }
    return;
  }
  try {
    const saved = await api(`/api/apps/${encodeURIComponent(app)}/config`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ values }),
    });
    if (app === selectedApp && loaded === configLoadSeq)
      acceptSavedBaseline(saved.keys, values);
    if (restart) await post(`/api/apps/${encodeURIComponent(app)}/restart`);
    toast(restart ? `${app} config saved — restarting` : `${app} config saved`);
    if (current()) {
      configDirty = false;    // before reloading the saved form's source badges
      await loadConfig();
    }
    await pollState();
  } catch (e) {
    if (current()) $("config-status").textContent = e.message;
  }
}

/* ---------- tls admin ---------- */

function tlsSummary(body) {
  const words = {
    off: "plain HTTP — no certificate configured",
    generated: "self-signed certificate, generated by barkeep",
    uploaded: "uploaded certificate",
    env: "certificate pinned by BARKEEP_TLS_CERT/KEY in .env — manage it there",
  };
  const bits = [words[body.source] || `configuration error: ${body.detail}`];
  if (body.cert && body.cert.not_after) bits.push(`expires ${body.cert.not_after}`);
  if (body.cert) bits.push(`SHA-256 ${body.cert.fingerprint_sha256}`);
  if (body.restart_required) bits.push("RESTART BARKEEP TO SERVE THE CHANGE");
  if (body.managed && !body.upload_allowed) {
    bits.push("OPEN OVER HTTPS OR A LOOPBACK/SSH TUNNEL TO INSTALL A PRIVATE KEY");
  }
  return bits.join(" · ");
}

async function loadTls() {
  try {
    const body = await api("/api/tls");
    $("tls-summary").textContent = tlsSummary(body);
    $("tls-editor").hidden = !body.managed || !body.upload_allowed;
    $("tls-revert").hidden = body.source !== "uploaded";
  } catch (e) {
    $("tls-summary").textContent = `unavailable: ${e.message}`;
    $("tls-editor").hidden = true;
  }
}

$("tls-install").addEventListener("click", async () => {
  const cert = $("tls-cert").value.trim();
  const key = $("tls-key").value.trim();
  if (!cert || !key) {
    $("tls-note").textContent = "paste both the certificate and its key";
    return;
  }
  try {
    await api("/api/tls", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ certificate_pem: cert, key_pem: key }),
    });
    // The pair is staged on the host now; never keep a private key in the DOM.
    $("tls-cert").value = "";
    $("tls-key").value = "";
    $("tls-note").textContent = "";
    toast("certificate installed — restart barkeep to serve it");
    await loadTls();
  } catch (e) {
    $("tls-note").textContent = e.message;
  }
});

$("tls-revert").addEventListener("click", async () => {
  try {
    await api("/api/tls", {
      method: "DELETE",
      headers: { "content-type": "application/json" },
    });
    toast("uploaded certificate removed — restart barkeep to apply");
    await loadTls();
  } catch (e) {
    $("tls-note").textContent = e.message;
  }
});

/* ---------- selection / tabs ---------- */

function selectApp(name) {
  selectedApp = name;
  sessionStorage.setItem("barkeep.selected", name);
  if (lastState) renderInspector(lastState);
  if (activeTab === "config") loadConfig(); else pollLogs();
}

function setTab(tab) {
  // No same-tab guard here: re-clicking CONFIG is the natural retry after a
  // failed load. Unsaved edits are already protected by loadConfig's dirty
  // check, which is the right place for it.
  activeTab = tab;
  for (const t of document.querySelectorAll(".tab")) {
    const on = t.dataset.tab === tab;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  }
  $("tab-logs").hidden = tab !== "logs";
  $("tab-config").hidden = tab !== "config";
  if (tab === "config") loadConfig(); else pollLogs();
}

/* ---------- state ---------- */

async function pollState() {
  try {
    render(await api("/api/state"));
  } catch {
    setLamp($("conn-lamp"), "off");
    $("conn-label").textContent = "NO LINK";
  }
}

/* ---------- events ---------- */

document.body.addEventListener("click", async (ev) => {
  const pick = ev.target.closest(".chips [data-pick]");
  if (pick) {
    const chips = pick.closest(".chips");
    const on = new Set(chips.dataset.value.split(",").filter(Boolean));
    on.has(pick.dataset.pick) ? on.delete(pick.dataset.pick)
                              : on.add(pick.dataset.pick);
    // Store in declared order so the value matches what the server would
    // canonicalize it to — otherwise the dirty-diff fires on click order.
    chips.dataset.value = chips.dataset.choices.split(",")
      .filter((c) => on.has(c)).join(",");
    pick.classList.toggle("on", on.has(pick.dataset.pick));
    pick.setAttribute("aria-checked", on.has(pick.dataset.pick));
    markConfigEdited();
    $("config-status").textContent = chips.dataset.value
      ? "" : `${chips.dataset.key}: select at least one`;
    return;
  }
  const choice = ev.target.closest(".seg [data-choice]");
  if (choice) {
    const seg = choice.closest(".seg");
    // Recompute the whole form: returning this choice to its baseline must
    // not clear an edit in another field.
    seg.dataset.value = choice.dataset.choice;
    markConfigEdited();
    for (const b of seg.querySelectorAll("[data-choice]")) {
      const on = b === choice;
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", on);
    }
    return;
  }
  const t = ev.target.closest("[data-fg], [data-toggle], [data-select], .tab");
  if (!t) return;
  try {
    if (t.classList.contains("tab")) {
      setTab(t.dataset.tab);
      return;
    }
    if (t.dataset.toggle !== undefined) {
      const verb = t.dataset.enabled === "true" ? "disable" : "enable";
      await post(`/api/apps/${t.dataset.toggle}/${verb}`);
      toast(`${t.dataset.toggle} ${verb}d`);
      selectApp(t.dataset.toggle);
      await pollState();
      return;
    }
    if (t.dataset.fg !== undefined) {
      const name = t.dataset.fg || null;
      await post("/api/foreground", { app: name });
      toast(name ? `${name} takes the display` : "bar on standby");
      if (name) selectApp(name);
      await pollState();
      return;
    }
    if (t.dataset.select !== undefined) {
      selectApp(t.dataset.select);
      $("inspector").scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  } catch (e) {
    toast(`error: ${e.message}`);
  }
});

$("insp-restart").addEventListener("click", async () => {
  if (!selectedApp) return;
  try {
    await post(`/api/apps/${selectedApp}/restart`);
    toast(`${selectedApp} restarting`);
    await pollState();
  } catch (e) {
    toast(`error: ${e.message}`);
  }
});

// Delegated on the form itself: per-input listeners would be lost on rebuild.
$("config-form").addEventListener("input", markConfigEdited);

$("config-save").addEventListener("click", () => saveConfig(false));
$("config-save-restart").addEventListener("click", () => saveConfig(true));

/* ---------- go ---------- */

pollState();
pollPreviews();
loadTls();
setTab("logs");
setInterval(pollState, 2000);
setInterval(pollPreviews, 5000);
setInterval(pollLogs, 2000);
