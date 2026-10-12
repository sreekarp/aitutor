// The whiteboard and the two side panels (topics and "Why?"): drawing only, no server calls.
// Loaded first: voice.js and app.js use its $ and make. Plain JS, no build step.
"use strict";

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
const boardEl = $("board");

function make(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}

// ---------- whiteboard ----------
let board = [];   // the lines on the board, kept like the server's: [{id, latex, formula, text}]

// Draws a line's LaTeX. With `part`, that exact snippet is highlighted; if KaTeX can't draw the
// snippet on its own, the whole line is highlighted instead.
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

// ---------- topics (progress) and "Why?" panels ----------
// Each topic is a button: picking one pauses the current topic (app.js sends it). ✓ mastered, ⚠ needs review,
// ⏸ paused, ▶ current, · not started; the dots are the points towards mastery (● one, ◐ half, ○ none).
function topicRow(row) {
  const icon = { mastered: "✓", needs_review: "⚠", paused: "⏸" }[row.status] || (row.current ? "▶" : "·");
  const button = make("button", "", "topic");
  button.type = "button";
  button.dataset.concept = row.concept_id;
  if (row.current) button.setAttribute("aria-current", "step");
  const dot = (n) => (row.points >= n ? "●" : row.points >= n - 0.5 ? "◐" : "○");
  const dots = Array.from({ length: POINTS_TO_MASTER }, (_, i) => dot(i + 1)).join("");
  button.append(make("span", icon, "icon"), make("span", row.title, "name"), make("span", dots, "dots"));
  const detail = topicDetail(row);
  if (detail) button.append(make("small", detail));
  const li = make("li", "", row.current ? "current" : row.status);
  li.append(button);
  return li;
}

function topicDetail(row) {   // the small line under a topic: where it stands
  if (row.current && row.status === "checking") {
    return `hint ${row.hint_level}/${MAX_HINTS} · reteach ${row.reteach_count}/${MAX_RETEACHES}`;
  }
  const step = Number.isInteger(row.step_index) ? `step ${row.step_index + 1} of ${row.step_count}` : "";
  if (row.status === "paused") return `saved at ${step || "a question"}`;
  return row.current ? step : "";
}

function drawPanels(data) {
  const current = data.progress.find((row) => row.current);
  $("concept").textContent = current ? current.title : "All concepts done";
  $("progress").replaceChildren(...data.progress.map(topicRow));
  $("why").replaceChildren(...data.why.slice(0, 5).map((decision) => {
    const li = make("li");
    li.append(make("b", decision.rule), decision.reason);
    return li;
  }));
}
