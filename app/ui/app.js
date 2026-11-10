/* Kavach live screen.
 *
 * One SSE connection carries every call event; one WebSocket carries microphone
 * audio upstream. No framework, no build step.
 */

const SIGNALS = [
  ["authority", "Claims authority"],
  ["fear", "Fear / legal threat"],
  ["urgency", "Urgency pressure"],
  ["isolation", "Asks for secrecy"],
  ["sensitive_request", "OTP / ID / app"],
  ["money_request", "Money demand"],
];

const $ = (id) => document.getElementById(id);
const OUT_RATE = 16000;

let currentCallId = null;
let micWs = null;
let mediaStream = null;
let audioCtx = null;
let micNode = null;
let segmentCount = 0;
let resampler = { carry: new Float32Array(0), phase: 0 };

/* ---------------- SSE ---------------- */

function connectStream() {
  const es = new EventSource("/api/stream");
  es.onopen = () => setConn(true);
  es.onerror = () => setConn(false);
  es.onmessage = (ev) => {
    try {
      handleEvent(JSON.parse(ev.data));
    } catch (err) {
      console.warn("bad event", err);
    }
  };
}

function setConn(ok) {
  const pill = $("conn");
  pill.textContent = ok ? "live" : "reconnecting";
  pill.className = "pill " + (ok ? "online" : "offline");
}

function handleEvent(evt) {
  if (evt.kind === "hello") return;
  if (currentCallId && evt.call_id && evt.call_id !== currentCallId) return;

  switch (evt.kind) {
    case "segment":
      addSegment(evt.data);
      break;
    case "risk":
      renderRisk(evt.data);
      break;
    case "alert":
      raiseAlert(evt.data);
      break;
    case "warning":
      playWarning(evt.data.url);
      break;
    case "status":
      handleStatus(evt.data);
      break;
    case "llm":
      if (evt.data.reason_en) $("reason").textContent = evt.data.reason_en;
      break;
  }
}

/* ---------------- rendering ---------------- */

function addSegment(data) {
  const box = $("transcript");
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();

  const line = document.createElement("div");
  line.className = "line";
  const text = document.createElement("span");
  text.textContent = data.text;
  const meta = document.createElement("span");
  meta.className = "meta";
  const lang = data.language ? ` · ${data.language}` : "";
  meta.textContent = `#${data.index} · ${data.latency_ms} ms stt${lang}`;
  line.append(text, meta);
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;

  segmentCount += 1;
  $("segment-count").textContent = `${segmentCount} segment${segmentCount === 1 ? "" : "s"}`;
}

function renderRisk(data) {
  const score = Math.round((data.score || 0) * 100);
  $("score").textContent = score;
  $("meter-fill").style.width = `${score}%`;

  const label = data.alerted ? "alert" : data.level || "calm";
  $("score-label").textContent = label;

  const color = data.alerted ? "var(--alert)" : score >= 45 ? "var(--elevated)" : "var(--calm)";
  $("meter-fill").style.background = color;
  $("score-label").style.color = color;
  $("score").style.color = data.alerted ? "var(--alert)" : "var(--text)";

  document.body.classList.toggle("alerting", Boolean(data.alerted));

  const active = data.signals || {};
  document.querySelectorAll("#signals li").forEach((li) => {
    li.classList.toggle("active", Boolean(active[li.dataset.signal]));
  });

  if (data.reason_en) $("reason").textContent = data.reason_en;
}

function raiseAlert(data) {
  $("alert-banner").classList.remove("hidden");
  $("alert-sub").textContent = data.warning_hi || "कोई ओटीपी या पैसे न भेजें। फोन काट दीजिए।";
  document.body.classList.add("alerting");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function playWarning(url) {
  const audio = $("warning-audio");
  audio.src = url + "?t=" + Date.now();
  audio.play().catch((err) => console.warn("audio blocked", err));
}

function handleStatus(data) {
  $("source-label").textContent = data.state === "running" ? data.source || "live" : data.state;
  if (data.state === "ended" || data.state === "error") {
    stopLocalAudio();
    setRunning(false);
    if (data.state === "error") {
      $("reason").textContent = data.message || "call failed";
    }
  }
  if (data.state === "running") setRunning(true);
}

function setRunning(running) {
  $("btn-stop").disabled = !running;
  $("btn-mic").disabled = running;
  $("btn-demo").disabled = running;
}

function resetUI() {
  $("transcript").innerHTML = '<div class="empty">Listening…</div>';
  $("score").textContent = "0";
  $("score-label").textContent = "calm";
  $("meter-fill").style.width = "0%";
  $("meter-fill").style.background = "var(--calm)";
  $("score").style.color = "var(--text)";
  $("score-label").style.color = "var(--calm)";
  $("reason").textContent = "No risk signals yet.";
  $("alert-banner").classList.add("hidden");
  document.body.classList.remove("alerting");
  document.querySelectorAll("#signals li").forEach((li) => li.classList.remove("active"));
  segmentCount = 0;
  $("segment-count").textContent = "0 segments";
}

/* ---------------- calls ---------------- */

async function playDemo() {
  const name = $("demo-select").value;
  if (!name) return;
  resetUI();
  const res = await fetch(`/api/calls/demo/${encodeURIComponent(name)}`, { method: "POST" });
  if (!res.ok) {
    $("reason").textContent = await errorText(res);
    return;
  }
  const body = await res.json();
  currentCallId = body.call_id;
  setRunning(true);
}

async function startMic() {
  resetUI();
  const res = await fetch("/api/calls/mic", { method: "POST" });
  if (!res.ok) {
    $("reason").textContent = await errorText(res);
    return;
  }
  const body = await res.json();
  currentCallId = body.call_id;

  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (err) {
    $("reason").textContent = "Microphone permission denied.";
    return;
  }

  micWs = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/mic/${currentCallId}`);
  micWs.binaryType = "arraybuffer";

  audioCtx = new AudioContext();
  const inRate = audioCtx.sampleRate;
  resampler = { carry: new Float32Array(0), phase: 0 };

  const blob = new Blob([TAP_WORKLET], { type: "application/javascript" });
  await audioCtx.audioWorklet.addModule(URL.createObjectURL(blob));

  const source = audioCtx.createMediaStreamSource(mediaStream);
  micNode = new AudioWorkletNode(audioCtx, "pcm-tap");
  micNode.port.onmessage = (evt) => sendPcm(evt.data, inRate);

  // Route through a zero-gain node: keeps the graph alive without echoing the
  // microphone back to the speakers.
  const mute = audioCtx.createGain();
  mute.gain.value = 0;
  source.connect(micNode);
  micNode.connect(mute);
  mute.connect(audioCtx.destination);

  setRunning(true);
  $("source-label").textContent = `mic @ ${inRate} Hz`;
}

async function stopCall() {
  stopLocalAudio();
  if (currentCallId) {
    await fetch(`/api/calls/${currentCallId}/stop`, { method: "POST" }).catch(() => {});
  }
  setRunning(false);
}

function stopLocalAudio() {
  if (micWs) {
    try { micWs.close(); } catch (_) {}
    micWs = null;
  }
  if (micNode) {
    try { micNode.disconnect(); } catch (_) {}
    micNode = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach((t) => t.stop());
    mediaStream = null;
  }
  if (audioCtx) {
    audioCtx.close().catch(() => {});
    audioCtx = null;
  }
}

/* ---------------- audio plumbing ---------------- */

const TAP_WORKLET = `
class PcmTap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("pcm-tap", PcmTap);
`;

function sendPcm(float32, inRate) {
  if (!micWs || micWs.readyState !== WebSocket.OPEN) return;

  const resampled = resample(float32, inRate, OUT_RATE);
  if (!resampled.length) return;

  const pcm = new Int16Array(resampled.length);
  for (let i = 0; i < resampled.length; i++) {
    const s = Math.max(-1, Math.min(1, resampled[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  micWs.send(pcm.buffer);
}

/* Linear-interpolation resampler with carry-over, so block boundaries do not
 * click and the server always receives 16 kHz. */
function resample(input, inRate, outRate) {
  if (inRate === outRate) return input;

  const data = new Float32Array(resampler.carry.length + input.length);
  data.set(resampler.carry, 0);
  data.set(input, resampler.carry.length);

  const ratio = inRate / outRate;
  const outLen = Math.max(0, Math.floor((data.length - resampler.phase - 1) / ratio));
  const out = new Float32Array(outLen);

  for (let k = 0; k < outLen; k++) {
    const pos = resampler.phase + k * ratio;
    const i0 = Math.floor(pos);
    const frac = pos - i0;
    const a = data[i0] ?? 0;
    const b = data[i0 + 1] ?? a;
    out[k] = a * (1 - frac) + b * frac;
  }

  const consumed = resampler.phase + outLen * ratio;
  const cut = Math.floor(consumed);
  resampler.carry = data.slice(cut);
  resampler.phase = consumed - cut;
  return out;
}

async function errorText(res) {
  try {
    const body = await res.json();
    return body.detail || res.statusText;
  } catch (_) {
    return res.statusText;
  }
}

/* ---------------- boot ---------------- */

function buildSignalList() {
  const ul = $("signals");
  ul.innerHTML = "";
  for (const [key, label] of SIGNALS) {
    const li = document.createElement("li");
    li.dataset.signal = key;
    const dot = document.createElement("span");
    dot.className = "dot";
    const span = document.createElement("span");
    span.textContent = label;
    li.append(dot, span);
    ul.appendChild(li);
  }
}

async function loadDemos() {
  try {
    const res = await fetch("/api/calls/demo");
    const body = await res.json();
    const select = $("demo-select");
    select.innerHTML = "";
    if (!body.demos.length) {
      select.innerHTML = '<option value="">no demo audio yet</option>';
      return;
    }
    for (const name of body.demos) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }
  } catch (_) {
    /* offline is fine; the controls will report it */
  }
}

buildSignalList();
loadDemos();
connectStream();
$("btn-demo").addEventListener("click", playDemo);
$("btn-mic").addEventListener("click", startMic);
$("btn-stop").addEventListener("click", stopCall);
