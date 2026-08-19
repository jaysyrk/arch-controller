"use strict";

const POLL_MS = 5000;
const $ = (id) => document.getElementById(id);

let state = null;
let pollTimer = null;
let sliderHeld = false;
let allApps = null;
let appsInit = false;
let pendingSudo = null;
let filesPath = null;
let filesInit = false;
let filesHidden = false;

/* ---------- transport ---------- */

async function api(path, options = {}) {
  const init = {
    method: options.method || "GET",
    credentials: "same-origin",
    headers: { "X-Archctl": "1" },
  };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  if (response.status === 401) {
    showLogin();
    throw new Error("locked");
  }
  if (options.raw) {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  return data;
}

const post = (path, body) => api(path, { method: "POST", body: body || {} });

/* ---------- chrome ---------- */

let toastTimer = null;
function toast(message, kind) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (kind ? ` ${kind}` : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

function showOutput(result) {
  const el = $("output");
  const text = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
  el.textContent = text || `(no output, exit ${result.code})`;
  el.hidden = false;
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function guard(promise, okMessage) {
  try {
    const result = await promise;
    if (okMessage) toast(okMessage, "good");
    return result;
  } catch (error) {
    if (error.message !== "locked") toast(error.message, "bad");
    return null;
  }
}

const bytes = (n) => {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
};

/* ---------- rendering ---------- */

function statTile(label, value, sub, percent) {
  const bar = percent === undefined ? "" :
    `<div class="bar${percent > 90 ? " warn" : ""}"><span style="width:${Math.min(percent, 100)}%"></span></div>`;
  return `<div class="stat">
    <div class="label">${label}</div>
    <div class="value">${value}</div>
    <div class="sub">${sub}</div>${bar}
  </div>`;
}

function render(data) {
  state = data;
  const sys = data.system;
  const caps = data.capabilities;

  $("hostname").textContent = sys.hostname;
  $("subtitle").textContent = `up ${sys.uptime_human} · ${sys.kernel}`;

  const tiles = [
    statTile("CPU", `${sys.cpu_percent}%`, `load ${sys.load[0].toFixed(2)} · ${sys.cpu_count} cores`, sys.cpu_percent),
    statTile("Memory", `${sys.memory.percent}%`, `${bytes(sys.memory.used)} / ${bytes(sys.memory.total)}`, sys.memory.percent),
    statTile("Disk", `${sys.disk.percent}%`, `${bytes(sys.disk.used)} / ${bytes(sys.disk.total)}`, sys.disk.percent),
  ];
  if (sys.battery) {
    tiles.push(statTile("Battery", `${sys.battery.percent}%`, sys.battery.status.toLowerCase(), sys.battery.percent));
  } else if (sys.temperature !== null) {
    tiles.push(statTile("Temp", `${sys.temperature}°C`, "warmest zone"));
  } else {
    tiles.push(statTile("Network", bytes(sys.network.rx_bytes), `${sys.network.interfaces.length} interfaces`));
  }
  $("stats").innerHTML = tiles.join("");

  $("media-card").hidden = !caps.media;
  if (caps.media) {
    $("now-playing").textContent =
      data.media.now_playing || (data.media.status === "playing" ? "playing" : "nothing playing");
  }

  $("volume-card").hidden = !data.volume.available;
  if (data.volume.available && !sliderHeld) {
    $("volume-slider").value = data.volume.percent;
    $("volume-value").textContent = data.volume.muted ? "muted" : `${data.volume.percent}%`;
  }

  $("brightness-card").hidden = !data.brightness.available;
  if (data.brightness.available && !sliderHeld) {
    $("brightness-slider").value = data.brightness.percent;
    $("brightness-value").textContent = `${data.brightness.percent}%`;
  }

  renderSudo(data.sudo);

  // Load once, not on every poll — and not again if the first attempt failed,
  // which would toast an error every five seconds.
  $("apps-card").hidden = !data.apps_enabled;
  if (data.apps_enabled && !appsInit) { appsInit = true; loadApps(); }

  $("files-card").hidden = !data.files_enabled;
  if (data.files_enabled && !filesInit) { filesInit = true; loadFiles(); }

  renderQuickActions(caps);

  $("commands-card").hidden = data.commands.length === 0;
  $("commands").innerHTML = data.commands
    .map((c) => `<button data-command="${c.id}" data-confirm="${c.confirm}">${escapeHtml(c.label)}</button>`)
    .join("");

  $("shell-card").hidden = !data.shell_enabled;
  $("footer").textContent = `arch-controller ${data.version}`;
}

function renderQuickActions(caps) {
  const buttons = [];
  if (caps.screenshot) buttons.push(`<button data-act="screenshot">Screenshot</button>`);
  if (caps.notify) buttons.push(`<button data-act="notify">Send notification</button>`);
  if (caps.clipboard) {
    buttons.push(`<button data-act="clip-get">Read clipboard</button>`);
    buttons.push(`<button data-act="clip-set">Write clipboard</button>`);
  }
  if (caps.open) buttons.push(`<button data-act="open">Open a URL</button>`);
  const container = $("quick-actions");
  container.innerHTML = buttons.join("") ||
    `<p class="muted">No desktop helpers installed — run <code>archctl check</code>.</p>`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}

/* ---------- polling ---------- */

async function refresh() {
  try {
    render(await api("/api/status"));
    showApp();
  } catch (error) {
    if (error.message !== "locked") $("subtitle").textContent = "offline — retrying";
  }
}

function startPolling() {
  stopPolling();
  refresh();
  pollTimer = setInterval(() => {
    if (document.visibilityState === "visible") refresh();
  }, POLL_MS);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
}

function showLogin() {
  stopPolling();
  $("app").hidden = true;
  $("login").hidden = false;
  $("token").focus();
}

/* ---------- events ---------- */

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("login-error");
  error.hidden = true;
  try {
    await post("/api/login", { token: $("token").value.trim() });
    $("token").value = "";
    startPolling();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

$("logout").addEventListener("click", async () => {
  await guard(post("/api/logout"));
  showLogin();
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest(
    "button[data-media],button[data-volume],button[data-power],button[data-act]," +
    "button[data-command],button[data-app],button[data-dir],button[data-file]");
  if (!button) return;
  const set = button.dataset;

  if (set.app) {
    await guard(post("/api/apps/launch", { id: set.app }), `opening ${button.textContent}`);
    return;
  }
  if (set.dir !== undefined) {
    loadFiles(set.dir);
    return;
  }
  if (set.file !== undefined) {
    await guard(post("/api/files/open", { path: set.file }), "opening on the desktop");
    return;
  }

  if (set.media) {
    const result = await guard(post("/api/media", { action: set.media }));
    if (result) render({ ...state, media: result });
  } else if (set.volume) {
    const result = await guard(post("/api/volume", { action: set.volume }));
    if (result) render({ ...state, volume: result });
  } else if (set.power) {
    await doPower(set.power);
  } else if (set.act) {
    await doQuickAction(set.act);
  } else if (set.command) {
    const command = state.commands.find((c) => c.id === set.command);
    if (command.confirm && !confirm(`Run “${command.label}”?`)) return;
    button.disabled = true;
    const result = await guard(post("/api/command", { id: set.command }));
    button.disabled = false;
    if (result) {
      showOutput(result);
      toast(result.ok ? `${command.label} finished` : `${command.label} exited ${result.code}`,
            result.ok ? "good" : "bad");
    }
  }
});

async function doPower(action) {
  const scary = ["reboot", "poweroff", "hibernate"].includes(action);
  if (scary && !confirm(`${action} ${state.system.hostname}?`)) return;
  await guard(post("/api/power", { action, confirm: scary }), `${action} sent`);
}

async function doQuickAction(action) {
  if (action === "screenshot") {
    const response = await guard(api("/api/screenshot", { raw: true }));
    if (!response) return;
    const image = $("shot");
    if (image.src.startsWith("blob:")) URL.revokeObjectURL(image.src);
    image.src = URL.createObjectURL(await response.blob());
    image.hidden = false;
    image.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } else if (action === "notify") {
    const body = prompt("Notification text");
    if (body) await guard(post("/api/notify", { title: "From your phone", body }), "sent");
  } else if (action === "clip-get") {
    const result = await guard(api("/api/clipboard"));
    if (!result) return;
    $("output").textContent = result.text || "(clipboard is empty)";
    $("output").hidden = false;
    if (navigator.clipboard && result.text) {
      navigator.clipboard.writeText(result.text).then(
        () => toast("copied to this phone", "good"), () => {});
    }
  } else if (action === "clip-set") {
    const text = prompt("Text to put on the desktop clipboard");
    if (text !== null) await guard(post("/api/clipboard", { text }), "clipboard updated");
  } else if (action === "open") {
    const url = prompt("URL to open on the desktop", "https://");
    if (url) await guard(post("/api/open", { url }), "opening");
  }
}

function wireSlider(id, path, valueId, suffix) {
  const slider = $(id);
  let pending = null;
  const hold = () => { sliderHeld = true; };
  slider.addEventListener("pointerdown", hold);
  slider.addEventListener("touchstart", hold, { passive: true });
  slider.addEventListener("input", () => {
    sliderHeld = true;
    $(valueId).textContent = `${slider.value}${suffix}`;
    clearTimeout(pending);
    pending = setTimeout(async () => {
      await guard(post(path, { action: "set", value: Number(slider.value) }));
      sliderHeld = false;
    }, 180);
  });
}

wireSlider("volume-slider", "/api/volume", "volume-value", "%");
wireSlider("brightness-slider", "/api/brightness", "brightness-value", "%");

$("load-processes").addEventListener("click", async () => {
  const result = await guard(api("/api/processes?limit=15"));
  if (!result) return;
  $("processes").innerHTML = result.processes
    .map((p) => `<div class="proc"><span class="name">${escapeHtml(p.name)} <span class="muted">${p.pid}</span></span><span class="rss">${bytes(p.rss)}</span></div>`)
    .join("");
});

$("shell-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("shell-input");
  const cmd = input.value.trim();
  if (!cmd) return;
  const result = await guard(post("/api/shell", { cmd }));
  if (result) showOutput(result);
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && pollTimer) refresh();
});

/* ---------- run as root ---------- */

function renderSudo(sudo) {
  $("sudo-card").hidden = !sudo.enabled;
  if (!sudo.enabled) return;
  const badge = $("sudo-state");
  if (!sudo.installed) badge.textContent = "sudo missing";
  else if (sudo.passwordless) badge.textContent = "no password needed";
  else if (sudo.cached) badge.textContent = "unlocked";
  else badge.textContent = "asks for password";
  $("sudo-history-row").hidden = !sudo.cached;
}

async function runSudo(cmd, password) {
  try {
    const result = await post("/api/sudo", password ? { cmd, password } : { cmd });
    $("sudo-auth").hidden = true;
    $("sudo-password").value = "";
    pendingSudo = null;
    showOutput(result);
    toast(result.ok ? "done" : `exited ${result.code}`, result.ok ? "good" : "bad");
    refresh();
  } catch (error) {
    if (error.payload && error.payload.needs_password) {
      pendingSudo = cmd;
      $("sudo-auth").hidden = false;
      $("sudo-password").focus();
      return;
    }
    if (error.message !== "locked") toast(error.message, "bad");
  }
}

$("sudo-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const cmd = $("sudo-input").value.trim();
  if (cmd) runSudo(cmd, null);
});

$("sudo-auth-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const password = $("sudo-password").value;
  if (password && pendingSudo) runSudo(pendingSudo, password);
});

$("sudo-forget").addEventListener("click", async () => {
  await guard(post("/api/sudo/forget"), "password forgotten");
  refresh();
});

/* ---------- app launcher ---------- */

async function loadApps() {
  const result = await guard(api("/api/apps"));
  if (!result) return;
  allApps = result.apps;
  $("apps-count").textContent = `${allApps.length}`;
  renderApps("");
}

function renderApps(query) {
  const needle = query.trim().toLowerCase();
  const matches = needle
    ? allApps.filter((a) => a.name.toLowerCase().includes(needle) || a.comment.toLowerCase().includes(needle))
    : allApps;
  const shown = matches.slice(0, 24);
  const list = $("apps-list");
  if (!matches.length) {
    list.innerHTML = `<p class="muted more">Nothing matches “${escapeHtml(query)}”.</p>`;
    return;
  }
  list.innerHTML =
    shown.map((a) => `<button data-app="${escapeHtml(a.id)}" title="${escapeHtml(a.comment)}">${escapeHtml(a.name)}</button>`).join("") +
    (matches.length > shown.length
      ? `<p class="muted more">${matches.length - shown.length} more — keep typing to narrow it down.</p>`
      : "");
}

$("app-search").addEventListener("input", (event) => {
  if (allApps) renderApps(event.target.value);
});

/* ---------- file browser ---------- */

async function loadFiles(path) {
  const params = new URLSearchParams({ hidden: filesHidden ? "1" : "0" });
  if (path) params.set("path", path);
  const result = await guard(api(`/api/files?${params}`));
  if (!result) return;
  filesPath = result.path;
  $("file-path").textContent = result.path;
  $("files-up").disabled = !result.parent;
  $("files-up").dataset.parent = result.parent || "";
  $("places").innerHTML = result.places
    .map((p) => `<button data-dir="${escapeHtml(p.path)}">${escapeHtml(p.label)}</button>`)
    .join("");
  renderFiles(result);
}

function renderFiles(result) {
  const list = $("file-list");
  if (!result.entries.length) {
    list.innerHTML = `<p class="muted">This folder is empty.</p>`;
    return;
  }
  list.innerHTML = result.entries.map((entry) => {
    const path = escapeHtml(entry.path);
    if (entry.is_dir) {
      return `<div class="file"><span class="icon">📁</span>
        <button class="label" data-dir="${path}">${escapeHtml(entry.name)}</button>
        <span class="size">›</span></div>`;
    }
    const href = `/api/files/download?path=${encodeURIComponent(entry.path)}`;
    return `<div class="file"><span class="icon">📄</span>
      <button class="label" data-file="${path}">${escapeHtml(entry.name)}</button>
      <span class="size">${bytes(entry.size)}</span>
      <a class="get" href="${href}" download>get</a></div>`;
  }).join("") + (result.truncated ? `<p class="muted">Showing the first 500 entries.</p>` : "");
}

$("files-up").addEventListener("click", (event) => {
  const parent = event.currentTarget.dataset.parent;
  if (parent) loadFiles(parent);
});

$("files-hidden").addEventListener("click", (event) => {
  filesHidden = !filesHidden;
  event.currentTarget.textContent = filesHidden ? "Hide hidden" : "Show hidden";
  loadFiles(filesPath);
});

/* ---------- boot ---------- */

(async function boot() {
  const hash = new URLSearchParams(location.hash.slice(1));
  const token = hash.get("t");
  if (token) {
    history.replaceState(null, "", location.pathname);
    try {
      await post("/api/login", { token });
    } catch (error) {
      toast("that login link is stale", "bad");
    }
  }
  try {
    render(await api("/api/status"));
    showApp();
    startPolling();
  } catch (error) {
    showLogin();
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
})();
