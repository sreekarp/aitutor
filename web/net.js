// Talking to the server: every request, its time limit and its errors. A turn's reply comes as lines: while the AI
// writes the tutor's reply, a {"wait": key, "say": line} line comes first, at once, so the page can say that line
// during the wait; the turn itself is the last line. Loaded after board.js, before the rest. Plain JS, no build step.
"use strict";

const REPLY_TIMEOUT_MS = 17000;   // stop waiting for the server after ~17 s
const TOO_LONG = "That took too long — try again or type it";

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;   // the HTTP status, or 0 when no answer came
  }
}

// Resolves to the reply's JSON, or throws ApiError. onWait(line) gets each waiting line as soon as it arrives; the
// time limit starts again then, as the server is busy with the AI and has its own deadline.
async function request(path, options = {}, onWait = () => {}) {
  const controller = new AbortController();
  let timer = setTimeout(() => controller.abort(), REPLY_TIMEOUT_MS);
  const waiting = (line) => {
    clearTimeout(timer);
    timer = setTimeout(() => controller.abort(), REPLY_TIMEOUT_MS);
    onWait(line);
  };
  try {
    const res = await fetch(path, { ...options, signal: controller.signal });
    if (res.ok) return await readLines(res, waiting);
    const data = await res.json().catch(() => ({}));
    const fallback = res.status === 422 ? "Please check what you typed." : "Something went wrong. Please try again.";
    throw new ApiError(res.status, typeof data.detail === "string" ? data.detail : fallback);
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(0, e.name === "AbortError" ? TOO_LONG : "Can't reach the tutor. Check your connection.");
  } finally {
    clearTimeout(timer);
  }
}

async function readLines(res, onWait) {   // each line as it arrives: waiting lines to onWait; the last one is the reply
  const reader = res.body.getReader(), decoder = new TextDecoder();
  let text = "", reply = {};
  for (;;) {
    const { value, done } = await reader.read();
    text += decoder.decode(value, { stream: !done });
    const lines = text.split("\n");
    text = done ? "" : lines.pop();   // a line still arriving waits for the rest of it
    for (const line of lines.filter((l) => l.trim())) {
      const data = JSON.parse(line);
      if (data.wait) onWait(data);
      else reply = data;
    }
    if (done) break;
  }
  if (reply.error) throw new ApiError(500, reply.error);
  return reply;
}

// keepalive: it is still sent if the page is closing. onWait: see request.
function postJSON(path, body, { keepalive = false, onWait } = {}) {
  return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    keepalive }, onWait);
}
