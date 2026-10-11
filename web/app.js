// The tutor's page: sign-in, Start / Continue and the conversation, one turn at a time, typed or spoken.
// board.js (the whiteboard and panels) and voice.js (sound and the mic) load first. Plain JS, no build step.
"use strict";

// ---------- settings ----------
const REPLY_TIMEOUT_MS = 17000;   // stop waiting for the server after ~17 s
const TOO_LONG = "That took too long — try again or type it";

const startButton = $("start"), sayForm = $("say-form"), textBox = $("text-box");

// ---------- talking to the server ----------
class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;   // the HTTP status, or 0 when no answer came
  }
}

async function request(path, options = {}) {   // resolves to the JSON reply, or throws ApiError
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REPLY_TIMEOUT_MS);
  try {
    const res = await fetch(path, { ...options, signal: controller.signal });
    const data = await res.json().catch(() => ({}));
    if (res.ok) return data;
    const fallback = res.status === 422 ? "Please check what you typed." : "Something went wrong. Please try again.";
    throw new ApiError(res.status, typeof data.detail === "string" ? data.detail : fallback);
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(0, e.name === "AbortError" ? TOO_LONG : "Can't reach the tutor. Check your connection.");
  } finally {
    clearTimeout(timer);
  }
}

function postJSON(path, body) {
  return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

// ---------- conversation and status ----------
function addTranscript(who, text) {   // who: "you", "tutor" or "note"
  const p = make("p", "", who);
  p.append(make("b", { you: "You", tutor: "Tutor", note: "Note" }[who]), " " + text);
  const box = $("transcript");
  box.append(p);
  box.scrollTop = box.scrollHeight;
}

function setStatus(text) {
  $("status").textContent = text;
}

function showNote(message) {   // a problem, in the status line and the transcript
  setStatus(message);
  addTranscript("note", message);
}

function setBusy(busy) {   // the text box and its buttons are off while a turn runs
  for (const el of sayForm.elements) el.disabled = busy;
}

// ---------- one turn ----------
function showSegment(segment) {
  runBoard(segment.board);
  addTranscript("tutor", segment.say);
}

// Every segment's audio is requested at once; the segments then play in order, and each one's board
// commands run as its audio starts. A segment without audio in time is read by the browser voice.
async function playReply(reply) {
  setStatus("Speaking…");
  const audios = reply.segments.map((_, i) => fetchAudio(reply.turn_id, i));
  for (const [i, segment] of reply.segments.entries()) {
    if (await playAudio(audios[i], () => { showSegment(segment); noteFirstAudio(); })) continue;
    showSegment(segment);
    await speakWithBrowser(segment.say);
  }
  audios.forEach((audio) => audio.then((url) => url && URL.revokeObjectURL(url)));
}

function showProblem(e) {
  if (!(e instanceof ApiError)) console.error(e);
  if (e.status === 401) return showSignIn("Please sign in again.");
  if (e.status === 409) {   // no session yet: back to the Start button
    sayForm.hidden = true;
    startButton.hidden = false;
  }
  const message = e instanceof ApiError ? e.message : "Something went wrong. Please try again.";
  showNote(message);
  if (e.status === 0) speakWithBrowser(message);   // the student may be listening, not looking
}

async function runTurn(send) {   // send() makes the request and resolves to the reply JSON
  stopSpeaking();
  setBusy(true);
  setStatus("Thinking…");
  let typeIt = false;
  try {
    const reply = await send();
    drawPanels(reply);
    await playReply(reply);
    typeIt = reply.input_hint === "type";
    setStatus(typeIt ? "Your turn. Typing may be easier." : "Your turn.");
  } catch (e) {
    showProblem(e);
  }
  stopClickedAt = 0;   // (voice.js) only this turn's reply counts as the "first audio"
  setBusy(false);
  if (typeIt) textBox.focus();
}

// ---------- sign-in, Start / Continue and the text box ----------
function showSignIn(message = "") {
  stopSpeaking();
  $("app").hidden = true;
  $("signin").hidden = false;
  $("signin-error").textContent = message;
}

function showTutor(state) {   // after sign-in or a reload: the saved board and panels
  $("signin").hidden = true;
  $("app").hidden = false;
  board = state.board;   // board.js keeps the lines
  drawBoard();
  drawPanels(state);
  startButton.textContent = state.progress.some((row) => row.status !== "not_started") ? "Continue" : "Start";
  startButton.hidden = false;
  sayForm.hidden = true;
  setStatus(`Press ${startButton.textContent} when you're ready.`);
}

async function loadPage() {
  try {
    showTutor(await request("/api/state"));
  } catch (e) {
    showSignIn(e.status === 401 ? "" : e.message);
  }
}

async function onSignIn(event) {
  event.preventDefault();
  $("signin-error").textContent = "";
  try {
    await postJSON("/api/login", Object.fromEntries(new FormData(event.target)));
    await loadPage();
  } catch (e) {
    $("signin-error").textContent = e.message;
  }
}

async function onSignOut() {
  stopSpeaking();
  await postJSON("/api/logout", {}).catch(() => {});
  location.reload();
}

function onStart() {
  unlockSound();   // first, while the click is still being handled
  startButton.hidden = true;
  sayForm.hidden = false;
  runTurn(() => postJSON("/api/session", {}));
}

function onSend(event) {
  event.preventDefault();
  const text = textBox.value.trim();
  if (!text) return;
  textBox.value = "";
  addTranscript("you", text);
  runTurn(() => postJSON("/api/turn", { text, prev_first_audio_ms: takeFirstAudioMs() }));
}

$("signin-form").addEventListener("submit", onSignIn);
$("signout").addEventListener("click", onSignOut);
startButton.addEventListener("click", onStart);
sayForm.addEventListener("submit", onSend);
micButton.addEventListener("click", onMic);
if (canSpeak) speechSynthesis.onvoiceschanged = chooseVoice;
if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {   // not https (or localhost): no mic
  micButton.hidden = true;
  $("mic-note").hidden = false;
}
loadPage();
