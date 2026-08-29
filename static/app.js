"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, options = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const isJson = (res.headers.get("content-type") || "").includes("json");
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.error) || `Request failed (${res.status})`);
  return body;
};

let CATALOG = null;
let EDITING = null;      // agent id being edited, or null for a new agent
let CALL_AGENT = null;   // agent selected on the Call view
let EVENT_SOURCE = null;

/* ------------------------------------------------------------------ */
/* chrome                                                              */
/* ------------------------------------------------------------------ */
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  setTimeout(() => (el.className = "toast"), 3200);
}

/* The builder and the call screen belong to one agent, so the sidebar swaps
   from the product nav to that agent's own sections while they are open. */
const AGENT_VIEWS = new Set(["builder", "call"]);
const CRUMB_FOR_VIEW = { agents: "Agents", credentials: "Settings" };

function show(view) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("view-" + view).classList.add("active");

  const inAgent = AGENT_VIEWS.has(view);
  $("root-nav").classList.toggle("hidden", inAgent);
  $("agent-nav").classList.toggle("hidden", !inAgent);

  // Settings lives in the pinned sidebar footer, outside #root-nav, so the
  // lit state is driven off every nav item that names a view.
  document.querySelectorAll(".nav-item[data-view]").forEach((t) =>
    t.classList.toggle("active", !inAgent && t.dataset.view === view)
  );
  if (!inAgent) $("crumb").textContent = CRUMB_FOR_VIEW[view] || "";

  if (view === "credentials") loadCredentials();
}

document.querySelectorAll(".nav-item[data-view]").forEach((tab) =>
  tab.addEventListener("click", () => show(tab.dataset.view))
);

/* Sections within one agent's workspace (Prompt / Speech / Turn taking /
   Conversations) — the same screen, so they switch panes rather than views. */
function showPane(pane) {
  if (!$("view-builder").classList.contains("active")) show("builder");
  document.querySelectorAll(".pane-view").forEach((p) =>
    p.classList.toggle("active", p.dataset.pane === pane)
  );
  document.querySelectorAll("#agent-nav .nav-item").forEach((t) =>
    t.classList.toggle("active", t.dataset.pane === pane)
  );
  $("crumb").textContent =
    `Agents / ${$("agent-nav-name").textContent} / ${paneLabel(pane)}`;
  if (pane === "conversations") loadAgentCalls();
}

function paneLabel(pane) {
  const item = document.querySelector(`#agent-nav .nav-item[data-pane="${pane}"]`);
  return item ? item.querySelector("span").textContent : "";
}

document.querySelectorAll("#agent-nav .nav-item").forEach((tab) =>
  tab.addEventListener("click", () => showPane(tab.dataset.pane))
);

$("back-to-agents").onclick = () => { show("agents"); loadAgents(); };

/* ------------------------------------------------------------------ */
/* theme                                                               */
/* ------------------------------------------------------------------ */
function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = theme;
  // The switch shows the theme that is on, not the one it would move to.
  $("theme-icon").textContent = dark ? "☾" : "☀";
  $("theme-label").textContent = dark ? "Dark" : "Light";
  $("theme-toggle").setAttribute("aria-checked", String(dark));
  try { localStorage.setItem("mv-theme", theme); } catch (e) { /* private mode */ }
}

$("theme-toggle").onclick = () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

applyTheme(document.documentElement.dataset.theme === "dark" ? "dark" : "light");

/* ------------------------------------------------------------------ */
/* catalog + builder wiring                                            */
/* ------------------------------------------------------------------ */
function fillSelect(select, items, valueKey, labelKey, selected) {
  select.innerHTML = "";
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = typeof item === "string" ? item : item[valueKey];
    option.textContent = typeof item === "string" ? item : item[labelKey];
    select.appendChild(option);
  });
  if (selected !== undefined && selected !== null) select.value = selected;
}

function entryFor(kind, provider) {
  return CATALOG[kind].find((e) => e.provider === provider);
}

function refreshSttModels(selected) {
  const entry = entryFor("stt", $("f-stt-provider").value);
  fillSelect($("f-stt-model"), entry ? entry.models : [], null, null, selected);
  $("stt-note").textContent = entry ? entry.note : "";
}

function refreshLlmModels(selected) {
  const entry = entryFor("llm", $("f-llm-provider").value);
  fillSelect($("f-llm-model"), entry ? entry.models : [], null, null, selected);
  $("llm-note").textContent = entry ? entry.note : "";
}

function refreshVoices(selected) {
  const entry = entryFor("tts", $("f-tts-provider").value);
  fillSelect($("f-tts-voice"), entry ? entry.voices : [], "id", "name", selected);
  $("tts-note").textContent = entry ? entry.note : "";
}

function refreshPromptVars() {
  const matches = ($("f-prompt").value.match(/\$\{?([a-zA-Z_][a-zA-Z0-9_]*)\}?/g) || [])
    .map((m) => m.replace(/[${}]/g, ""));
  const unique = [...new Set(matches)];
  $("prompt-vars").textContent = unique.length ? unique.join(", ") : "none";
}

async function loadCatalog() {
  CATALOG = await api("/api/catalog");

  fillSelect($("f-stt-provider"), CATALOG.stt, "provider", "label");
  fillSelect($("f-llm-provider"), CATALOG.llm, "provider", "label");
  fillSelect($("f-tts-provider"), CATALOG.tts, "provider", "label");
  fillSelect($("f-language"), CATALOG.languages, "code", "name");
  fillSelect($("f-allowed-languages"), CATALOG.languages, "code", "name");

  const starter = $("f-starter");
  starter.innerHTML = '<option value="">— template —</option>';
  CATALOG.starters.forEach((s) => {
    const option = document.createElement("option");
    option.value = s.id;
    option.textContent = s.name;
    starter.appendChild(option);
  });

  refreshSttModels();
  refreshLlmModels();
  refreshVoices();
}

$("f-stt-provider").addEventListener("change", () => refreshSttModels());
$("f-llm-provider").addEventListener("change", () => refreshLlmModels());
$("f-tts-provider").addEventListener("change", () => refreshVoices());
$("f-prompt").addEventListener("input", refreshPromptVars);
$("f-filler-delay").addEventListener("input", (e) => {
  $("filler-delay-label").textContent = e.target.value;
});
$("f-starter").addEventListener("change", (e) => {
  const starter = CATALOG.starters.find((s) => s.id === e.target.value);
  if (starter) {
    $("f-prompt").value = starter.body;
    /* The opening line is part of the template. Sending it as a fixed string
       rather than asking the LLM for it removes ~1.8s of dead air on connect,
       which is long enough that the callee says "hello?" into the silence. */
    if (starter.greeting) {
      $("f-greeting-mode").value = "static";
      $("f-greeting-text").value = starter.greeting;
    }
    refreshPromptVars();
  }
});

/* ------------------------------------------------------------------ */
/* agents                                                              */
/* ------------------------------------------------------------------ */
let AGENTS = [];

async function loadAgents() {
  AGENTS = (await api("/api/agents")).agents;
  renderAgents();
}

function renderAgents() {
  const query = ($("agent-search").value || "").trim().toLowerCase();
  const agents = query
    ? AGENTS.filter((a) => (a.name || "").toLowerCase().includes(query))
    : AGENTS;

  const list = $("agent-list");
  list.innerHTML = "";
  const empty = $("agents-empty");
  empty.classList.toggle("hidden", agents.length > 0);
  if (!agents.length) {
    empty.textContent = AGENTS.length
      ? `No agent matches "${$("agent-search").value.trim()}".`
      : "No agents yet. Create one to get started — you'll pick the speech, " +
        "language and voice models, then write what the agent should say.";
  }

  agents.forEach((a) => {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-head">
        <img class="logo-mark" src="/static/images/vaani_logo.jpg" alt="">
        <div>
          <h4></h4>
          <p class="meta-line">${a.llm_provider} · ${a.tts_voice}</p>
        </div>
      </div>
      <div class="stack">
        <span class="chip">${a.stt_provider}</span>
        <span class="chip">${a.llm_model}</span>
        <span class="chip">${a.tts_provider} · ${a.tts_voice}</span>
        <span class="chip">${a.language_mode === "auto" ? "auto-detect" : a.language}</span>
      </div>
      <div class="actions">
        <button class="btn primary small" data-act="open">Open</button>
        <button class="btn ghost small" data-act="call">Call</button>
        <button class="btn ghost small" data-act="logs">Logs</button>
        <button class="btn ghost small" data-act="delete">Delete</button>
      </div>`;
    card.querySelector("h4").textContent = a.name;
    card.querySelector("h4").onclick = () => openBuilder(a.id);
    card.querySelector('[data-act="call"]').onclick = () => openCall(a.id);
    card.querySelector('[data-act="open"]').onclick = () => openBuilder(a.id);
    card.querySelector('[data-act="logs"]').onclick = () =>
      openBuilder(a.id, "conversations");
    card.querySelector('[data-act="delete"]').onclick = async () => {
      if (!confirm(`Delete "${a.name}"?`)) return;
      await api(`/api/agents/${a.id}`, { method: "DELETE" });
      toast("Agent deleted");
      loadAgents();
    };
    list.appendChild(card);
  });
}

$("agent-search").addEventListener("input", renderAgents);

function formToAgent() {
  return {
    name: $("f-name").value.trim() || "Untitled agent",
    stt_provider: $("f-stt-provider").value,
    stt_model: $("f-stt-model").value,
    language_mode: $("f-language-mode").value,
    language: $("f-language").value,
    allowed_languages: selectedValues("f-allowed-languages").join(","),
    language_switch_turns: parseInt($("f-lang-switch-turns").value, 10) || 2,
    language_switch_min_seconds: parseFloat($("f-lang-switch-secs").value) || 1.0,
    llm_provider: $("f-llm-provider").value,
    llm_model: $("f-llm-model").value,
    temperature: parseFloat($("f-temperature").value) || 0.4,
    max_output_tokens: parseInt($("f-max-tokens").value, 10) || 220,
    tts_provider: $("f-tts-provider").value,
    tts_voice: $("f-tts-voice").value,
    tts_speaking_rate: numOr("f-tts-rate", 0.95),
    tts_pitch: numOr("f-tts-pitch", 0),
    tts_pause_ms: numOr("f-tts-pause", 350, true),
    system_prompt: $("f-prompt").value,
    greeting_mode: $("f-greeting-mode").value,
    greeting_text: $("f-greeting-text").value,
    fillers_enabled: $("f-fillers").checked,
    filler_delay_ms: parseInt($("f-filler-delay").value, 10) || 350,
    silence_threshold_rms: parseInt($("f-rms").value, 10) || 300,
    silence_end_seconds: parseFloat($("f-silence").value) || 0.8,
    min_utterance_seconds: numOr("f-min-utterance", 0.4),
    noise_margin: numOr("f-noise-margin", 2.0),
    barge_in_seconds: numOr("f-barge", 0.5),
    barge_in_grace_seconds: numOr("f-barge-grace", 0.7),
    no_reply_seconds: numOr("f-no-reply", 6.0),
    no_reply_prompts: numOr("f-no-reply-prompts", 2, true),
    redirect_number: $("f-redirect").value.trim(),
  };
}

// `parseFloat(x) || fallback` silently rewrites a deliberate 0 — which is a
// real setting for the grace period and the pause length.
function numOr(id, fallback, integer = false) {
  const raw = (integer ? parseInt($(id).value, 10) : parseFloat($(id).value));
  return Number.isFinite(raw) ? raw : fallback;
}

function selectedValues(id) {
  return [...$(id).selectedOptions].map((o) => o.value);
}

function setSelectedValues(id, csv) {
  const wanted = new Set((csv || "").split(",").map((s) => s.trim()).filter(Boolean));
  [...$(id).options].forEach((o) => { o.selected = wanted.has(o.value); });
}

function agentToForm(a) {
  $("f-name").value = a.name || "";
  $("f-stt-provider").value = a.stt_provider;
  refreshSttModels(a.stt_model);
  $("f-language-mode").value = a.language_mode;
  $("f-language").value = a.language;
  setSelectedValues("f-allowed-languages", a.allowed_languages);
  $("f-lang-switch-turns").value = a.language_switch_turns;
  $("f-lang-switch-secs").value = a.language_switch_min_seconds;
  $("f-llm-provider").value = a.llm_provider;
  refreshLlmModels(a.llm_model);
  $("f-temperature").value = a.temperature;
  $("f-max-tokens").value = a.max_output_tokens;
  $("f-tts-provider").value = a.tts_provider;
  refreshVoices(a.tts_voice);
  $("f-tts-rate").value = a.tts_speaking_rate;
  $("f-tts-pitch").value = a.tts_pitch;
  $("f-tts-pause").value = a.tts_pause_ms;
  $("f-prompt").value = a.system_prompt || "";
  $("f-greeting-mode").value = a.greeting_mode;
  $("f-greeting-text").value = a.greeting_text || "";
  $("f-fillers").checked = !!a.fillers_enabled;
  $("f-filler-delay").value = a.filler_delay_ms;
  $("filler-delay-label").textContent = a.filler_delay_ms;
  $("f-rms").value = a.silence_threshold_rms;
  $("f-silence").value = a.silence_end_seconds;
  $("f-min-utterance").value = a.min_utterance_seconds;
  $("f-noise-margin").value = a.noise_margin;
  $("f-barge").value = a.barge_in_seconds;
  $("f-barge-grace").value = a.barge_in_grace_seconds;
  $("f-no-reply").value = a.no_reply_seconds;
  $("f-no-reply-prompts").value = a.no_reply_prompts;
  $("f-redirect").value = a.redirect_number || "";
  refreshPromptVars();
}

const DEFAULT_AGENT = {
  name: "", stt_provider: "sarvam", stt_model: "saarika:v2.5",
  language_mode: "auto", language: "hi-IN",
  allowed_languages: "", language_switch_turns: 2, language_switch_min_seconds: 1.0,
  llm_provider: "gemini", llm_model: "gemini-2.5-flash-lite",
  temperature: 0.4, max_output_tokens: 220,
  tts_provider: "sarvam", tts_voice: "anushka",
  tts_speaking_rate: 0.95, tts_pitch: 0, tts_pause_ms: 350,
  system_prompt: "", greeting_mode: "llm", greeting_text: "",
  fillers_enabled: true, filler_delay_ms: 350,
  silence_threshold_rms: 300, silence_end_seconds: 0.8, min_utterance_seconds: 0.4,
  noise_margin: 2.0, barge_in_seconds: 0.5, barge_in_grace_seconds: 0.7,
  no_reply_seconds: 6.0, no_reply_prompts: 2,
  redirect_number: "",
};

async function openBuilder(agentId, pane = "prompt") {
  EDITING = agentId || null;
  if (agentId) {
    const agent = await api(`/api/agents/${agentId}`);
    $("builder-title").textContent = agent.name;
    $("builder-subtitle").textContent =
      `${agent.llm_provider} · ${agent.tts_provider} ${agent.tts_voice} · ` +
      (agent.language_mode === "auto" ? "auto-detect" : agent.language);
    $("agent-nav-name").textContent = agent.name;
    agentToForm(agent);
  } else {
    $("builder-title").textContent = "New agent";
    $("builder-subtitle").textContent =
      "Configure how this agent speaks and what it says, then save it.";
    $("agent-nav-name").textContent = "New agent";
    agentToForm({
      ...DEFAULT_AGENT,
      system_prompt: CATALOG.starters[0].body,
      greeting_mode: CATALOG.starters[0].greeting ? "static" : "llm",
      greeting_text: CATALOG.starters[0].greeting || "",
    });
  }
  // An unsaved agent has no call log and cannot be dialled yet.
  $("builder-call").classList.toggle("hidden", !agentId);
  document.querySelector('#agent-nav .nav-item[data-pane="conversations"]')
    .classList.toggle("hidden", !agentId);

  show("builder");
  showPane(agentId ? pane : "prompt");
}

$("new-agent").onclick = () => openBuilder(null);
$("builder-cancel").onclick = () => { show("agents"); loadAgents(); };
$("builder-call").onclick = () => { if (EDITING) openCall(EDITING); };
$("builder-save").onclick = async () => {
  const payload = formToAgent();
  try {
    if (EDITING) {
      await api(`/api/agents/${EDITING}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("Agent saved");
      await loadAgents();
      openBuilder(EDITING, currentPane());
    } else {
      const created = await api("/api/agents",
        { method: "POST", body: JSON.stringify(payload) });
      toast("Agent created");
      await loadAgents();
      openBuilder(created.id, "prompt");
    }
  } catch (err) {
    toast(err.message, true);
  }
};

function currentPane() {
  const active = document.querySelector(".pane-view.active");
  return active ? active.dataset.pane : "prompt";
}

/* ------------------------------------------------------------------ */
/* one agent's call log                                                */
/* ------------------------------------------------------------------ */
async function loadAgentCalls() {
  if (!EDITING) return;
  const { calls } = await api(`/api/agents/${EDITING}/calls`);
  renderCallRows(calls, $("agent-calls-body"), $("agent-calls-empty"));
}

$("refresh-agent-calls").onclick = loadAgentCalls;

$("preview-voice").onclick = async () => {
  const status = $("preview-status");
  status.textContent = "synthesizing…";
  try {
    const res = await fetch("/api/tts/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: $("f-tts-provider").value,
        voice: $("f-tts-voice").value,
        language: $("f-language").value,
      }),
    });
    if (!res.ok) throw new Error((await res.json()).error || "Preview failed");
    const audio = new Audio(URL.createObjectURL(await res.blob()));
    await audio.play();
    status.textContent = "";
  } catch (err) {
    status.textContent = "";
    toast(err.message, true);
  }
};

/* ------------------------------------------------------------------ */
/* call view                                                           */
/* ------------------------------------------------------------------ */
async function openCall(agentId) {
  CALL_AGENT = await api(`/api/agents/${agentId}`);
  $("call-agent-name").textContent = CALL_AGENT.name;
  $("call-error").textContent = "";
  $("call-meta").classList.add("hidden");
  $("transcript").innerHTML =
    '<p class="empty">The conversation will appear here as it happens.</p>';

  const box = $("call-variables");
  box.innerHTML = "";
  (CALL_AGENT.variables || []).forEach((name) => {
    const label = document.createElement("label");
    label.textContent = name.replace(/_/g, " ");
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.var = name;
    label.appendChild(input);
    box.appendChild(label);
  });

  // The call screen lives inside the agent, so the sidebar keeps its name.
  EDITING = CALL_AGENT.id;
  $("agent-nav-name").textContent = CALL_AGENT.name;
  show("call");
  document.querySelectorAll("#agent-nav .nav-item")
    .forEach((t) => t.classList.remove("active"));
  $("crumb").textContent = `Agents / ${CALL_AGENT.name} / Test call`;
}

$("call-back").onclick = () => {
  if (EVENT_SOURCE) { EVENT_SOURCE.close(); EVENT_SOURCE = null; }
  // Back into the agent this call belongs to, on its own call log.
  if (CALL_AGENT) openBuilder(CALL_AGENT.id, "conversations");
  else show("agents");
};

$("do-call").onclick = async () => {
  const to = $("f-to").value.trim();
  if (!to) return toast("Enter a phone number", true);

  const variables = {};
  document.querySelectorAll("#call-variables input").forEach((input) => {
    if (input.value.trim()) variables[input.dataset.var] = input.value.trim();
  });

  $("do-call").disabled = true;
  $("call-error").textContent = "";
  try {
    const res = await api("/api/call", {
      method: "POST",
      body: JSON.stringify({ agent_id: CALL_AGENT.id, to, variables }),
    });
    $("call-meta").classList.remove("hidden");
    $("transcript").innerHTML = "";
    setStatus("dialing");
    watchCall(res.call_id);
    toast("Calling " + to);
  } catch (err) {
    $("call-error").textContent = err.message;
    toast(err.message, true);
  } finally {
    $("do-call").disabled = false;
  }
};

function setStatus(status) {
  const pill = $("call-status");
  pill.textContent = status.replace(/_/g, " ");
  pill.className = "pill " + status;
}

function bubble(who, text, cls, latency) {
  const empty = $("transcript").querySelector(".empty");
  if (empty) empty.remove();

  const el = document.createElement("div");
  el.className = "bubble " + cls;
  const label = document.createElement("div");
  label.className = "who";
  label.textContent = who;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  el.append(label, body);

  if (latency) {
    const meta = document.createElement("div");
    meta.className = "latency";
    meta.innerHTML = latency;
    el.appendChild(meta);
  }
  $("transcript").appendChild(el);
  $("transcript").scrollTop = $("transcript").scrollHeight;
}

function watchCall(callId) {
  if (EVENT_SOURCE) EVENT_SOURCE.close();
  EVENT_SOURCE = new EventSource(`/api/calls/${callId}/events`);
  const pending = {};

  EVENT_SOURCE.onmessage = (message) => {
    const e = JSON.parse(message.data);

    switch (e.event) {
      case "call_start":
        setStatus("in_progress");
        break;
      case "status":
        setStatus(e.status);
        break;
      case "stt":
        if (e.customer_said) {
          bubble(`Caller · ${e.language || ""}`, e.customer_said, "user",
                 `STT <b>${e.latency_ms}ms</b>`);
        }
        pending[e.turn] = { stt: e.latency_ms };
        break;
      case "filler":
        bubble("Filler", `(${e.category})`, "filler agent", null);
        break;
      case "llm":
        pending[e.turn] = { ...(pending[e.turn] || {}), llm: e.latency_ms,
                            text: e.agent_reply };
        break;
      case "tts_first_byte":
        pending[e.turn] = { ...(pending[e.turn] || {}), ttfb: e.ttfb_ms };
        break;
      case "turn_total": {
        const p = pending[e.turn] || {};
        bubble("Vaani", p.text || "", "agent",
          `STT <b>${e.stt_ms}ms</b> · LLM <b>${e.llm_ms}ms</b> · ` +
          `TTS <b>${p.ttfb ?? e.tts_ms}ms</b> · total <b>${e.total_ms}ms</b>` +
          (e.filler ? ` · filler <b>${e.filler}</b>` : ""));
        delete pending[e.turn];
        break;
      }
      case "llm_error":
        bubble("LLM failed — caller heard the fallback line", e.error || "", "filler agent");
        toast("LLM error: " + (e.error || "empty response"), true);
        break;
      case "outcome":
        bubble("Outcome recorded", `${e.outcome} — ${e.summary}`, "filler agent");
        break;
      case "barge_in":
        bubble("Caller interrupted", "—", "filler user");
        break;
      case "noise_rejected":
        // Shown, not hidden: "the agent ignored me" and "the agent answered the
        // television" look identical from outside, and this is the line that
        // tells them apart while tuning.
        bubble(
          "Ignored as background",
          e.reason === "empty_transcript"
            ? `"${e.transcript}"`
            : `${e.reason} · peak ${e.peak_rms} vs room ${e.noise_floor}`,
          "filler user"
        );
        break;
      case "language_held":
        bubble(
          "Kept " + e.language,
          `detected ${e.detected} — ${e.reason}`,
          "filler agent"
        );
        break;
      case "language_switched":
        bubble("Switched language", e.language, "filler agent");
        break;
      case "no_reply":
        // The agent speaking without the model behind it. Shown as its own kind
        // of line so a transcript full of check-ins reads as "the caller was
        // never getting through", which is what it means.
        bubble(
          e.closing ? "No answer — closing the call" : "Checking the caller can hear",
          `${e.spoken} (${e.reason}, ${e.rejected} discarded)`,
          "filler agent"
        );
        break;
      case "voice_language_corrected":
        bubble(
          "Reply was in the wrong language",
          `${e.script} text on a ${e.call_language} call — spoken as ${e.spoken_language}`,
          "filler agent"
        );
        break;
      case "call_end":
        setStatus("completed");
        EVENT_SOURCE.close();
        EVENT_SOURCE = null;
        break;
    }
  };

  EVENT_SOURCE.onerror = () => { /* EventSource retries on its own */ };
}

/* greeting arrives as an llm event on turn 0 with no matching turn_total */
/* handled above: it simply stays in `pending` until the first real turn.  */

/* ------------------------------------------------------------------ */
/* call log rows                                                       */
/* ------------------------------------------------------------------ */
function fmtWhen(iso) {
  if (!iso) return "—";
  const [date, time] = iso.replace("T", " ").split(" ");
  return `<b>${date}</b><br><span class="note inline">${(time || "").slice(0, 5)}</span>`;
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return "—";
  const total = Math.round(seconds);
  return total < 60 ? `${total} sec`
                    : `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
}

/* Calls are only ever listed inside the agent that placed them, so the rows
   carry no agent column — the workspace around them already names it. */
function renderCallRows(calls, body, emptyEl) {
  body.innerHTML = "";
  if (emptyEl) emptyEl.classList.toggle("hidden", calls.length > 0);

  calls.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtWhen(c.created_at)}</td>
      <td>${c.to_number}</td>
      <td><span class="pill ${c.status}">${(c.status || "").replace(/_/g, " ")}</span></td>
      <td>${c.turns || 0}</td>
      <td>${fmtDuration(c.duration_s)}</td>
      <td>${c.outcome || "—"}</td>
      <td class="row-open"><button class="btn ghost small">Details</button></td>`;
    tr.querySelector("button").onclick = () => openCallDrawer(c, tr);
    body.appendChild(tr);
  });
}

/* ------------------------------------------------------------------ */
/* call drawer — transcript, recording and per-turn latency            */
/* ------------------------------------------------------------------ */
function closeDrawer() {
  $("call-drawer").classList.add("hidden");
  // Stop whatever is playing; the element is replaced on the next open anyway.
  $("drawer-audio").innerHTML = "";
  document.querySelectorAll("tr.selected").forEach((tr) => tr.classList.remove("selected"));
}

$("drawer-close").onclick = closeDrawer;
$("drawer-scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("call-drawer").classList.contains("hidden")) closeDrawer();
});

document.querySelectorAll(".drawer-tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".drawer-tab").forEach((t) =>
      t.classList.toggle("active", t === tab));
    document.querySelectorAll(".drawer-pane").forEach((p) =>
      p.classList.toggle("active", p.dataset.tab === tab.dataset.tab));
  })
);

async function openCallDrawer(summary, row) {
  document.querySelectorAll("tr.selected").forEach((tr) => tr.classList.remove("selected"));
  if (row) row.classList.add("selected");

  $("call-drawer").classList.remove("hidden");
  $("drawer-sub").textContent =
    `${summary.agent_name || "Agent"} → ${summary.to_number} · ` +
    (summary.created_at || "").replace("T", " ");
  $("drawer-audio").innerHTML = '<p class="empty-audio">Loading recording…</p>';
  $("detail-transcript").innerHTML = '<p class="empty">Loading…</p>';

  const [{ call, turns }, audio] = await Promise.all([
    api(`/api/calls/${summary.id}`),
    api(`/api/calls/${summary.id}/audio`).catch(() => ({ available: false, clips: [] })),
  ]);

  renderAudioBlock(audio);
  renderDrawerTranscript(turns, audio);
  renderOverview(call);
  renderMetrics(turns);
}

function renderAudioBlock(audio) {
  const box = $("drawer-audio");
  if (!audio.available) {
    box.innerHTML =
      '<div class="empty-audio"><strong>Recording unavailable</strong>' +
      "The audio for this call was not kept on disk.</div>";
    return;
  }
  box.innerHTML = "";
  const player = document.createElement("audio");
  player.controls = true;
  player.preload = "none";
  player.src = audio.full_url;
  const caption = document.createElement("p");
  caption.className = "note";
  caption.textContent =
    `Full conversation · ${audio.clips.length} clips, caller and agent in order.`;
  box.append(player, caption);
}

function renderDrawerTranscript(turns, audio) {
  const box = $("detail-transcript");
  box.innerHTML = turns.length ? "" : '<p class="empty">No turns recorded.</p>';

  // One clip per (turn, role), so a bubble can play back exactly its own audio.
  const clipFor = {};
  (audio.clips || []).forEach((c) => { clipFor[`${c.turn}:${c.role}`] = c.url; });

  turns.forEach((t) => {
    const el = document.createElement("div");
    el.className = "bubble " + (t.role === "user" ? "user" : "agent");

    const who = document.createElement("div");
    who.className = "who";
    const label = document.createElement("span");
    label.textContent = (t.role === "user" ? "Caller" : "Vaani") +
                        (t.language ? ` · ${t.language}` : "");
    who.appendChild(label);

    const clip = clipFor[`${t.turn}:${t.role === "user" ? "user" : "agent"}`];
    if (clip) who.appendChild(playButton(clip));

    const bodyEl = document.createElement("div");
    bodyEl.className = "body";
    bodyEl.textContent = t.text;
    el.append(who, bodyEl);

    if (t.total_ms || t.stt_ms) {
      const meta = document.createElement("div");
      meta.className = "latency";
      const parts = [];
      if (t.stt_ms) parts.push(`STT <b>${t.stt_ms}ms</b>`);
      if (t.llm_ms) parts.push(`LLM <b>${t.llm_ms}ms</b>`);
      if (t.total_ms) parts.push(`total <b>${t.total_ms}ms</b>`);
      if (t.filler_played) parts.push(`filler <b>${t.filler_played}</b>`);
      meta.innerHTML = parts.join(" · ");
      el.appendChild(meta);
    }
    box.appendChild(el);
  });
  box.scrollTop = 0;
}

let CLIP_PLAYER = null;

function playButton(url) {
  const button = document.createElement("button");
  button.className = "play-turn";
  button.type = "button";
  button.textContent = "▶";
  button.title = "Play this turn";
  button.onclick = () => {
    // Only ever one clip at a time, so turns don't talk over each other.
    if (CLIP_PLAYER) {
      CLIP_PLAYER.pause();
      document.querySelectorAll(".play-turn.playing").forEach((b) => {
        b.classList.remove("playing");
        b.textContent = "▶";
      });
      if (CLIP_PLAYER.dataset.url === url) { CLIP_PLAYER = null; return; }
    }
    CLIP_PLAYER = new Audio(url);
    CLIP_PLAYER.dataset.url = url;
    button.classList.add("playing");
    button.textContent = "■";
    CLIP_PLAYER.onended = () => {
      button.classList.remove("playing");
      button.textContent = "▶";
      CLIP_PLAYER = null;
    };
    CLIP_PLAYER.play().catch(() => toast("Could not play this clip", true));
  };
  return button;
}

function renderOverview(call) {
  const variables = (() => {
    try { return JSON.parse(call.variables || "{}"); } catch (e) { return {}; }
  })();
  const rows = [
    ["Agent", call.agent_name || "—"],
    ["To", call.to_number],
    ["Direction", call.direction],
    ["Status", (call.status || "").replace(/_/g, " ")],
    ["Started", (call.started_at || "—").replace("T", " ")],
    ["Ended", (call.ended_at || "—").replace("T", " ")],
    ["Duration", fmtDuration(call.duration_s)],
    ["Turns", call.turns || 0],
    ["Outcome", call.outcome || "—"],
    ["Summary", call.outcome_summary || "—"],
    ["Call SID", call.call_sid || "—"],
  ];
  Object.entries(variables).forEach(([k, v]) => rows.push([`$${k}`, String(v)]));

  const list = $("detail-overview");
  list.innerHTML = "";
  rows.forEach(([term, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  });
}

function renderMetrics(turns) {
  const measured = turns.filter((t) => t.total_ms || t.stt_ms || t.llm_ms);
  const box = $("detail-metrics");
  if (!measured.length) {
    box.innerHTML = '<p class="empty">No latency was recorded for this call.</p>';
    return;
  }
  const avg = (key) => {
    const values = measured.map((t) => t[key]).filter(Boolean);
    return values.length
      ? Math.round(values.reduce((a, b) => a + b, 0) / values.length) + " ms"
      : "—";
  };
  box.innerHTML = `
    <dl class="facts">
      <dt>Average STT</dt><dd>${avg("stt_ms")}</dd>
      <dt>Average LLM</dt><dd>${avg("llm_ms")}</dd>
      <dt>Average TTS first byte</dt><dd>${avg("tts_ttfb_ms")}</dd>
      <dt>Average turn total</dt><dd>${avg("total_ms")}</dd>
    </dl>
    <div class="table-wrap" style="margin-top:16px">
      <table class="table">
        <thead><tr><th>Turn</th><th class="right">STT</th><th class="right">LLM</th>
          <th class="right">TTS</th><th class="right">Total</th><th>Filler</th></tr></thead>
        <tbody>${measured.map((t) => `
          <tr><td>${t.turn}</td>
            <td class="right">${t.stt_ms || "—"}</td>
            <td class="right">${t.llm_ms || "—"}</td>
            <td class="right">${t.tts_ttfb_ms || "—"}</td>
            <td class="right">${t.total_ms || "—"}</td>
            <td>${t.filler_played || "—"}</td></tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

/* ------------------------------------------------------------------ */
/* credentials                                                         */
/* ------------------------------------------------------------------ */
const STATUS_CLASS = { ok: "ok", invalid: "bad", unknown: "warn" };

function showCredResult(key, result) {
  const el = document.querySelector(`[data-status-for="${key}"]`);
  if (!el || !result) return;
  const prefix = { ok: "✓", invalid: "✕", unknown: "!" }[result.status] || "";
  el.textContent = `${prefix} ${result.message}`;
  el.className = `note cred-status ${STATUS_CLASS[result.status] || ""}`;
}

function showCredResults(results) {
  Object.entries(results || {}).forEach(([key, result]) => showCredResult(key, result));
}

async function loadCredentials() {
  const data = await api("/api/credentials");
  const box = $("credential-fields");
  box.innerHTML = "";

  data.providers.forEach((p) => {
    const label = document.createElement("label");
    label.innerHTML = `${p.label} <span class="note inline">— ${p.hint}</span>`;
    const input = document.createElement("input");
    input.type = "password";
    input.dataset.cred = p.key;
    input.placeholder = p.configured ? `saved (${p.masked})` : "not set";
    label.appendChild(input);
    box.appendChild(label);

    const status = document.createElement("p");
    status.className = "note cred-status";
    status.dataset.statusFor = p.key;
    box.appendChild(status);
  });

  $("tw-sid").value = data.twilio.account_sid || "";
  $("tw-from").value = data.twilio.from_number || "";
  $("tw-token").placeholder = data.twilio.configured
    ? `saved (${data.twilio.auth_token_masked})` : "not set";
  $("tw-status").textContent = data.twilio.configured
    ? "Twilio is configured." : "Twilio is not configured — calls will be refused.";
  $("tw-status").className = "note cred-status";
}

$("save-credentials").onclick = async (event) => {
  const button = event.currentTarget;
  const providers = {};
  document.querySelectorAll("#credential-fields input").forEach((input) => {
    if (input.value.trim()) providers[input.dataset.cred] = input.value.trim();
  });

  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const data = await api("/api/credentials", {
      method: "POST",
      body: JSON.stringify({
        providers,
        twilio: {
          account_sid: $("tw-sid").value.trim(),
          auth_token: $("tw-token").value.trim(),
          from_number: $("tw-from").value.trim(),
        },
      }),
    });
    await loadCredentials();
    showCredResults(data.results);
    if (data.rejected.length) {
      toast(`Not saved — rejected by the provider: ${data.rejected.join(", ")}`, true);
    } else {
      toast(data.saved.length ? "Credentials verified and saved" : "Nothing to save");
    }
    loadCatalog();
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Save";
  }
};

$("test-credentials").onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Testing…";
  try {
    const data = await api("/api/credentials/test", { method: "POST" });
    showCredResults(data.results);
    const bad = Object.entries(data.results).filter(([, r]) => r.status === "invalid");
    toast(
      bad.length ? `Rejected: ${bad.map(([k]) => k).join(", ")}` : "All saved keys work",
      bad.length > 0
    );
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Test saved keys";
  }
};

/* ------------------------------------------------------------------ */
(async function init() {
  try {
    await loadCatalog();
    await loadAgents();
  } catch (err) {
    toast(err.message, true);
  }
})();
