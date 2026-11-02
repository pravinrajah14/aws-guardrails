const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

const STATUS_WORDS = {
  ready: "Ready",
  "needs clarification": "Needs clarification",
  "needs review": "Needs review",
  invalid: "Invalid",
};
const CHIP_WORDS = { running: "running", done: "done", waiting: "needs you", skipped: "skipped", failed: "failed" };
let steps = [];
let current = { sentence: "", answers: {}, picks: {}, transient: {} };
let currentPolicy = null;
let contextExampleIds = new Set();
let busy = false;

async function* readEvents(response) {
  if (!response.ok) throw new Error(`The server answered ${response.status}: ${await response.text()}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) if (line.trim()) yield JSON.parse(line);
  }
}

function setStatus(status, reasons) {
  const box = $("status");
  box.dataset.status = status;
  box.textContent = STATUS_WORDS[status] || (status === "working" ? "Working..." : "No rule yet");
  const show = reasons && reasons.length > 0;
  $("reasons-box").hidden = !show;
  if (show) {
    $("reasons-title").textContent = status === "ready" ? "Why it's ready" : `Why: ${STATUS_WORDS[status].toLowerCase()}`;
    $("reasons").replaceChildren(...reasons.map((r) => el("li", { text: r })));
  }
}

function drawSteps() {
  $("steps").replaceChildren(
    ...steps.map((name) => el("li", { "data-state": "idle", "data-step": name }, el("span", { class: "name", text: name }), el("span", { class: "chip" })))
  );
}

function setStep(name, state) {
  const item = document.querySelector(`[data-step="${name}"]`);
  item.dataset.state = state;
  item.querySelector(".chip").textContent = CHIP_WORDS[state] || "";
}

function clearResult() {
  for (const id of ["reading", "questions", "reasons-box", "summary-box", "code-box", "validate-box", "explain-box", "context-box"]) $(id).hidden = true;
  $("answer-error").hidden = true;
  drawSteps();
}

function drawMarked(sentence, marks) {
  const parts = [];
  let at = 0;
  for (const m of marks) {
    if (m.start > at) parts.push(sentence.slice(at, m.start));
    parts.push(el("span", { class: `mark ${m.source}`, title: m.note || m.source, text: sentence.slice(m.start, m.end) }));
    at = m.end;
  }
  parts.push(sentence.slice(at));
  $("marked").replaceChildren(...parts);
}

function drawUnderstood(event) {
  const u = event.understood;
  $("reading").hidden = false;
  drawMarked(current.sentence, event.highlights);
  const pieces = [];
  if (u.rule_type) {
    pieces.push(`Rule type: ${u.rule_type}.`);
    for (const [name, slot] of Object.entries(u.slots)) {
      if (slot.source !== "missing") {
        const value = Array.isArray(slot.value) ? slot.value.join(", ") : slot.value;
        pieces.push(`${name} = ${value}${slot.direction_text ? ` (${slot.direction_text})` : ""}${slot.evidence ? `, ${slot.evidence}` : ""}.`);
      }
    }
  } else {
    pieces.push("Not one of the rule types this tool can write.");
  }
  if (u.readings) pieces.push(`The model read: ${u.readings.model} Code read: ${u.readings.code}.`);
  $("reading-line").textContent = pieces.join(" ");
}

function withMarks(text, spans) {
  const nodes = [];
  let at = 0;
  for (const [start, end] of spans) {
    nodes.push(text.slice(at, start), el("mark", { class: "shared", text: text.slice(start, end) }));
    at = end;
  }
  nodes.push(text.slice(at));
  return nodes;
}

const contextRowId = (id) => `context-example-${id}`;

function policyToggle(e) {
  const toggle = el("details", {}, el("summary", { text: "Show its policy" }),
    el("p", { class: "source-note", text: "For reference only; not copied into the new policy." }),
    e.policy ? el("pre", { text: e.policy }) : el("p", { text: `No ${e.policy_kind} text was written for it.` }));
  if (e.notes.length) toggle.append(el("p", { text: "Jack's notes:" }), el("ul", {}, ...e.notes.map((n) => el("li", { text: n }))));
  return toggle;
}

function similarRulesTable(examples, sentence) {
  const rows = examples.map((e) => el("tr", { id: contextRowId(e.id) },
    el("td", {},
      el("div", { class: "source", text: `${e.id}, ${e.source}` }),
      el("div", {}, ...withMarks(e.sentence, e.sentence_marks)),
      el("div", { class: "source" }, "Input: ", ...withMarks(sentence, e.input_marks))),
    el("td", { text: e.unsupported_reason === null ? e.type : `unsupported: ${e.unsupported_reason}` }),
    el("td", { text: e.weight }),
    el("td", {}, el("ul", {}, ...e.used_for.map((u) => el("li", { text: u })))),
    el("td", {}, policyToggle(e))));
  return [
    el("h3", { text: "Similar rules used as context" }),
    el("table", { class: "evidence" },
      el("thead", {}, el("tr", {}, ...["Example", "Rule type", "Weight", "Used for", "Policy"].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...rows)),
    el("p", { class: "source-note", text: "These rules help pick the rule type and suggest missing values. The new policy is built from the rule type's template by the converter." }),
  ];
}

function drawContext(context) {
  const group = (title, items) => (items.length ? [el("h3", { text: title }), el("ul", {}, ...items)] : []);
  const row = (main, source) => el("li", {}, main + " ", el("span", { class: "source", text: `(${source})` }));
  contextExampleIds = new Set(context.examples.map((e) => e.id));
  $("context").replaceChildren(
    ...group("Schema fields", context.schema.map((t) => row(`${t.tool}: ${t.fields_used.join(", ")}`, t.source))),
    ...group("Glossary", context.glossary.map((g) => row(`"${g.text}" means ${g.field} = ${g.value}`, g.source))),
    ...(context.examples.length ? similarRulesTable(context.examples, current.sentence) : []),
    ...group("Saved answers", context.saved_answers.map((a) => row(`${a.field}, "${a.term}" = ${a.value}`, `${a.answered_by}, ${a.answered_at}`)))
  );
  $("context-box").hidden = false;
}

function answer(field, value, picking) {
  const target = picking ? current.picks : field === "order_id" ? current.transient : current.answers;
  target[field] = value;
  submitRule();
}

function evidenceTable(evidence) {
  const link = (e) => {
    if (!contextExampleIds.has(e.id)) return e.id;
    const a = el("a", { href: `#${contextRowId(e.id)}` }, e.id);
    a.addEventListener("click", () => { $("context-box").open = true; });
    return a;
  };
  const rows = evidence.map((e) => el("tr", {}, el("td", { text: e.value }), el("td", { text: e.weight }), el("td", { text: e.sentence }), el("td", { text: e.note || "" }), el("td", {}, link(e), `, ${e.source}`)));
  return el("table", { class: "evidence" },
    el("thead", {}, el("tr", {}, ...["Value", "Weight", "Example", "Note", "Source"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...rows));
}

function drawQuestion(q) {
  const box = el("div", { class: "question" }, el("p", { text: q.question }));
  if (q.proposal !== null) box.append(el("p", {}, "Proposed from these examples, if you confirm: ", el("strong", { text: q.proposal })));
  if (q.examples_disagree) box.append(el("p", { text: "The examples disagree, so nothing is proposed." }));
  if (q.evidence.length) box.append(evidenceTable(q.evidence));
  if (q.field === null) return box;
  const form = el("form");
  if (q.options) {
    for (const option of q.options) {
      const button = el("button", { type: "button", class: "secondary", text: option });
      button.addEventListener("click", () => answer(q.field, option));
      form.append(button);
    }
  } else {
    if (q.proposal !== null) {
      const accept = el("button", { type: "button", class: "primary", text: `Use ${q.proposal}` });
      accept.addEventListener("click", () => answer(q.field, String(q.proposal)));
      form.append(accept);
    }
    const input = el("input", { type: "text", "aria-label": `Answer for ${q.field}`, placeholder: q.field === "window" ? "e.g. 30 minutes" : "" });
    const save = el("button", { class: "secondary", text: "Save answer" });
    form.append(input, save);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (input.value.trim()) answer(q.field, input.value.trim());
    });
  }
  box.append(form);
  return box;
}

function drawConflict(conflict) {
  const box = el("div", { class: "question" }, el("p", { text: `Saved answers disagree for "${conflict.term}". Pick the one to keep.` }));
  const form = el("form");
  for (const row of conflict.rows) {
    const button = el("button", { type: "button", class: "secondary", text: `${row.value} (${row.answered_by}, ${row.answered_at})` });
    button.addEventListener("click", () => answer(conflict.field, row.value, true));
    form.append(button);
  }
  box.append(form);
  return box;
}

function drawQuestions(event) {
  const items = [...event.questions.map(drawQuestion), ...event.conflicts.map(drawConflict)];
  $("questions").hidden = items.length === 0;
  $("question-list").replaceChildren(...items);
  if (event.limit_scope && event.limit_scope.answer) {
    $("reading-line").textContent += ` The amount limits ${event.limit_scope.answer}, ${event.limit_scope.evidence}.`;
  }
}

function confirmButton(says, expect, events) {
  const button = el("button", { type: "button", class: "secondary", text: "Confirm" });
  button.addEventListener("click", async () => {
    button.disabled = true;
    const response = await postJson("/api/confirm", { policy_id: currentPolicy.id, says, expect, events });
    drawSavedCases(response.saved_cases);
    button.textContent = "Saved as a test";
  });
  return button;
}

function exampleTable(examples, confirmable) {
  const rows = examples.map((e) => el("tr", {},
    el("td", { text: e.says }),
    el("td", {}, el("span", { class: `verdict ${e.got}`, text: e.got })),
    el("td", { class: e.agrees ? "" : "off", text: e.agrees ? "agrees" : `summary says ${e.expect}` }),
    ...(confirmable ? [el("td", {}, confirmButton(e.says, e.got, e.events))] : [])));
  const heads = ["Example", "Dogwood", "With the summary", ...(confirmable ? ["Right?"] : [])];
  return el("table", { class: "evidence" },
    el("thead", {}, el("tr", {}, ...heads.map((h) => el("th", { text: h })))),
    el("tbody", {}, ...rows));
}

async function postJson(url, body) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `The server answered ${response.status}`);
  return data;
}

function drawSavedCases(cases) {
  const box = $("saved-cases");
  if (!cases.length) {
    box.replaceChildren(el("p", { class: "empty", text: "None yet. Confirm an edge example or a difference to keep it as a test; it reruns whenever this rule's policy changes." }));
    return;
  }
  const rows = cases.map((c) => el("tr", {},
    el("td", { text: c.says }),
    el("td", { text: c.expect }),
    el("td", {}, el("span", { class: `verdict ${c.got}`, text: c.got })),
    el("td", { class: c.passed ? "" : "off", text: c.passed ? "passes" : "fails" })));
  box.replaceChildren(el("table", { class: "evidence" },
    el("thead", {}, el("tr", {}, ...["Case", "Should be", "Dogwood", "Result"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...rows)));
}

function verdictLine(label, verdict) {
  return el("p", { class: "verdict-line" }, `${label}: `, el("span", { class: `verdict ${verdict}`, text: verdict }));
}

function drawDifference(result, bug) {
  const difference = result.difference;
  const box = $("reference-result");
  const fix = el("button", { type: "button", class: "primary", text: "Fix from the reference" });
  fix.addEventListener("click", () => applyFix(bug));
  box.replaceChildren(
    el("p", { text: bug ? "With the bug loaded, this policy says:" : "This policy says:" }),
    el("p", { class: "summary", text: result.summary }),
    el("p", { text: "The reference says:" }),
    el("p", { class: "summary", text: result.reference_summary }),
    el("h3", { text: "The shortest history where they differ" }),
    el("p", { class: "summary", text: difference.story }),
    verdictLine("Dogwood under this policy", difference.policy.verdict),
    verdictLine("Dogwood under the reference", difference.reference.verdict),
    el("div", { class: "row" }, fix, confirmButton(difference.story, difference.reference.verdict, difference.case.events)),
    el("p", { class: "source-note", text: `Reference: ${result.reference} (${result.reference_source})` })
  );
}

function drawFix(result) {
  const side = (title, part) => el("div", {},
    el("h3", { text: title }),
    verdictLine("Dogwood on that history", part.verdict),
    el("p", { text: part.summary }),
    el("pre", { text: part.dogwood }));
  $("reference-result").replaceChildren(
    el("h3", { text: "That history" }),
    el("p", { class: "summary", text: result.story }),
    el("div", { class: "before-after" }, side("Before", result.before), side("After", result.after)),
    el("p", { text: `Dogwood validate after the fix: ${result.validate_output}` }),
    el("p", { text: result.recheck_same ? "Recheck: no history tells the fixed policy apart from the reference." : "Recheck: the fixed policy still differs." })
  );
  setStatus(result.status.status, []);
  return loadPolicy(result.policy_id);
}

async function runCheck(bug) {
  $("reference-progress").textContent = "Searching for a history where they differ...";
  $("reference-result").replaceChildren();
  try {
    const result = await postJson("/api/reference", { policy_id: currentPolicy.id, bug });
    $("reference-progress").textContent = result.message || "";
    if (result.difference) drawDifference(result, bug);
  } catch (error) {
    $("reference-progress").textContent = error.message;
  }
}

async function applyFix(bug) {
  $("reference-progress").textContent = "Fixing from the reference and rechecking...";
  try {
    await drawFix(await postJson("/api/fix", { policy_id: currentPolicy.id, bug }));
    $("reference-progress").textContent = "";
  } catch (error) {
    $("reference-progress").textContent = error.message;
  }
}

async function replayPlanted() {
  const { rows } = await (await fetch("/api/planted")).json();
  const table = el("table", { class: "evidence" },
    el("thead", {}, el("tr", {}, ...["Planted error", "Correct policy", "Broken policy", "Caught"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...rows.map((r) => el("tr", {},
      el("td", { text: `${r.policy}: ${r.label}` }),
      el("td", {}, el("span", { class: `verdict ${r.intended}`, text: r.intended })),
      el("td", {}, el("span", { class: `verdict ${r.broken}`, text: r.broken })),
      el("td", { class: r.caught ? "" : "off", text: r.caught ? "yes" : "no" })))));
  $("planted").replaceChildren(table);
}

function handleStep(event) {
  setStep(event.step, event.state);
  if (event.step === "Understand" && event.state === "done") drawUnderstood(event);
  if (event.step === "Context") drawContext(event.context);
  if (event.step === "Fill or ask") drawQuestions(event);
  if (event.step === "Summary" && event.state === "done") {
    $("summary-box").hidden = false;
    $("summary").textContent = event.summary;
  }
  if (event.step === "Generate" && event.state === "done") {
    $("code-box").hidden = false;
    $("code").textContent = event.dogwood;
  }
  if (event.step === "Validate" && event.output) {
    $("validate-box").hidden = false;
    $("validate").textContent = event.output;
  }
  if (event.step === "Explain" && event.state === "done") {
    $("explain-box").hidden = false;
    $("literal").textContent = event.literal;
    $("examples").replaceChildren(exampleTable(event.examples, false));
  }
  if (event.step === "Status") {
    setStatus(event.status, event.reasons);
    if (event.status === "ready") loadPolicy(event.policy);
  }
}

function setBusy(on) {
  busy = on;
  $("go").disabled = on;
  $("run").disabled = on;
  $("reset").disabled = on;
  $("reset-yes").disabled = on;
}

async function submitRule() {
  if (busy) return;
  setBusy(true);
  clearResult();
  setStatus("working");
  $("progress").textContent = "Reading the rule...";
  const started = Date.now();
  const clock = setInterval(() => {
    $("progress").textContent = `The local model is reading the rule: ${Math.round((Date.now() - started) / 1000)}s`;
  }, 1000);
  const body = { sentence: current.sentence, answers: { ...current.answers, ...current.transient }, picks: current.picks };
  current.answers = {};
  current.picks = {};
  try {
    const response = await fetch("/api/policy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    for await (const event of readEvents(response)) {
      if (event.type === "step") handleStep(event);
      else if (event.type === "waiting") $("progress").textContent = "Waiting for another model request to finish...";
      else if (event.type === "answer_error") {
        $("answer-error").textContent = `That answer wasn't saved: ${event.message}`;
        $("answer-error").hidden = false;
      } else if (event.type === "error") throw new Error(event.message);
    }
    if ($("status").dataset.status === "working") throw new Error("The server stopped before finishing. Its terminal has the details.");
    $("progress").textContent = `Took ${Math.round((Date.now() - started) / 1000)}s`;
  } catch (error) {
    setStatus("none");
    $("answer-error").textContent = error.message;
    $("answer-error").hidden = false;
    $("progress").textContent = "";
  } finally {
    clearInterval(clock);
    setBusy(false);
  }
}

async function loadPolicy(id) {
  const response = await fetch(`/api/policy/${id}`);
  currentPolicy = await response.json();
  $("no-policy").hidden = true;
  $("policy-view").hidden = false;
  $("check-summary").textContent = currentPolicy.summary;
  $("check-code").textContent = currentPolicy.dogwood;
  $("check-examples").replaceChildren(exampleTable(currentPolicy.examples, true));
  if (!currentPolicy.has_reference) $("check-examples").prepend(el("p", { text: "This rule has no reference. Please confirm each example that looks right." }));
  drawSavedCases(currentPolicy.saved_cases || []);
  $("bug").replaceChildren(...currentPolicy.bugs.map((b) => el("option", { value: b.id, text: b.label })));
  $("load-bug").disabled = !currentPolicy.bugs.length;
  $("past-runs").replaceChildren(...currentPolicy.runs.map((r) => el("li", {}, el("a", { href: `/logs/${currentPolicy.id}/${r.session}/timeline.txt`, target: "_blank", rel: "noopener", text: r.session }))));
  if (!currentPolicy.runs.length) $("past-runs").append(el("li", { text: "None yet" }));
}

function showResetConfirm(on) {
  $("reset").hidden = on;
  $("reset-confirm").hidden = !on;
  if (on) $("reset-result").textContent = "";
}

async function resetAnswers() {
  if (busy) return;
  try {
    const { cleared, backup } = await postJson("/api/answers/reset", {});
    $("reset-result").textContent = backup ? `Cleared ${cleared}. Backup: ${backup}` : "There were no saved answers.";
    clearResult();
    setStatus("none");
    $("progress").textContent = "";
    currentPolicy = null;
    $("no-policy").hidden = false;
    $("policy-view").hidden = true;
  } catch (error) {
    $("reset-result").textContent = error.message;
  }
  showResetConfirm(false);
}

function selectTab(name) {
  for (const tab of ["write", "check"]) {
    $(`tab-${tab}`).setAttribute("aria-selected", tab === name);
    $(tab).hidden = tab !== name;
  }
}

function showTaskPrompt(tasks) {
  const task = tasks.find((t) => t.id === $("task").value);
  $("task-prompt").textContent = task.prompt;
  document.querySelector(`input[name="approval"][value="${task.approval || "yes"}"]`).checked = true;
  if (task.different_amount) $("different").value = task.different_amount;
}

function describeAgentEvent(event) {
  const d = event.data;
  if (event.kind === "policy_decision") {
    const detail = [d.note, d.rules && d.rules.length ? `rules: ${d.rules.join(", ")}` : ""].filter(Boolean).join("; ");
    return { title: `${d.tool}(${Object.entries(d.args).map(([k, v]) => `${k}=${v}`).join(", ")})`, verdict: d.decision, detail };
  }
  if (event.kind === "facts") return { title: "Engine facts", detail: `${d.status}: ${event.summary}` };
  const titles = { user_task: "Task", policy_validate: "Dogwood validate", model_turn: "Model", tool_result: "Tool result", tool_error: "Tool error", blocked: "Not run", agent_final: "Agent's answer", nudge: "Note to the agent", claim_nudge: "Note to the agent", empty_reply: "Malformed reply", bad_call: "Bad call", claim_ignored: "Claim ignored", needs_review: "Flagged for a person" };
  return { title: titles[event.kind] || event.kind, detail: event.summary };
}

function addAgentStep(event) {
  const shown = describeAgentEvent(event);
  const item = el("li", { class: shown.verdict || "" }, el("span", { class: "kind", text: shown.title }));
  if (shown.verdict) item.append(el("span", { class: `verdict ${shown.verdict}`, text: shown.verdict }));
  if (shown.detail) item.append(el("span", { class: "detail", text: shown.detail }));
  $("run-steps").append(item);
}

function drawEndState(event) {
  $("end-state").hidden = false;
  if (!event.validated) {
    $("state").replaceChildren(el("p", { class: "off", text: `The policy did not validate: ${event.output}` }));
  } else {
    const refunds = event.state.refunds.map((r) => `$${r.amount} on order ${r.order_id} (${r.refund_id})`);
    const approvals = event.state.approvals.map((a) => (a.approved ? `$${a.approved_amount} approved on order ${a.order_id} by ${a.approver}` : `Refused on order ${a.order_id} by ${a.approver}`));
    const deployed = Object.entries(event.state.deployed).map(([k, v]) => `${k} is at ${v}`);
    const list = (title, items) => [el("h3", { text: title }), items.length ? el("ul", {}, ...items.map((t) => el("li", { text: t }))) : el("p", { text: "None" })];
    $("state").replaceChildren(...list("Refunds issued", refunds), ...list("Manager's answers", approvals), ...list("Deployed versions", deployed));
  }
  $("log-link").href = `/logs/${event.policy_id}/${event.session}/timeline.txt`;
}

async function runAgent(e) {
  e.preventDefault();
  if (busy || !currentPolicy) return;
  setBusy(true);
  $("run-steps").replaceChildren();
  $("end-state").hidden = true;
  const approval = document.querySelector('input[name="approval"]:checked').value;
  const body = { policy_id: currentPolicy.id, task_id: $("task").value, approval, different_amount: approval === "different" ? Number($("different").value) : null };
  const started = Date.now();
  const clock = setInterval(() => {
    $("run-progress").textContent = `The agent is working: ${Math.round((Date.now() - started) / 1000)}s`;
  }, 1000);
  try {
    const response = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    for await (const event of readEvents(response)) {
      if (event.type === "agent") addAgentStep(event);
      else if (event.type === "finished") drawEndState(event);
      else if (event.type === "waiting") $("run-progress").textContent = "Waiting for another model request to finish...";
      else if (event.type === "error") throw new Error(event.message);
    }
    $("run-progress").textContent = `Finished in ${Math.round((Date.now() - started) / 1000)}s`;
    await loadPolicy(currentPolicy.id);
  } catch (error) {
    $("run-progress").textContent = error.message;
  } finally {
    clearInterval(clock);
    setBusy(false);
  }
}

async function start() {
  const setup = await (await fetch("/api/setup")).json();
  steps = setup.steps;
  drawSteps();
  for (const sentence of setup.demo_sentences) {
    const button = el("button", { type: "button", text: sentence });
    button.addEventListener("click", () => {
      $("sentence").value = sentence;
      current = { sentence, answers: {}, picks: {}, transient: {} };
      submitRule();
    });
    $("demos").append(button);
  }
  $("task").append(...setup.tasks.map((t) => el("option", { value: t.id, text: t.id })));
  $("task").addEventListener("change", () => showTaskPrompt(setup.tasks));
  showTaskPrompt(setup.tasks);
  $("sentence-form").addEventListener("submit", (e) => {
    e.preventDefault();
    current = { sentence: $("sentence").value.trim(), answers: {}, picks: {}, transient: {} };
    submitRule();
  });
  $("run-form").addEventListener("submit", runAgent);
  $("reference-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runCheck(null);
  });
  $("load-bug").addEventListener("click", () => runCheck($("bug").value));
  $("planted-run").addEventListener("click", replayPlanted);
  $("reset").addEventListener("click", () => showResetConfirm(true));
  $("reset-no").addEventListener("click", () => showResetConfirm(false));
  $("reset-yes").addEventListener("click", resetAnswers);
  $("tab-write").addEventListener("click", () => selectTab("write"));
  $("tab-check").addEventListener("click", () => selectTab("check"));
}

start();
