// The tutor's page: sign-in, Start / Continue and the conversation, one turn at a time, typed or spoken.
// board.js (the whiteboard and panels), net.js (talking to the server), voice.js (sound and the mic) and topics.js
// (choosing a topic) load first. Plain JS, no build step.
"use strict";

const TOAST_MS = 3000;            // how long "Topic mastered!" stays up
const startButton = $("start"), sayForm = $("say-form"), textBox = $("text-box");

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
let speakingReply = null;   // the reply being spoken: {turnId, segments, heard and shown (counts), stopped}
let stopSaved = Promise.resolve();   // telling the server where a stop left off: the next turn waits for it
let waitSaid = Promise.resolve();    // the line said while the AI writes the reply: the reply starts after it
const AFTER_STOP = [{ label: "↻ Repeat", say: "repeat" }, { label: "▶ Continue", say: "continue" }];

function showSegment(segment) {
  runBoard(segment.board);
  setCaption(segment.say);
  addTranscript("tutor", segment.say);
}

// Stop, Esc, a reply, a recording or leaving the page: the tutor goes quiet, and the rest of its reply stays unsaid
// and unshown. The server is told how much was said and shown, so "repeat", "continue" or coming back later picks it
// up. True when some was left.
function hush() {
  stopSpeaking();   // (voice.js)
  const reply = speakingReply;
  if (!reply) return false;
  speakingReply = null;
  reply.stopped = true;
  if (document.activeElement === $("hush")) micButton.focus();   // a keyboard user keeps their place
  $("hush").hidden = true;
  if (reply.heard >= reply.segments.length || !reply.turnId) return false;   // all of it was said
  const where = { heard: reply.heard, shown: reply.shown };
  stopSaved = postJSON(`/api/turn/${reply.turnId}/stopped`, where, { keepalive: true }).catch(() => {});
  return true;
}

function sayWait(wait) {   // the AI is writing the reply: a short line meanwhile, in the tutor's voice (net.js)
  setCaption(wait.say);
  addTranscript("tutor", wait.say);
  waitSaid = playAudio(fetchAudio(`/api/tts/wait/${wait.wait}`), noteFirstAudio)
    .then((played) => played || speakWithBrowser(wait.say));
}

function onStop() {   // Stop, Esc or Speak: the reply's own buttons stay, and Repeat and Continue come first
  if (hush()) $("choices").prepend(...AFTER_STOP.map(choiceButton));
}

function cutShort() {   // a recording starts: the tutor goes quiet, and its turn won't hand the controls back
  turnNo += 1;
  onStop();   // Repeat and Continue are there if the mic fails
}

// Every segment's audio is requested at once; the segments then play in order, and each one's board
// commands run as its audio starts. A segment without audio in time is read by the browser voice.
async function playReply(reply) {
  setStatus("Speaking…");
  const now = { turnId: reply.turn_id, segments: reply.segments, heard: 0, shown: 0, stopped: false };
  speakingReply = now;
  $("hush").hidden = false;
  const audios = reply.segments.map((_, i) => fetchAudio(`/api/tts/${reply.turn_id}/${i}`));
  for (const [i, segment] of reply.segments.entries()) {
    const show = () => { now.shown = i + 1; showSegment(segment); };
    const played = await playAudio(audios[i], () => { show(); noteFirstAudio(); });
    if (!played && !now.stopped) {   // no audio in time (and no stop while it was on the way)
      show();
      await speakWithBrowser(segment.say);
    }
    if (now.stopped) break;   // the segment being said at the stop doesn't count as said
    now.heard = i + 1;
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
    await stopSaved;   // "continue" needs the server to know where the stop left off
    const reply = await send();
    await waitSaid;   // a "let me think" line finishes first
    waitSaid = Promise.resolve();
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
  await stopSaved;   // where the tutor was stopped is kept for next time
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

function sendText(text) {   // a typed reply, or a choice button's words: the same turn either way (runTurn hushes)
  addTranscript("you", text);
  setCaption(`You: ${text}`, "you");
  runTurn(() => postJSON("/api/turn", { text, prev_first_audio_ms: takeFirstAudioMs() }, { onWait: sayWait }));
}

function onSend(event) {
  event.preventDefault();
  const text = textBox.value.trim();
  if (!text) return;
  textBox.value = "";
  sendText(text);
}

// ---------- choice buttons ----------
function drawChoices(data) {   // the buttons for the next reply
  $("choices").replaceChildren(...(data.choices || []).map(choiceButton));
}

function choiceButton(choice) {   // it sits in the say form, so setBusy turns it off and on with the text box
  const button = make("button", choice.label, "choice");
  button.type = "button";   // not a submit button: a click sends the choice, not the text box
  button.dataset.say = choice.say;
  button.disabled = busy;   // drawn mid-turn: on when the turn ends
  return button;
}

function onChoice(event) {
  const button = event.target.closest("button.choice");
  if (!button || busy) return;
  sendText(button.dataset.say);
}

$("signin-form").addEventListener("submit", onSignIn);
$("signout").addEventListener("click", onSignOut);
startButton.addEventListener("click", onStart);
$("hush").addEventListener("click", onStop);
addEventListener("pagehide", onStop);   // a reload or a closed tab mid-reply is a stop too (back: Repeat, Continue)
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && speakingReply) onStop(); });
sayForm.addEventListener("submit", onSend);
$("choices").addEventListener("click", onChoice);
micButton.addEventListener("click", onMic);
if (canSpeak) speechSynthesis.onvoiceschanged = chooseVoice;
if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {   // not https (or localhost): no mic
  micButton.hidden = true;
  $("mic-note").hidden = false;
}
loadPage();
