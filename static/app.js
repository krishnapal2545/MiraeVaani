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

function show(view) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("view-" + view).classList.add("active");
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === view)
  );
  if (view === "calls") loadCalls();
  if (view === "credentials") loadCredentials();
}

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => show(tab.dataset.view))
);

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
    refreshPromptVars();
  }
});

/* ------------------------------------------------------------------ */
/* agents                                                              */
/* ------------------------------------------------------------------ */
async function loadAgents() {
  const { agents } = await api("/api/agents");
  const list = $("agent-list");
  list.innerHTML = "";
  $("agents-empty").classList.toggle("hidden", agents.length > 0);

  agents.forEach((a) => {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <h4></h4>
      <div class="stack">
        <span class="chip">${a.stt_provider}</span>
        <span class="chip">${a.llm_model}</span>
        <span class="chip">${a.tts_provider} · ${a.tts_voice}</span>
        <span class="chip">${a.language_mode === "auto" ? "auto-detect" : a.language}</span>
      </div>
      <div class="actions">
        <button class="btn primary small" data-act="call">Call</button>
        <button class="btn ghost small" data-act="edit">Edit</button>
        <button class="btn ghost small" data-act="delete">Delete</button>
      </div>`;
    card.querySelector("h4").textContent = a.name;
    card.querySelector('[data-act="call"]').onclick = () => openCall(a.id);
    card.querySelector('[data-act="edit"]').onclick = () => openBuilder(a.id);
    card.querySelector('[data-act="delete"]').onclick = async () => {
      if (!confirm(`Delete "${a.name}"?`)) return;
      await api(`/api/agents/${a.id}`, { method: "DELETE" });
      toast("Agent deleted");
      loadAgents();
    };
    list.appendChild(card);
  });
}

function formToAgent() {
  return {
    name: $("f-name").value.trim() || "Untitled agent",
    stt_provider: $("f-stt-provider").value,
    stt_model: $("f-stt-model").value,
    language_mode: $("f-language-mode").value,
    language: $("f-language").value,
    llm_provider: $("f-llm-provider").value,
    llm_model: $("f-llm-model").value,
    temperature: parseFloat($("f-temperature").value) || 0.4,
    max_output_tokens: parseInt($("f-max-tokens").value, 10) || 150,
    tts_provider: $("f-tts-provider").value,
    tts_voice: $("f-tts-voice").value,
    system_prompt: $("f-prompt").value,
    greeting_mode: $("f-greeting-mode").value,
    greeting_text: $("f-greeting-text").value,
    fillers_enabled: $("f-fillers").checked,
    filler_delay_ms: parseInt($("f-filler-delay").value, 10) || 350,
    silence_threshold_rms: parseInt($("f-rms").value, 10) || 300,
    silence_end_seconds: parseFloat($("f-silence").value) || 0.8,
    min_utterance_seconds: 0.3,
    redirect_number: $("f-redirect").value.trim(),
  };
}

function agentToForm(a) {
  $("f-name").value = a.name || "";
  $("f-stt-provider").value = a.stt_provider;
  refreshSttModels(a.stt_model);
  $("f-language-mode").value = a.language_mode;
  $("f-language").value = a.language;
  $("f-llm-provider").value = a.llm_provider;
  refreshLlmModels(a.llm_model);
  $("f-temperature").value = a.temperature;
  $("f-max-tokens").value = a.max_output_tokens;
  $("f-tts-provider").value = a.tts_provider;
  refreshVoices(a.tts_voice);
  $("f-prompt").value = a.system_prompt || "";
  $("f-greeting-mode").value = a.greeting_mode;
  $("f-greeting-text").value = a.greeting_text || "";
  $("f-fillers").checked = !!a.fillers_enabled;
  $("f-filler-delay").value = a.filler_delay_ms;
  $("filler-delay-label").textContent = a.filler_delay_ms;
  $("f-rms").value = a.silence_threshold_rms;
  $("f-silence").value = a.silence_end_seconds;
  $("f-redirect").value = a.redirect_number || "";
  refreshPromptVars();
}

const DEFAULT_AGENT = {
  name: "", stt_provider: "sarvam", stt_model: "saarika:v2.5",
  language_mode: "auto", language: "hi-IN",
  llm_provider: "gemini", llm_model: "gemini-2.5-flash-lite",
  temperature: 0.4, max_output_tokens: 150,
  tts_provider: "sarvam", tts_voice: "anushka",
  system_prompt: "", greeting_mode: "llm", greeting_text: "",
  fillers_enabled: true, filler_delay_ms: 350,
  silence_threshold_rms: 300, silence_end_seconds: 0.8, redirect_number: "",
};

async function openBuilder(agentId) {
  EDITING = agentId || null;
  if (agentId) {
    const agent = await api(`/api/agents/${agentId}`);
    $("builder-title").textContent = `Edit — ${agent.name}`;
    agentToForm(agent);
  } else {
    $("builder-title").textContent = "New agent";
    agentToForm({ ...DEFAULT_AGENT, system_prompt: CATALOG.starters[0].body });
  }
  show("builder");
}

$("new-agent").onclick = () => openBuilder(null);
$("builder-cancel").onclick = () => { show("agents"); loadAgents(); };
$("builder-save").onclick = async () => {
  const payload = formToAgent();
  try {
    if (EDITING) {
      await api(`/api/agents/${EDITING}`, { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await api("/api/agents", { method: "POST", body: JSON.stringify(payload) });
    }
    toast("Agent saved");
    show("agents");
    loadAgents();
  } catch (err) {
    toast(err.message, true);
  }
};

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

  show("call");
}

$("call-back").onclick = () => {
  if (EVENT_SOURCE) { EVENT_SOURCE.close(); EVENT_SOURCE = null; }
  show("agents");
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
      case "outcome":
        bubble("Outcome recorded", `${e.outcome} — ${e.summary}`, "filler agent");
        break;
      case "barge_in":
        bubble("Caller interrupted", "—", "filler user");
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
/* call history                                                        */
/* ------------------------------------------------------------------ */
async function loadCalls() {
  const { calls } = await api("/api/calls");
  const body = $("calls-body");
  body.innerHTML = "";
  calls.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${(c.created_at || "").replace("T", " ")}</td>
      <td>${c.agent_name || "—"}</td>
      <td>${c.to_number}</td>
      <td><span class="pill ${c.status}">${(c.status || "").replace(/_/g, " ")}</span></td>
      <td>${c.turns || 0}</td>
      <td>${c.outcome || "—"}</td>
      <td><button class="btn ghost small">View</button></td>`;
    tr.querySelector("button").onclick = () => showCallDetail(c.id);
    body.appendChild(tr);
  });
}

async function showCallDetail(callId) {
  const { turns } = await api(`/api/calls/${callId}`);
  const box = $("detail-transcript");
  box.innerHTML = turns.length ? "" : '<p class="empty">No turns recorded.</p>';

  turns.forEach((t) => {
    const el = document.createElement("div");
    el.className = "bubble " + (t.role === "user" ? "user" : "agent");
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = (t.role === "user" ? "Caller" : "Vaani") +
                      (t.language ? ` · ${t.language}` : "");
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

  $("call-detail").classList.remove("hidden");
}

$("close-detail").onclick = () => $("call-detail").classList.add("hidden");
$("refresh-calls").onclick = loadCalls;

/* ------------------------------------------------------------------ */
/* credentials                                                         */
/* ------------------------------------------------------------------ */
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
  });

  $("tw-sid").value = data.twilio.account_sid || "";
  $("tw-from").value = data.twilio.from_number || "";
  $("tw-token").placeholder = data.twilio.configured
    ? `saved (${data.twilio.auth_token_masked})` : "not set";
  $("tw-status").textContent = data.twilio.configured
    ? "Twilio is configured." : "Twilio is not configured — calls will be refused.";
}

$("save-credentials").onclick = async () => {
  const providers = {};
  document.querySelectorAll("#credential-fields input").forEach((input) => {
    if (input.value.trim()) providers[input.dataset.cred] = input.value.trim();
  });
  try {
    await api("/api/credentials", {
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
    toast("Credentials saved");
    loadCredentials();
    loadCatalog();
  } catch (err) {
    toast(err.message, true);
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
