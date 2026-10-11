// Sound and the mic: the browser voice, each segment's server audio, the stop-click-to-first-audio timing,
// and recording. The mic stays on between answers, so after the first one Speak starts at once; while the mic
// opens, the Speak button reads "Opening mic…". Each recording is sent with two mic timings (mic_open_ms,
// recording_ms). Loaded after board.js; it calls runTurn, request, setStatus, showNote, setBusy and
// addTranscript from app.js. Plain JS, no build step.
"use strict";

const MAX_RECORDING_MS = 30000;   // a recording stops by itself after 30 s
const MIC_IDLE_MS = 60000;        // the mic is switched off 1 minute after the last recording
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
// After the first answer the mic stays on, so the next Speak records at once. It is switched off a minute after the
// last recording, when the page is hidden or left (sign-out reloads it), or when its track ends by itself. Nothing
// is recorded or sent unless Speak is pressed.
// iPhones and iPads switch it off after each recording: Safari plays the tutor's voice very quietly while it's on.
const iPhoneOrIPad = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);   // an iPad that says it's a Mac
let recorder = null;    // the MediaRecorder while recording
let micStream = null;   // the open mic, kept for the next answer
let idleTimer = 0;      // switches the kept mic off MIC_IDLE_MS after the last recording

async function onMic() {
  unlockSound();   // first, while the click is still being handled
  if (recorder) return stopRecording();
  const clickedAt = performance.now();   // the first-ever click's mic_open_ms includes the browser's permission prompt
  setBusy(true);   // no typing, and no second recording, while recording
  clearTimeout(idleTimer);   // a new recording: the mic stays on
  try {
    if (!micIsLive()) await openMic();   // a kept mic records at once
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
  setStatus("Opening the microphone… start talking when it says Listening.");
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

function afterRecording() {   // keep the mic on for the next answer; iPhones, iPads and a hidden page switch it off
  if (iPhoneOrIPad || document.visibilityState === "hidden") return releaseMic();
  idleTimer = setTimeout(releaseMic, MIC_IDLE_MS);
}

function onPageHidden() {   // the page is hidden or left: a recording in progress is still sent, then the mic is off
  if (recorder) stopRecording();
  releaseMic();
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") onPageHidden(); });
window.addEventListener("pagehide", onPageHidden);

function startRecording(stream, clickedAt) {
  const mimeType = RECORDING_TYPES.find((type) => MediaRecorder.isTypeSupported(type));   // undefined: browser picks
  const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks = [];
  let startedAt = 0;   // performance.now() when the recording really started
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.onstart = () => {   // the recording has really started: only now is it safe to talk
    if (recorder !== rec) return;   // stopped already: the page was hidden as it started
    startedAt = performance.now();
    showMic(true);
    setStatus("Listening… speak now, then click Stop.");
    setTimeout(() => { if (recorder === rec) stopRecording(); }, MAX_RECORDING_MS);
  };
  rec.onstop = () => {
    const byItself = recorder === rec;   // no Stop click: its track ended (mic unplugged or permission taken back)
    if (byItself) { recorder = null; showMic(false); }   // so the next click starts a new recording
    const stoppedAt = byItself ? performance.now() : stopClickedAt;
    // -1 if it stopped before it really started: then neither time means anything
    const micOpenMs = startedAt ? Math.round(startedAt - clickedAt) : -1;     // Speak click -> recording started
    const recordingMs = startedAt ? Math.round(stoppedAt - startedAt) : -1;   // recording started -> stopped
    afterRecording();
    sendRecording(new Blob(chunks, { type: rec.mimeType || mimeType || "audio/webm" }), micOpenMs, recordingMs);
  };
  rec.start();
  recorder = rec;
}

function stopRecording() {   // the stop click, 30 s, or the page hidden; onstop then sends the recording
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
  if (micOpenMs >= 0) form.append("mic_open_ms", String(micOpenMs));        // only times that make sense
  if (recordingMs >= 0) form.append("recording_ms", String(recordingMs));
  const ms = takeFirstAudioMs();
  if (ms !== null) form.append("prev_first_audio_ms", String(ms));
  runTurn(async () => {
    const reply = await request("/api/turn/audio", { method: "POST", body: form });
    if (reply.transcript) addTranscript("you", reply.transcript);
    return reply;
  });
}
