// The whiteboard and the two side panels (progress and "Why?"): drawing only, no server calls.
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
