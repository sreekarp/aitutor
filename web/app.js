// The tutor's page: sign-in, the whiteboard, the panels and the conversation, typed or spoken.
// Plain JS, no build step.
"use strict";

// ---------- settings ----------
const REPLY_TIMEOUT_MS = 17000;   // stop waiting for the server after ~17 s (spec §7.2)
const TOO_LONG = "That took too long — try again or type it";
const MAX_RECORDING_MS = 30000;   // a recording stops by itself after 30 s
const AUDIO_WAIT_MS = 3000;       // a segment's audio must start within ~3 s of being due, else the browser voice reads it
const RECORDING_TYPES = ["audio/webm;codecs=opus", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/ogg;codecs=opus"];
const SILENCE = "data:audio/wav;base64,UklGRnQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YVAAAACAgICAgICAgICAgICA" +
  "gICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgA==";   // 10 ms of silence
const BOARD_MAX_LINES = 8;        // as board.py: at most 8 lines, and the question's two lines stay
const PINNED_IDS = ["q-label", "q-eq"];
const POINTS_TO_MASTER = 2, MAX_HINTS = 3, MAX_RETEACHES = 3;   // as config.py
const KATEX_OPTIONS = {
  displayMode: true,
  throwOnError: true,
  trust: (c) => c.command === "\\htmlClass" && c.class === "hl",   // only our highlight
  strict: (code) => (code === "htmlExtension" ? "ignore" : "warn"),
};

const $ = (id) => document.getElementById(id);
const boardEl = $("board"), startButton = $("start"), sayForm = $("say-form"), textBox = $("text-box");
const micButton = $("mic");

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

// ---------- whiteboard ----------
let board = [];   // the lines on the board, kept like the server's: [{id, latex, formula, text}]

// Draws a line's LaTeX. With `part`, that exact snippet is highlighted; if KaTeX can't draw the
// snippet on its own, the whole line is highlighted instead (spec §6.4).
function renderLine(el, latex, part) {
  const i = part ? latex.indexOf(part) : -1;
  if (i >= 0) {
    const marked = latex.slice(0, i) + "\\htmlClass{hl}{" + part + "}" + latex.slice(i + part.length);
    try {
      katex.render(marked, el, KATEX_OPTIONS);
      return;
    } catch (e) {
      // the snippet isn't a whole piece of maths: highlight the whole line below
    }
  }
  if (part) el.classList.add("line-hl");
  try {
    katex.render(latex, el, KATEX_OPTIONS);
  } catch (e) {
    el.textContent = latex;   // KaTeX didn't load: show the source rather than nothing
  }
}

function drawBoard(marks = new Map()) {   // marks: line id -> the snippet to highlight ("" = the whole line)
  boardEl.replaceChildren(...board.map((line) => {
    const div = make("div", "", line.formula ? "line formula" : line.latex ? "line" : "line words");
    if (line.latex) renderLine(div, line.latex, marks.get(line.id));
    else div.textContent = line.text || "";
    if (marks.has(line.id) && !(line.latex && marks.get(line.id))) div.classList.add("line-hl");   // whole line
    return div;
  }));
}

function writeLine(c) {   // the same id rewrites a line in place; past 8 lines the oldest unpinned one goes
  const line = { id: c.id, latex: c.latex, formula: c.formula, text: c.text };
  const i = board.findIndex((old) => old.id === c.id);
  if (i >= 0) board[i] = line;
  else board.push(line);
  while (board.length > BOARD_MAX_LINES) board.splice(board.findIndex((l) => !PINNED_IDS.includes(l.id)), 1);
}

function runBoard(commands) {   // one segment's commands; its highlights replace the last segment's
  const marks = new Map();
  for (const c of commands) {
    if (c.op === "clear") board = [];
    if (c.op === "write") writeLine(c);
    if (c.op === "highlight") marks.set(c.target, c.part || "");
  }
  drawBoard(marks);
}

// ---------- progress and "Why?" panels ----------
function make(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}

function progressRow(row) {   // ✓ mastered, ⚠ needs review, ▶ current (with its step or points), · not started
  const icon = { mastered: "✓", needs_review: "⚠" }[row.status] || (row.current ? "▶" : "·");
  const li = make("li", `${icon} ${row.title}`, row.current ? "current" : row.status);
  if (row.current && row.status === "checking") {
    li.append(make("small", `points ${row.points}/${POINTS_TO_MASTER} · hint ${row.hint_level}/${MAX_HINTS}` +
      ` · reteach ${row.reteach_count}/${MAX_RETEACHES}`));
  } else if (row.current && Number.isInteger(row.step_index)) {
    li.append(make("small", `step ${row.step_index + 1} of ${row.step_count}`));
  }
  return li;
}

function drawPanels(data) {
  const current = data.progress.find((row) => row.current);
  $("concept").textContent = current ? current.title : "All concepts done";
  $("progress").replaceChildren(...data.progress.map(progressRow));
  $("why").replaceChildren(...data.why.slice(0, 5).map((decision) => {
    const li = make("li");
    li.append(make("b", decision.rule), decision.reason);
    return li;
  }));
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

// ---------- browser voice ----------
const canSpeak = "speechSynthesis" in window;
let browserVoice = null;   // an Indian-English voice if the browser has one, else any English voice
let utterance = null;      // keep a reference: Chrome can lose the events of an utterance nobody holds

function chooseVoice() {   // voices load late, so this runs again on "voiceschanged"
  const english = speechSynthesis.getVoices().filter((v) => /^en([-_]|$)/i.test(v.lang));
  browserVoice = english.find((v) => /^en[-_]IN$/i.test(v.lang)) || english[0] || null;
}

function stopSpeaking() {
  if (canSpeak) speechSynthesis.cancel();
}

function speakSentence(sentence) {   // resolves true if the voice started speaking
  return new Promise((resolve) => {
    if (!browserVoice) chooseVoice();
    utterance = new SpeechSynthesisUtterance(sentence);
    utterance.lang = browserVoice ? browserVoice.lang : "en-IN";
    if (browserVoice) utterance.voice = browserVoice;
    let started = false;
    const finish = () => { clearTimeout(noStart); clearTimeout(stuck); resolve(started); };
    const noStart = setTimeout(() => { if (!started) { speechSynthesis.cancel(); finish(); } }, 2000);
    const stuck = setTimeout(finish, 3000 + sentence.length * 120);   // "end" never came
    utterance.onstart = () => { started = true; noteFirstAudio(); };
    utterance.onend = utterance.onerror = finish;
    speechSynthesis.speak(utterance);
  });
}

async function speakWithBrowser(text) {   // one sentence per utterance: Chrome cuts off long speech
  if (!canSpeak) return false;
  const sentences = text.replace(/([.!?])\s+/g, "$1\n").split("\n").filter((s) => s.trim());
  for (const sentence of sentences) {
    if (!(await speakSentence(sentence))) return false;   // no working voice: the text is on screen
  }
  return true;
}

// ---------- server audio ----------
const player = new Audio();   // the one <audio> element: iPhones unlock sound per element, so every segment plays here

function unlockSound() {   // call inside a click: browsers (iPhones most of all) only allow sound after one
  player.src = SILENCE;
  player.play().catch(() => {});
  if (canSpeak) speechSynthesis.speak(new SpeechSynthesisUtterance(""));
}

function fetchAudio(turnId, i) {   // resolves to a blob: URL of segment i's audio, or null when there is none
  return fetch(`/api/tts/${turnId}/${i}`)
    .then((res) => (res.ok ? res.blob() : null))
    .then((blob) => (blob ? URL.createObjectURL(blob) : null))
    .catch(() => null);
}

// Plays one segment's audio on the one <audio> element. Resolves true once it has played, or false when it
// failed or hadn't started ~3 s after it was due (then the browser voice reads the segment instead).
function playAudio(audio, onStart) {
  return new Promise((resolve) => {
    let started = false, finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      player.onplaying = player.onpause = player.onended = player.onerror = null;
      if (!started) player.pause();   // too late: it must not talk over the browser voice
      resolve(started);
    };
    const timer = setTimeout(finish, AUDIO_WAIT_MS);
    audio.then((url) => {
      if (!url || finished) return finish();
      player.onplaying = () => {
        if (started) return;
        started = true;
        clearTimeout(timer);
        onStart();
      };
      player.onpause = player.onended = player.onerror = finish;
      player.src = url;
      player.play().catch(finish);
    });
  });
}

// ---------- latency: stop click -> first audio (spec §8.5) ----------
let stopClickedAt = 0;     // performance.now() at the last stop click; 0 when nothing is being measured
let firstAudioMs = null;   // the last measurement, sent with the next turn

function noteFirstAudio() {   // the first sound of the reply to a recording
  if (!stopClickedAt) return;
  firstAudioMs = Math.round(performance.now() - stopClickedAt);
  stopClickedAt = 0;
}

function takeFirstAudioMs() {   // hands the measurement to the next turn, once
  const ms = firstAudioMs;
  firstAudioMs = null;
  return ms;
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
  stopClickedAt = 0;   // only this turn's reply counts as the "first audio"
  setBusy(false);
  if (typeIt) textBox.focus();
}

// ---------- recording: click to start, click to stop ----------
let recorder = null;   // the MediaRecorder while the mic is on

async function onMic() {
  unlockSound();   // first, while the click is still being handled
  if (recorder) return stopRecording();
  setBusy(true);   // no typing, and no second recording, while the mic is on
  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    startRecording(stream);
  } catch (e) {
    if (stream) stream.getTracks().forEach((track) => track.stop());
    setBusy(false);
    showNote("I can't use the microphone. Allow it in the browser, or type instead.");
  }
}

function startRecording(stream) {
  const mimeType = RECORDING_TYPES.find((type) => MediaRecorder.isTypeSupported(type));   // undefined: browser picks
  const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks = [];
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = () => {
    stream.getTracks().forEach((track) => track.stop());   // release the mic
    sendRecording(new Blob(chunks, { type: rec.mimeType || mimeType || "audio/webm" }));
  };
  rec.start();
  recorder = rec;
  showMic(true);
  setStatus("Listening… click Stop when you're done.");
  setTimeout(() => { if (recorder === rec) stopRecording(); }, MAX_RECORDING_MS);
}

function stopRecording() {   // the stop click (or 30 s); onstop then sends the recording
  stopClickedAt = performance.now();
  recorder.stop();
  recorder = null;
  showMic(false);
}

function showMic(recording) {   // while recording, the mic button is a Stop button and the only control on
  micButton.setAttribute("aria-pressed", String(recording));
  micButton.textContent = recording ? "■ Stop" : "🎤 Speak";
  micButton.disabled = !recording;
}

function sendRecording(blob) {
  const form = new FormData();
  form.append("audio", blob, "recording");
  const ms = takeFirstAudioMs();
  if (ms !== null) form.append("prev_first_audio_ms", String(ms));
  runTurn(async () => {
    const reply = await request("/api/turn/audio", { method: "POST", body: form });
    if (reply.transcript) addTranscript("you", reply.transcript);
    return reply;
  });
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
  board = state.board;
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
