// The tutor's page: sign-in, Start / Continue and the conversation, one turn at a time, typed or spoken.
// board.js (the whiteboard and panels) and voice.js (sound and the mic) load first. Plain JS, no build step.
"use strict";

// ---------- settings ----------
const REPLY_TIMEOUT_MS = 17000;   // stop waiting for the server after ~17 s
const TOO_LONG = "That took too long — try again or type it";
const TOAST_MS = 3000;            // how long "Topic mastered!" stays up

const startButton = $("start"), sayForm = $("say-form"), textBox = $("text-box"), topicStart = $("topic-start");

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

function setStatus(text) { $("status").textContent = text; }

function showNote(message) {   // a problem, in the status line and the transcript
  setStatus(message);
  addTranscript("note", message);
}

let busy = false;   // waiting for the server, or recording: no typing, and no topic can be started

function setBusy(on) {   // the text box and its buttons are off while waiting (not while the tutor speaks)
  busy = on;
  for (const el of sayForm.elements) el.disabled = on;
  topicStart.disabled = on;
  $("progress").classList.toggle("busy", on);
}

function setCaption(text, who = "tutor") {   // the line under the board: what the tutor says, or what it heard
  $("caption").textContent = text;
  $("caption").className = `caption ${who}`;
}

function celebrate() {
  const toast = $("toast");
  toast.textContent = "Topic mastered! ✓";
  toast.hidden = false;
  setTimeout(() => { toast.hidden = true; }, TOAST_MS);
}

// ---------- one turn ----------
let turnNo = 0;             // the newest turn: an older one, cut short by it, leaves the page alone
let speakingReply = null;   // the reply being spoken, {segments, shown}: what a stop still has to show

function showSegment(segment) {
  runBoard(segment.board);
  setCaption(segment.say);
  addTranscript("tutor", segment.say);
}

function hush() {   // Stop, Esc or a new turn: the tutor goes quiet, and the rest of its reply is shown at once
  stopSpeaking();   // (voice.js)
  const reply = speakingReply;
  if (!reply) return;
  while (reply.shown < reply.segments.length) showSegment(reply.segments[reply.shown++]);
  speakingReply = null;
  if (document.activeElement === $("hush")) micButton.focus();   // a keyboard user keeps their place
  $("hush").hidden = true;
}

function cutShort() {   // a recording starts: the tutor goes quiet, and its turn won't hand the controls back
  turnNo += 1;
  hush();
}

// Every segment's audio is requested at once; the segments then play in order, and each one's board
// commands run as its audio starts. A segment without audio in time is read by the browser voice.
async function playReply(reply) {
  setStatus("Speaking…");
  const now = { segments: reply.segments, shown: 0 };
  speakingReply = now;
  $("hush").hidden = false;
  const audios = reply.segments.map((_, i) => fetchAudio(reply.turn_id, i));
  for (const [i, segment] of reply.segments.entries()) {
    if (now.shown > i) continue;   // a stop has shown it already
    const show = () => { now.shown = i + 1; showSegment(segment); };
    if (await playAudio(audios[i], () => { show(); noteFirstAudio(); })) continue;
    if (now.shown > i) continue;   // stopped while its audio was on the way
    show();
    await speakWithBrowser(segment.say);
  }
  if (speakingReply === now) hush();   // spoken to the end: Stop goes away
  audios.forEach((audio) => audio.then((url) => url && URL.revokeObjectURL(url)));
}

function showProblem(e) {
  if (!(e instanceof ApiError)) console.error(e);
  if (e.status === 401) return showSignIn("Please sign in again.");
  if (e.status === 409) {   // no session yet: back to the Start button
    sayForm.hidden = true;
    startButton.hidden = false;
    showChosen();
  }
  const message = e instanceof ApiError ? e.message : "Something went wrong. Please try again.";
  showNote(message);
  if (e.status === 0) speakWithBrowser(message);   // the student may be listening, not looking
}

async function runTurn(send) {   // send() makes the request and resolves to the reply JSON
  const mine = ++turnNo;
  hush();
  setBusy(true);
  setStatus("Thinking…");
  let typeIt = false;
  try {
    const reply = await send();
    drawPanels(reply);
    showChosen();
    drawChoices(reply);
    if (reply.decision.action === "mastered") celebrate();
    setBusy(false);   // while the tutor speaks, every control works, and cuts it short
    await playReply(reply);
    // cut short by a newer turn or a recording, which has the page now (this turn's stop click is spent)
    if (mine !== turnNo) { stopClickedAt = 0; return; }
    typeIt = reply.input_hint === "type";
    setStatus(typeIt ? "Your turn. Typing may be easier." : "Your turn.");
  } catch (e) {
    if (mine !== turnNo) { stopClickedAt = 0; return; }
    showProblem(e);
  }
  stopClickedAt = 0;   // (voice.js) only this turn's reply counts as the "first audio"
  setBusy(false);
  startIdleTimer();   // (voice.js) the student's turn: a kept mic goes off if Speak isn't pressed within a minute
  if (typeIt) textBox.focus();
}

// ---------- sign-in, Start / Continue and the text box ----------
function showSignIn(message = "") {
  hush();
  $("app").hidden = true;
  $("signin").hidden = false;
  $("signin-error").textContent = message;
}

function showTutor(state) {   // after sign-in or a reload: the saved board and panels
  $("signin").hidden = true;
  $("app").hidden = false;
  board = state.board;   // board.js keeps the lines
  drawBoard();
  setCaption("");
  drawPanels(state);
  drawChoices(state);
  startLabel = state.progress.some((row) => row.status !== "not_started") ? "Continue" : "Start";
  startButton.hidden = false;
  sayForm.hidden = true;
  chosen = null;
  showChosen();
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
  hush();
  await postJSON("/api/logout", {}).catch(() => {});
  location.reload();
}

function onStart() {   // the big button: Start / Continue, or the topic chosen in the panel
  unlockSound();   // first, while the click is still being handled
  startButton.hidden = true;
  sayForm.hidden = false;
  if (chosen) return startChosen();
  runTurn(() => postJSON("/api/session", {}));
}

// ---------- topics: a click chooses one, and a Start button opens it ----------
let chosen = null;          // the topic clicked in the panel; null: carry on where the lesson is
let startLabel = "Start";   // the big button's words with no topic chosen: Start, or Continue

function onPickTopic(event) {   // click a topic to choose it; click it again, or the current one, to unchoose
  const id = event.target.closest("button.topic")?.dataset.concept;
  if (!id || event.detail > 1) return;   // the second click of a double-click must not unchoose it
  chosen = id === chosen ? null : id;   // (the current topic is dropped by showChosen)
  showChosen();
}

function showChosen() {   // marks the chosen topic, and names it on the button that opens it
  const title = markChosen(chosen);   // (board.js) null: nothing chosen, or it is (or became) the current topic
  if (!title) chosen = null;
  const label = title ? `Start “${title}”` : startLabel;
  if (sayForm.hidden) {   // before a lesson: the big button
    startButton.textContent = label;
    setStatus(`Press ${label} when you're ready.`);
  }
  topicStart.textContent = label;
  topicStart.hidden = sayForm.hidden || !chosen;   // during a lesson: the panel's Start button
}

function startChosen() {   // the tutor pauses this topic and opens the chosen one
  const concept_id = chosen;
  chosen = null;
  showChosen();
  runTurn(() => postJSON("/api/jump", { concept_id }));
}

function sendText(text) {   // a typed reply, or a choice button's words: the same turn either way
  hush();   // the rest of the tutor's reply goes in the transcript before the student's words
  addTranscript("you", text);
  setCaption(`You: ${text}`, "you");
  runTurn(() => postJSON("/api/turn", { text, prev_first_audio_ms: takeFirstAudioMs() }));
}

function onSend(event) {
  event.preventDefault();
  const text = textBox.value.trim();
  if (!text) return;
  textBox.value = "";
  sendText(text);
}

// ---------- choice buttons ----------
function drawChoices(data) {   // the buttons for the next reply; they sit in the say form, so setBusy turns them off
  $("choices").replaceChildren(...(data.choices || []).map((choice) => {
    const button = make("button", choice.label, "choice");
    button.type = "button";   // not a submit button: a click sends the choice, not the text box
    button.dataset.say = choice.say;
    button.disabled = busy;   // drawn mid-turn: on when the turn ends
    return button;
  }));
}

function onChoice(event) {
  const button = event.target.closest("button.choice");
  if (!button || busy) return;
  sendText(button.dataset.say);
}

$("signin-form").addEventListener("submit", onSignIn);
$("signout").addEventListener("click", onSignOut);
startButton.addEventListener("click", onStart);
$("progress").addEventListener("click", onPickTopic);
topicStart.addEventListener("click", () => { if (!busy && chosen) { unlockSound(); startChosen(); } });
$("hush").addEventListener("click", hush);
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && speakingReply) hush(); });
sayForm.addEventListener("submit", onSend);
$("choices").addEventListener("click", onChoice);
micButton.addEventListener("click", onMic);
if (canSpeak) speechSynthesis.onvoiceschanged = chooseVoice;
if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {   // not https (or localhost): no mic
  micButton.hidden = true;
  $("mic-note").hidden = false;
}
loadPage();
