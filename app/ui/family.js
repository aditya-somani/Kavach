/* Family dashboard: flagged calls, refreshed live over SSE. */

const $ = (id) => document.getElementById(id);

function connectStream() {
  const es = new EventSource("/api/stream");
  es.onopen = () => setConn(true);
  es.onerror = () => setConn(false);
  es.onmessage = (evt) => {
    try {
      const event = JSON.parse(evt.data);
      if (event.kind === "alert" || event.kind === "status") refresh();
    } catch (_) {
      /* ignore malformed frames */
    }
  };
}

function setConn(ok) {
  const pill = $("conn");
  pill.textContent = ok ? "live" : "reconnecting";
  pill.className = "pill " + (ok ? "online" : "offline");
}

async function refresh() {
  const res = await fetch("/api/calls?limit=40");
  const body = await res.json();
  render(body.calls || []);
}

function render(calls) {
  const box = $("calls");
  const flagged = calls.filter((c) => c.alerted || c.max_risk >= 0.45);
  if (!flagged.length) {
    box.innerHTML = '<div class="empty">No flagged calls yet.</div>';
    return;
  }

  box.innerHTML = "";
  for (const call of flagged) {
    const card = document.createElement("article");
    card.className = "call-card" + (call.alerted ? " flagged" : "");

    const head = document.createElement("div");
    head.className = "call-head";
    const time = document.createElement("time");
    time.textContent = formatTime(call.started_at);
    const score = document.createElement("span");
    score.className = "risk-chip";
    score.textContent = `${Math.round((call.max_risk || 0) * 100)}% risk`;
    head.append(time, score);

    const summary = document.createElement("p");
    summary.className = "call-summary";
    summary.textContent = call.summary || "No summary captured.";

    const reasons = document.createElement("ul");
    reasons.className = "call-reasons";
    const list = (call.reasons || []).slice(0, 3);
    if (!list.length) {
      const li = document.createElement("li");
      li.textContent = "No reason recorded.";
      reasons.appendChild(li);
    }
    for (const reason of list) {
      const li = document.createElement("li");
      li.textContent = reason;
      reasons.appendChild(li);
    }

    card.append(head, summary, reasons);
    box.appendChild(card);
  }
}

function formatTime(epochSeconds) {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

connectStream();
refresh();
setInterval(refresh, 10000);
