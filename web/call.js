// The call page. Microphone -> AudioWorklet (16 kHz PCM16) -> WebSocket -> server; server audio (PCM16 + sample rate) ->
// gapless scheduled playback. Everything the server sends is inserted as text (textContent), never as HTML.
"use strict";
const $ = (id) => document.getElementById(id);
const LABEL = { idle: "Tap to call", connecting: "Connecting…", listening: "Listening", thinking: "Thinking…", speaking: "Speaking. Talk to interrupt" };

let ws = null, ctx = null, stream = null, node = null, sink = null, wake = null;
let ready = false, muted = false, ending = false, acceptAudio = false, endpointMs = 650;
let nextT = 0, framesRecv = 0, idleTimer = null, level = 0;
const active = new Set();

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } },
};

function show(el, on) { el.hidden = !on; }
function setError(msg) { const e = $("error"); e.textContent = msg || ""; show(e, !!msg); }

function setState(s) {
  const orb = $("orb");
  orb.dataset.state = s;
  $("status").textContent = ready && muted && s === "listening" ? "Muted" : LABEL[s] || s;
  const inCall = s !== "idle";
  orb.setAttribute("aria-label", inCall ? "Call in progress" : "Start call");
  for (const id of ["mute", "stop", "end", "text", "send"]) $(id).disabled = !inCall || (id === "stop" && s !== "speaking" && s !== "thinking");
}

function addLine(who, text, sources) {
  const li = document.createElement("li");
  li.className = who;
  const b = document.createElement("b");
  b.textContent = who === "you" ? "You" : "Assistant";
  li.append(b, document.createTextNode(text));
  if (sources && sources.length) {
    const s = document.createElement("span");
    s.className = "cite";
    s.textContent = "Sources: " + [...new Set(sources.map((x) => x.source))].join(", ");
    li.append(s);
  }
  const log = $("log");
  log.append(li);
  while (log.children.length > 80) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

// ------------------------------------------------------------------ playback
function flush() {
  for (const s of active) { try { s.onended = null; s.stop(); } catch (e) { /* already stopped */ } }
  active.clear();
  nextT = 0;
  clearTimeout(idleTimer);
  idleTimer = 0;
}

function sendIdle() {
  if (ws && ws.readyState === 1 && active.size === 0) ws.send(JSON.stringify({ type: "idle", chunks: framesRecv }));
}

function play(buf) {
  framesRecv++;
  if (!acceptAudio || !ctx) return;
  const dv = new DataView(buf);
  const rate = dv.getUint32(0, true);
  const n = (buf.byteLength - 4) >> 1;
  if (n <= 0) return;
  const pcm = new Int16Array(buf, 4, n);
  const ab = ctx.createBuffer(1, n, rate);
  const ch = ab.getChannelData(0);
  for (let i = 0; i < n; i++) ch[i] = pcm[i] / 32768;
  const src = ctx.createBufferSource();
  src.buffer = ab;
  src.connect(ctx.destination);
  clearTimeout(idleTimer);
  const t = Math.max(ctx.currentTime + 0.04, nextT);
  src.start(t);
  nextT = t + ab.duration;
  active.add(src);
  src.onended = () => {
    active.delete(src);
    if (active.size === 0) { clearTimeout(idleTimer); idleTimer = setTimeout(() => { idleTimer = 0; sendIdle(); }, 250); }
  };
}

// ------------------------------------------------------------------ call
async function startCall() {
  setError("");
  const code = $("code").value.trim() || store.get("invite") || "";
  if (!code) { setError("Enter your invite code first."); return; }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setError("This page needs a secure connection (https or localhost) to use the microphone."); return;
  }
  setState("connecting");
  ready = false; ending = false; muted = false; framesRecv = 0; acceptAudio = false;
  $("mute").textContent = "Mute"; $("mute").setAttribute("aria-pressed", "false"); $("metrics").textContent = "";
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  } catch (e) {
    teardown(e.name === "NotAllowedError" ? "The microphone is blocked. Allow it in the address bar, then tap to call again."
      : e.name === "NotFoundError" ? "No microphone was found." : "Could not open the microphone: " + e.message);
    return;
  }
  try {
    ctx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: "interactive" });
    await ctx.resume();
    await ctx.audioWorklet.addModule("/web/worklet.js");
    const src = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, "capture");
    sink = ctx.createGain();
    sink.gain.value = 0;                                  // the worklet is pulled by being connected, but must stay silent
    src.connect(node); node.connect(sink); sink.connect(ctx.destination);
    node.port.onmessage = (e) => {
      level = level * 0.7 + Math.min(1, e.data.rms * 6) * 0.3;
      $("orb").style.setProperty("--level", level.toFixed(2));
      if (ready && !muted && ws && ws.readyState === 1 && ws.bufferedAmount < 262144) ws.send(e.data.pcm);
    };
  } catch (e) {
    teardown("Audio could not start in this browser: " + e.message);
    return;
  }
  try { wake = await navigator.wakeLock.request("screen"); } catch (e) { /* not supported or denied: fine */ }

  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/call");
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(JSON.stringify({ type: "hello", invite: code }));
  ws.onmessage = (e) => (typeof e.data === "string" ? onEvent(JSON.parse(e.data), code) : play(e.data));
  ws.onclose = () => { if (!ending) teardown(ready ? "" : "Could not connect. Is the assistant running?"); };
  ws.onerror = () => { /* onclose follows */ };
}

function onEvent(m, code) {
  switch (m.type) {
    case "ready":
      ready = true; endpointMs = m.endpoint_ms || 650;
      $("who").textContent = "Signed in as " + m.name;
      if ($("remember").checked) store.set("invite", code); else store.del("invite");
      break;
    case "state":
      if (m.state === "speaking") acceptAudio = true;
      setState(m.state);
      break;
    case "user": addLine("you", m.text); break;
    case "assistant": addLine("agent", m.text, m.sources); break;
    case "clear": flush(); acceptAudio = false; break;
    case "metrics":
      $("metrics").textContent = "Replied " + ((m.first_audio_ms + endpointMs) / 1000).toFixed(1) + " s after you stopped talking"
        + (m.stt_ms != null ? " · heard in " + m.stt_ms + " ms" : "") + (m.brain_ms != null ? " · looked up in " + m.brain_ms + " ms" : "");
      break;
    case "bye": ending = true; drainThenTeardown(); break;
    case "error":
      if (m.code === "auth") { store.del("invite"); show($("call"), false); show($("join"), true); $("join-error").textContent = m.message; show($("join-error"), true); }
      teardown(m.code === "auth" ? "" : m.message);
      break;
  }
}

function drainThenTeardown() {
  let tries = 0;
  const wait = () => { if ((active.size === 0 && !idleTimer) || ++tries > 80) teardown(""); else setTimeout(wait, 100); };
  setTimeout(wait, 150);
}

function teardown(message) {
  ending = true; ready = false;
  flush();
  try { if (ws) { ws.onclose = null; ws.close(); } } catch (e) { /* ignore */ }
  try { if (node) node.disconnect(); } catch (e) { /* ignore */ }
  try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* ignore */ }
  try { if (ctx) ctx.close(); } catch (e) { /* ignore */ }
  try { if (wake) wake.release(); } catch (e) { /* ignore */ }
  ws = ctx = stream = node = sink = wake = null;
  $("orb").style.setProperty("--level", "0");
  setState("idle");
  setError(message || "");
  $("status").textContent = message ? LABEL.idle : "Call ended. Tap to call again";
}

function endCall() {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "end" }));
  teardown("");
}

function interrupt() {
  if (!ws || ws.readyState !== 1) return;
  flush(); acceptAudio = false;
  ws.send(JSON.stringify({ type: "interrupt" }));
}

// ------------------------------------------------------------------ wiring
$("join-btn").addEventListener("click", () => {
  const code = $("code").value.trim();
  if (!code) { $("join-error").textContent = "Enter your invite code."; show($("join-error"), true); return; }
  if ($("remember").checked) store.set("invite", code);
  show($("join-error"), false); show($("join"), false); show($("call"), true);
  $("who").textContent = "Ready";
});
$("code").addEventListener("keydown", (e) => { if (e.key === "Enter") $("join-btn").click(); });
$("leave").addEventListener("click", () => { endCall(); store.del("invite"); $("code").value = ""; show($("call"), false); show($("join"), true); $("code").focus(); });
$("orb").addEventListener("click", () => { if ($("orb").dataset.state === "idle") startCall(); });
$("end").addEventListener("click", endCall);
$("stop").addEventListener("click", interrupt);
$("mute").addEventListener("click", () => {
  muted = !muted;
  $("mute").textContent = muted ? "Unmute" : "Mute";
  $("mute").setAttribute("aria-pressed", String(muted));
  setState($("orb").dataset.state);
});
$("typed").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("text").value.trim();
  if (!t || !ws || ws.readyState !== 1) return;
  $("text").value = "";
  flush(); acceptAudio = false;
  ws.send(JSON.stringify({ type: "text", text: t }));
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("orb").dataset.state !== "idle") interrupt(); });

(function init() {
  setState("idle");
  const saved = store.get("invite");
  if (saved) { $("code").value = saved; show($("join"), false); show($("call"), true); $("who").textContent = "Ready"; }
})();
