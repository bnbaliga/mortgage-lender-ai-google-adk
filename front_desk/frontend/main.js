// Front desk client: mic -> 16 kHz PCM -> /ws, 24 kHz voice back, client-side barge-in,
// live captions, a typed fallback, and the visit card filled in from tool results.
// Same wire protocol as the DJ builds, plus `action` / `tool_result` / `turn_complete`.

const $ = (id) => document.getElementById(id);
const orb = $("orb"), level = $("level"), statusEl = $("status"), callBtn = $("callbtn"), muteBtn = $("mutebtn");
const captions = $("captions"), typed = $("typed"), sendBtn = $("sendbtn"), clockEl = $("clock");
const patientEl = $("f-patient"), items = $("items"), slipnote = $("slipnote");
const logEl = $("log"), logCount = $("logcount"), alarm = $("alarm");

const BARGE_RMS = 0.04;      // mic level that counts as the caller talking over Ava
const BARGE_CHUNKS = 2;      // ...for this many 40 ms chunks in a row (ignores clicks)

let ws = null, audioCtx = null, micStream = null, worklet = null, sink = null;
let live = false, muted = false, speaking = false, loud = 0;
let nextStart = 0, sources = [];
let openLine = { user: null, agent: null };
let started = 0, clockTimer = null, logN = 0;

const TOOL_WORDS = {
  get_clinic_info: "Looked up clinic information", check_insurance: "Checked an insurance plan",
  find_available_slots: "Searched for open times", verify_patient: "Checked patient identity",
  register_new_patient: "Registered a new patient", list_my_appointments: "Opened upcoming appointments",
  book_appointment: "Booked an appointment", cancel_appointment: "Cancelled an appointment",
  reschedule_appointment: "Moved an appointment", request_prescription_refill: "Sent a refill request",
  transfer_to_staff: "Transferred to a team member", flag_emergency: "Flagged an emergency",
};

// ---------------------------------------------------------------- UI state ---
function setState(s, text) {
  orb.className = "orb " + s;
  if (text !== undefined) statusEl.textContent = text;
}

// the assistant's name comes from the server (FRONT_DESK_AGENT_NAME) — never hard-code it in the page
let agent = "Ava";
// emergency numbers come from clinic.json (India: 112 / 108 / Tele-MANAS 14416)
let em = { number: "112", ambulance: "108", crisis_line: "Tele-MANAS", crisis_number: "14416" };
function setNum(sel, n) {
  document.querySelectorAll(sel).forEach((a) => { a.textContent = n; a.href = "tel:" + n.replace(/[^\d+]/g, ""); });
}
fetch("/api/clinic").then((r) => r.json()).then((c) => {
  agent = c.agent;
  if (c.emergency) {
    em = c.emergency;
    setNum(".em-num", em.number); setNum(".em-amb", em.ambulance); setNum(".em-crisis", em.crisis_number);
    document.querySelectorAll(".em-crisis-name").forEach((el) => { el.textContent = em.crisis_line; });
  }
  $("clinic").textContent = c.clinic;
  document.querySelectorAll(".agent").forEach((el) => { el.textContent = agent; });
  typed.placeholder = `Prefer typing? Write to ${agent} here`;
  document.title = `${c.clinic} · front desk`;
}).catch(() => {});

function tick() {
  const s = Math.floor((Date.now() - started) / 1000);
  clockEl.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// ---------------------------------------------------------------- captions ---
function caption(role, text, final) {
  const empty = captions.querySelector(".empty");
  if (empty) empty.remove();
  let line = openLine[role];
  if (!line) {
    line = document.createElement("p");
    line.className = `line ${role} open`;
    const who = document.createElement("span");
    who.className = "who"; who.textContent = role === "agent" ? agent : "You";
    const body = document.createElement("span"); body.className = "body";
    line.append(who, body);
    captions.appendChild(line);
    openLine[role] = line;
    // a new line from one side closes the other side's line
    const other = role === "agent" ? "user" : "agent";
    if (openLine[other]) { openLine[other].classList.remove("open"); openLine[other] = null; }
  }
  const body = line.querySelector(".body");
  body.textContent = final ? text : body.textContent + text;
  if (final) { line.classList.remove("open"); openLine[role] = null; }
  captions.scrollTop = captions.scrollHeight;
}

// ------------------------------------------------------ the visit card + log ---
function addItem(what, tone, detail) {
  const li = document.createElement("li");
  li.className = "item fresh";
  const w = document.createElement("div"); w.className = `what ${tone}`; w.textContent = what;
  li.appendChild(w);
  if (detail) { const d = document.createElement("div"); d.className = "detail"; d.append(...detail); li.appendChild(d); }
  items.appendChild(li);
  slipnote.hidden = true;
}
const appt = (a) => `${a.visit} with ${a.provider}, ${a.when}`;

function onToolResult(tool, r) {
  if (!r || r.result !== "ok") {
    if (tool === "verify_patient" && r && r.code === "no_match") {
      patientEl.textContent = r.attempts_left ? "details didn't match — try again" : "couldn't confirm — ask for a team member";
    }
    return;
  }
  if (tool === "verify_patient" || tool === "register_new_patient") {
    patientEl.textContent = r.first_name + (tool === "register_new_patient" ? " (new patient)" : "");
    patientEl.classList.remove("blank");
  } else if (tool === "book_appointment") {
    addItem("Booked", "ok", [appt(r.booked)]);
  } else if (tool === "reschedule_appointment") {
    const old = document.createElement("s"); old.textContent = r.moved_from.when;
    addItem("Moved", "ok", [old, document.createElement("br"), appt(r.booked)]);
  } else if (tool === "cancel_appointment") {
    addItem("Cancelled", "off", [appt(r.cancelled) + (r.late_cancellation ? " — late cancellation fee may apply" : "")]);
  } else if (tool === "request_prescription_refill") {
    addItem("Refill request sent", "ok", [`To ${r.routed_to}. Usually handled within 2 business days.`]);
  } else if (tool === "transfer_to_staff") {
    addItem("Handed to a team member", "ok", ["Someone will call back within one business hour if the line is busy."]);
  } else if (tool === "flag_emergency") {
    alarm.hidden = false;
    addItem("Emergency", "off", [`Call ${em.number} now, or ${em.ambulance} for an ambulance. ` +
      `For a mental-health crisis, call ${em.crisis_line} on ${em.crisis_number}.`]);
  }
}

function onAction(m) {
  const li = document.createElement("li");
  if (m.status === "blocked") li.className = "blocked";
  li.textContent = (TOOL_WORDS[m.tool] || m.tool) + " ";
  const v = document.createElement("span");
  v.className = "verdict";
  v.textContent = m.status === "blocked" ? `— stopped: ${m.reason}` : "— allowed";
  li.appendChild(v);
  logEl.appendChild(li);
  logEl.scrollTop = logEl.scrollHeight;
  logCount.textContent = `(${++logN})`;
}

// ------------------------------------------------------------ token monitor ---
const fmt = (n) => (n || 0).toLocaleString();
function onUsage(u) {
  $("tok-empty").hidden = true; $("tok-body").hidden = false;
  const by = u.by_modality || {};
  $("tok-total").textContent = fmt(u.total);
  $("tok-head").textContent = `(${fmt(u.total)})`;
  $("tok-in").textContent = fmt(u.input);
  $("tok-in-audio").textContent = fmt(by["in:AUDIO"]);
  $("tok-in-text").textContent = fmt(by["in:TEXT"]);
  $("tok-out").textContent = fmt(u.output);
  $("tok-out-audio").textContent = fmt(by["out:AUDIO"]);
  $("tok-out-text").textContent = fmt(by["out:TEXT"]);
  $("tok-turns").textContent = fmt(u.reports);
  $("tok-cost").textContent = u.cost_usd == null ? "set prices in .env" : `$${u.cost_usd.toFixed(4)}`;
  $("tok-ctx").textContent = fmt(u.context);
  $("tok-ctx-max").textContent = fmt(u.context_limit);
  const pct = Math.min(100, (100 * u.context) / (u.context_limit || 1));
  const bar = $("tok-ctx-bar");
  bar.style.width = pct + "%";
  bar.classList.toggle("high", pct >= 80);
}
function resetUsage() {
  $("tok-empty").hidden = false; $("tok-body").hidden = true; $("tok-head").textContent = "";
}
function loadServerTotals() {
  fetch("/api/usage").then((r) => r.json()).then((s) => {
    if (!s.calls) return;
    const cost = s.cost_usd == null ? "" : `, about $${s.cost_usd.toFixed(4)}`;
    $("tok-server").textContent =
      `Since the server started: ${fmt(s.total)} tokens over ${s.calls} call${s.calls === 1 ? "" : "s"}${cost}.`;
  }).catch(() => {});
}
loadServerTotals();

// ---------------------------------------------------------- voice playback ---
function playVoice(buf) {
  const pcm = new Int16Array(buf);
  const f32 = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 0x8000;
  const ab = audioCtx.createBuffer(1, f32.length, 24000);
  ab.getChannelData(0).set(f32);
  const src = audioCtx.createBufferSource();
  src.buffer = ab; src.connect(audioCtx.destination);
  nextStart = Math.max(nextStart, audioCtx.currentTime + 0.02);
  src.start(nextStart); nextStart += ab.duration;
  sources.push(src);
  src.onended = () => {
    sources = sources.filter((s) => s !== src);
    if (!sources.length && live) { speaking = false; setState("listening", "Listening…"); }
  };
  if (!speaking) { speaking = true; setState("speaking", `${agent} is speaking`); }
}

function stopVoice() {                     // barge-in: silence Ava immediately
  sources.forEach((s) => { try { s.stop(); } catch {} });
  sources = []; nextStart = 0; speaking = false;
  if (live) setState("listening", "Listening…");
}

// ------------------------------------------------------------------ the call ---
async function startCall() {
  callBtn.disabled = true;
  resetUsage();
  setState("connecting", "Connecting…");
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  let micNote = "";
  try {
    await audioCtx.audioWorklet.addModule("/pcm-processor.js");
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    // no mic is not the end of the call: you can still hear Ava and type to her
    micNote = e.name === "NotAllowedError"
      ? `Microphone blocked — you can still type to ${agent} below.`
      : `No microphone found — you can still type to ${agent} below.`;
  }
  if (micStream) wireMic();
  connect(micNote);
}

function wireMic() {
  const src = audioCtx.createMediaStreamSource(micStream);
  worklet = new AudioWorkletNode(audioCtx, "pcm-processor");
  sink = audioCtx.createGain(); sink.gain.value = 0;          // keeps the graph running, silently
  src.connect(worklet); worklet.connect(sink); sink.connect(audioCtx.destination);
  worklet.port.onmessage = ({ data }) => {
    if (muted) return;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(data.pcm);
    level.style.transform = `scale(${1 + Math.min(data.rms * 4, 0.25)})`;
    loud = data.rms >= BARGE_RMS ? loud + 1 : 0;
    if (speaking && loud >= BARGE_CHUNKS) stopVoice();
  };
}

function connect(micNote) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    live = true; started = Date.now(); clockTimer = setInterval(tick, 1000); tick();
    setState("thinking", micNote || `${agent} is picking up…`);
    callBtn.textContent = "End call"; callBtn.classList.add("end"); callBtn.disabled = false;
    muteBtn.hidden = !micStream; typed.disabled = false; sendBtn.disabled = false;
    if (!micStream) typed.focus();
  };
  ws.onmessage = (evt) => {
    if (typeof evt.data !== "string") { playVoice(evt.data); return; }
    const m = JSON.parse(evt.data);
    if (m.type === "transcript") {
      caption(m.role, m.text, m.final);
      if (m.role === "user" && !speaking) setState("thinking", `${agent} is thinking…`);
    } else if (m.type === "action") onAction(m);
    else if (m.type === "tool_result") onToolResult(m.tool, m.response);
    else if (m.type === "usage") onUsage(m);
    else if (m.type === "interrupted") stopVoice();
    else if (m.type === "error") setState("error", m.message);
  };
  ws.onclose = () => {
    const wasLive = live;
    cleanup();
    if (!wasLive) setState("error", "Couldn't reach the front desk server. Check that it's running, then call again.");
    else if (!orb.classList.contains("error")) setState("idle", "The call ended. Press call to start again.");
  };
}

function cleanup() {
  live = false; speaking = false; muted = false;
  sources.forEach((s) => { try { s.stop(); } catch {} }); sources = []; nextStart = 0;
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  if (audioCtx) audioCtx.close().catch(() => {});
  micStream = audioCtx = worklet = sink = null;
  clearInterval(clockTimer);
  setTimeout(loadServerTotals, 1500);   // the server adds this call to its totals as it hangs up
  callBtn.textContent = "Call the front desk"; callBtn.classList.remove("end"); callBtn.disabled = false;
  muteBtn.hidden = true; muteBtn.setAttribute("aria-pressed", "false"); muteBtn.textContent = "Mute";
  typed.disabled = true; sendBtn.disabled = true;
  level.style.transform = "";
  openLine = { user: null, agent: null };
}

function endCall() {
  if (ws) ws.close();
  cleanup();
  setState("idle", "Call ended. Press call to start again.");
}

callBtn.addEventListener("click", () => (live ? endCall() : startCall()));
muteBtn.addEventListener("click", () => {
  muted = !muted;
  muteBtn.setAttribute("aria-pressed", String(muted));
  muteBtn.textContent = muted ? "Unmute" : "Mute";
  if (live && !speaking) setState("listening", muted ? `Muted — ${agent} can't hear you` : "Listening…");
});
$("typeform").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = typed.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  stopVoice();
  ws.send(JSON.stringify({ type: "text", text }));
  typed.value = "";
});
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || ["INPUT", "BUTTON", "SUMMARY", "TEXTAREA"].includes(e.target.tagName)) return;
  e.preventDefault();
  live ? endCall() : startCall();
});
