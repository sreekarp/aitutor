// Choosing a topic: a click in the progress panel chooses one, and a Start button opens it. Loaded after board.js
// and voice.js and before app.js, whose runTurn, postJSON, setStatus, busy and the say form it uses once the page
// runs. Plain JS, no build step.
"use strict";

const topicStart = $("topic-start");   // the panel's Start button, during a lesson
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

$("progress").addEventListener("click", onPickTopic);
topicStart.addEventListener("click", () => { if (!busy && chosen) { unlockSound(); startChosen(); } });
