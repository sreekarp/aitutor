// Sound and the mic: the browser voice, each segment's server audio, the stop-click-to-first-audio timing,
// and recording. The mic stays on between answers, so after the first one Speak starts at once; while the mic
// opens, the Speak button reads "Opening mic…". Each recording is sent with two mic timings (mic_open_ms,
// recording_ms). Loaded after board.js and net.js (request); it calls runTurn, cutShort, sayWait, setStatus,
// showNote, setBusy, addTranscript and setCaption from app.js. Plain JS, no build step.
"use strict";

const MAX_RECORDING_MS = 30000;   // a recording stops by itself after 30 s
const MIC_IDLE_MS = 60000;        // the kept mic goes off if Speak isn't pressed within 1 minute of the student's turn
const DOUBLE_CLICK_MS = 400;      // a Stop click this soon after the recording starts is a double-click: ignored
const AUDIO_WAIT_MS = 3000;       // a segment's audio must start within ~3 s of being due, else the browser voice reads it
const RECORDING_TYPES = ["audio/webm;codecs=opus", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/ogg;codecs=opus"];
const SILENCE = "data:audio/wav;base64,UklGRnQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YVAAAACAgICAgICAgICAgICA" +
  "gICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgA==";   // 10 ms of silence
const micButton = $("mic");

// ---------- browser voice ----------
const canSpeak = "speechSynthesis" in window;
let browserVoice = null;   // an Indian-English voice if the browser has one, else any English voice
let utterance = null;      // keep a reference: Chrome can lose the events of an utterance nobody holds

function chooseVoice() {   // voices load late, so this runs again on "voiceschanged"
  const english = speechSynthesis.getVoices().filter((v) => /^en([-_]|$)/i.test(v.lang));
  browserVoice = english.find((v) => /^en[-_]IN$/i.test(v.lang)) || english[0] || null;
}

let hushed = 0;      // goes up at every stop: speech that began before a stop says nothing more
let endNow = null;   // ends the sentence or segment being spoken right now, if any

function stopSpeaking() {   // silence the tutor at once: the browser voice and the server audio
  hushed += 1;
  if (canSpeak) speechSynthesis.cancel();
  if (player.src !== SILENCE) player.pause();   // (not unlockSound's silence: a click may be unlocking sound)
  if (endNow) endNow();
}

function speakSentence(sentence) {   // resolves true if the voice started speaking
  return new Promise((resolve) => {
    if (!browserVoice) chooseVoice();
    utterance = new SpeechSynthesisUtterance(sentence);
    utterance.lang = browserVoice ? browserVoice.lang : "en-IN";
    if (browserVoice) utterance.voice = browserVoice;
    let started = false;
    const finish = () => {
      clearTimeout(noStart);
      clearTimeout(stuck);
      if (endNow === finish) endNow = null;
      resolve(started);
    };
    const noStart = setTimeout(() => { if (!started) { speechSynthesis.cancel(); finish(); } }, 2000);
    const stuck = setTimeout(finish, 3000 + sentence.length * 120);   // "end" never came
    utterance.onstart = () => { started = true; noteFirstAudio(); };
    utterance.onend = utterance.onerror = finish;
    endNow = finish;   // a stop ends it at once, even in a browser that sends no event on cancel
    speechSynthesis.speak(utterance);
  });
}

async function speakWithBrowser(text) {   // one sentence per utterance: Chrome cuts off long speech
  if (!canSpeak) return false;
  const round = hushed;
  const sentences = text.replace(/([.!?])\s+/g, "$1\n").split("\n").filter((s) => s.trim());
  for (const sentence of sentences) {
    if (hushed !== round) return true;   // stopped: say nothing more
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

function fetchAudio(url) {   // resolves to a blob: URL of that audio (a segment's, or a waiting line's), or null
  return fetch(url)
    .then((res) => (res.ok ? res.blob() : null))
    .then((blob) => (blob ? URL.createObjectURL(blob) : null))
    .catch(() => null);
}

// Plays one segment's audio on the one <audio> element. Resolves true once it has played, or false when it
// failed or hadn't started ~3 s after it was due (then the browser voice reads the segment instead).
function playAudio(audio, onStart) {
  const round = hushed;
  return new Promise((resolve) => {
    let started = false, finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (endNow === finish) endNow = null;
      player.onplaying = player.onpause = player.onended = player.onerror = null;
      if (!started) player.pause();   // too late: it must not talk over the browser voice
      resolve(started);
    };
    const timer = setTimeout(finish, AUDIO_WAIT_MS);
    endNow = finish;   // a stop ends it at once, even while its audio is still on the way
    audio.then((url) => {
      if (!url || finished || hushed !== round) return finish();
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

// ---------- latency: stop click -> first audio ----------
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

// ---------- recording: click to start, click to stop ----------
// After the first answer the mic stays on, so the next Speak records at once. It is switched off if Speak isn't
// pressed within a minute of the student's turn starting, when the page is hidden or left (sign-out reloads it), or
// when its track ends by itself. Nothing is recorded or sent unless Speak is pressed; a hidden page records nothing.
let recorder = null;    // the MediaRecorder while recording
let startedAt = 0;      // performance.now() when that recording really started; 0 until it has
let micStream = null;   // the open mic, kept for the next answer
let idleTimer = 0;      // switches the kept mic off MIC_IDLE_MS after the student's turn starts
let ticker = null;      // Web Audio for the "speak now" tick, made in a Speak click (sound needs a click first)

async function onMic() {
  if (!recorder) cutShort();   // (app.js) Speak cuts the tutor short: its voice must not be recorded
  unlockSound();   // while the click is still being handled
  if (recorder) {   // the Stop click
    if (performance.now() - startedAt < DOUBLE_CLICK_MS) return;   // the second click of a double-click: keep recording
    return stopRecording();
  }
  const clickedAt = performance.now();   // the first-ever click's mic_open_ms includes the browser's permission prompt
  readyTicker();   // while the click is still being handled
  setBusy(true);   // no typing, and no second recording, while recording
  clearTimeout(idleTimer);   // a new recording: the mic stays on
  try {
    if (!micIsLive()) await openMic();   // a kept mic records at once
    if (document.visibilityState === "hidden") return cancelRecording();   // hidden while the mic opened: don't record
    startRecording(micStream, clickedAt);
  } catch (e) {
    releaseMic();
    showMic(false);
    setBusy(false);
    showNote("I can't use the microphone. Allow it in the browser, or type instead.");
  }
}

async function openMic() {   // asks the browser for the mic (the first time, the student must allow it)
  releaseMic();   // let go of a kept mic whose track has ended
  micButton.textContent = "⏳ Opening mic…";   // still disabled: opening the mic can take a second
  setStatus("Opening the microphone… speak after the tick.");
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  stream.getTracks().forEach((track) => {   // mic unplugged or permission taken back: the next Speak opens it again
    track.onended = () => { if (micStream === stream) releaseMic(); };
  });
  micStream = stream;
}

function micIsLive() {   // the kept mic can record only while every track is still live
  return micStream !== null && micStream.getTracks().every((track) => track.readyState === "live");
}

function releaseMic() {   // switch the mic off: stop every track and forget the stream
  clearTimeout(idleTimer);
  if (micStream) micStream.getTracks().forEach((track) => track.stop());
  micStream = null;
}

function afterRecording() {   // keep the mic on for the next answer, except on a touch-first device or a hidden page
  // A touch-first device (any phone or tablet) switches it off, because phones turn the tutor's voice down while the
  // mic is on. With a mouse or trackpad it stays on.
  if (matchMedia("(pointer: coarse)").matches || document.visibilityState === "hidden") releaseMic();
}

function startIdleTimer() {   // the tutor has finished and it's the student's turn: the minute starts now
  clearTimeout(idleTimer);
  if (micStream) idleTimer = setTimeout(releaseMic, MIC_IDLE_MS);
}

function cancelRecording() {   // nothing to send: the mic is switched off and the controls come back
  stopClickedAt = 0;   // no reply follows, so a later typed turn's reply is not timed from this stop
  releaseMic();
  showMic(false);
  setBusy(false);
  setStatus("Your turn.");
}

function onPageHidden() {   // hidden or left: a recording that has started is still sent, then the mic is off
  if (recorder) stopRecording();
  releaseMic();
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") onPageHidden(); });
window.addEventListener("pagehide", onPageHidden);

function startRecording(stream, clickedAt) {
  const mimeType = RECORDING_TYPES.find((type) => MediaRecorder.isTypeSupported(type));   // undefined: browser picks
  const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks = [];
  startedAt = 0;   // until this recording really starts
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.onstart = () => {   // the recording has really started: only now is it safe to talk
    if (recorder !== rec) return;   // stopped already: the page was hidden as it started
    startedAt = performance.now();
    showMic(true);
    setStatus("Listening… speak now, then click Stop.");
    setTimeout(() => { if (recorder === rec) stopRecording(); }, MAX_RECORDING_MS);
    playTick();   // only now is the mic really listening: a word said before this would be cut off
  };
  rec.onstop = () => {
    const byItself = recorder === rec;   // no Stop click: its track ended (mic unplugged or permission taken back)
    if (byItself) { recorder = null; showMic(false); }   // so the next click starts a new recording
    if (!startedAt) return cancelRecording();   // stopped before it really started (the page was hidden): not sent
    const stoppedAt = byItself ? performance.now() : stopClickedAt;
    const micOpenMs = Math.round(startedAt - clickedAt);     // Speak click -> recording started
    const recordingMs = Math.round(stoppedAt - startedAt);   // recording started -> stopped
    afterRecording();
    sendRecording(new Blob(chunks, { type: rec.mimeType || mimeType || "audio/webm" }), micOpenMs, recordingMs);
  };
  rec.start();
  recorder = rec;
}

function readyTicker() {
  try {
    ticker = ticker || new (window.AudioContext || window.webkitAudioContext)();
    ticker.resume().catch(() => {});
  } catch {
    ticker = null;   // no Web Audio: the words on screen still say when to talk
  }
}

function playTick() {   // a soft 80 ms tick that says "speak now"; the status line says it too, so a failure is fine
  if (!ticker) return;
  try {
    if (ticker.state !== "running") ticker.resume().catch(() => {});   // iPhones may pause it as the mic opens
    const tone = ticker.createOscillator(), volume = ticker.createGain(), now = ticker.currentTime;
    tone.frequency.value = 880;
    volume.gain.setValueAtTime(0.2, now);
    volume.gain.exponentialRampToValueAtTime(0.001, now + 0.08);
    tone.connect(volume).connect(ticker.destination);
    tone.start(now);
    tone.stop(now + 0.08);
  } catch {}   // no tick this time
}

function stopRecording() {   // the stop click, 30 s, or the page hidden; onstop then sends it, if it had really started
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

function sendRecording(blob, micOpenMs, recordingMs) {   // the recording and its mic timings, never the mic's name
  const form = new FormData();
  form.append("audio", blob, "recording");
  form.append("mic_open_ms", String(micOpenMs));
  form.append("recording_ms", String(recordingMs));
  const ms = takeFirstAudioMs();
  if (ms !== null) form.append("prev_first_audio_ms", String(ms));
  runTurn(async () => {
    const reply = await request("/api/turn/audio", { method: "POST", body: form }, sayWait);   // (app.js)
    if (reply.transcript) {   // what the mic heard, on the board too: a mishearing is easy to spot
      addTranscript("you", reply.transcript);
      setCaption(`You said: ${reply.transcript}`, "you");
    }
    return reply;
  });
}
