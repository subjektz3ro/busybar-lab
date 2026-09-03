"use strict";

const state = {
  scenarios: [],
  sessions: [],
  session: null,
  artifact: null,
  manifest: null,
  poll: null,
  activeJob: null,
  refreshing: false,
  refreshPending: false,
  sessionEpoch: 0,
  lastScenario: null,
  refreshTicks: 0,
};

const $ = (id) => document.getElementById(id);

function eventId() {
  return `evt_${crypto.randomUUID().replaceAll("-", "")}`;
}

async function api(path, options = {}) {
  const request = {
    ...options,
    headers: options.body ? {"content-type": "application/json"} : {},
  };
  let response;
  try {
    response = await fetch(path, request);
  } catch (error) {
    let retryable = false;
    if (options.body) {
      try {
        retryable = Boolean(JSON.parse(options.body).event_id);
      } catch {
        retryable = false;
      }
    }
    if (!retryable) throw error;
    // A response can be lost after the append committed. Retry exactly once
    // with the same serialized body and event id; the API replays it safely.
    response = await fetch(path, request);
  }
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const error = new Error(body.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

let toastTimer;
function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = error ? "show error" : "show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = ""; }, 3000);
}

function selectedScenario() {
  return state.scenarios.find((item) => item.id === $("scenario").value);
}

function renderControls() {
  const scenario = selectedScenario();
  if (state.lastScenario && state.lastScenario !== scenario?.id) {
    $("inputs").value = "[]";
  }
  state.lastScenario = scenario?.id || null;
  $("scenario-description").textContent = scenario?.description || "";
  const host = $("controls");
  host.replaceChildren();
  for (const control of scenario?.controls || []) {
    const label = document.createElement("label");
    label.textContent = control.label;
    let input;
    if (control.choices?.length) {
      input = document.createElement("select");
      for (const choice of control.choices) {
        const option = document.createElement("option");
        option.value = JSON.stringify(choice);
        option.textContent = String(choice);
        if (choice === control.default) option.selected = true;
        input.append(option);
      }
    } else {
      input = document.createElement("input");
      input.type = control.kind === "number" ? "number" : "text";
      if (control.minimum != null) input.min = control.minimum;
      if (control.maximum != null) input.max = control.maximum;
      input.value = control.default;
    }
    input.dataset.control = control.id;
    input.dataset.kind = control.kind;
    label.append(input);
    host.append(label);
  }

  const semantic = $("semantic-inputs");
  const actions = $("input-actions");
  actions.replaceChildren();
  semantic.classList.toggle("hidden", !(scenario?.inputs?.length));
  for (const spec of scenario?.inputs || []) {
    const row = document.createElement("div");
    row.className = "input-action";
    const name = document.createElement("span");
    name.textContent = spec.label;
    const buttons = document.createElement("div");
    buttons.className = "button-row";
    let choices = spec.values || [];
    if (!choices.length && spec.kind === "encoder.delta") choices = [-1, 1];
    if (!choices.length && spec.kind === "button.press") choices = [true];
    if (!choices.length) choices = [true];
    for (const value of choices) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = spec.kind === "encoder.delta"
        ? (Number(value) > 0 ? `+${value}` : String(value))
        : (value === true ? "Press" : String(value));
      button.setAttribute("aria-label", `${spec.label}: ${button.textContent}`);
      button.addEventListener("click", () => appendSemanticInput(spec, value));
      buttons.append(button);
    }
    row.append(name, buttons);
    actions.append(row);
  }
}

function appendSemanticInput(spec, value) {
  let timeline;
  try {
    timeline = JSON.parse($("inputs").value);
    if (!Array.isArray(timeline)) throw new Error();
  } catch {
    toast("Fix the advanced JSON timeline before adding an input", true);
    return;
  }
  const tUs = Number($("input-time").value);
  if (!Number.isInteger(tUs) || tUs < 0 || tUs > 60000000) {
    toast("Virtual timestamp must be an integer from 0 to 60,000,000 µs", true);
    return;
  }
  timeline.push({t_us: tUs, kind: spec.kind, control: spec.id, value});
  timeline.sort((left, right) => left.t_us - right.t_us);
  $("inputs").value = JSON.stringify(timeline, null, 2);
}

function parameters() {
  const values = {};
  for (const input of document.querySelectorAll("[data-control]")) {
    if (input.tagName === "SELECT") {
      values[input.dataset.control] = JSON.parse(input.value);
    } else if (input.dataset.kind === "number") {
      values[input.dataset.control] = Number(input.value);
    } else {
      values[input.dataset.control] = input.value;
    }
  }
  return values;
}

function updateSession(session) {
  state.session = session;
  $("session-title").textContent = session?.title || "No session selected";
  $("session-meta").textContent = session
    ? `${session.id} · revision ${session.revision}${session.current_artifact_id ? ` · artifact ${session.current_artifact_id.slice(0, 12)}` : ""}`
    : "Create a session to keep render requests, feedback, and approvals in one durable event stream.";
  $("render").disabled = !session;
  $("send-feedback").disabled = !session;
  const canReview = Boolean(
    session && state.artifact && state.artifact === session.current_artifact_id
  );
  $("approve").disabled = !canReview;
  $("request-changes").disabled = !canReview;
  $("export").classList.toggle("hidden", !session);
  if (session) $("export").href = `/api/sessions/${session.id}/export.jsonl`;
  if (session && $("session-select").value !== session.id) {
    $("session-select").value = session.id;
  }
  if (session && $("job-state").textContent === "Waiting for a session.") {
    $("job-state").textContent = "Ready for a deterministic render.";
  }
}

function renderSessionOptions(sessions) {
  const selected = state.session?.id || "";
  const visibleSessions = (
    state.session && !sessions.some((item) => item.id === state.session.id)
  ) ? [state.session, ...sessions] : sessions;
  state.sessions = visibleSessions;
  const select = $("session-select");
  select.replaceChildren();
  if (!visibleSessions.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No saved sessions";
    select.append(option);
    return;
  }
  for (const session of visibleSessions) {
    const option = document.createElement("option");
    option.value = session.id;
    option.textContent = `${session.title} · r${session.revision}`;
    select.append(option);
  }
  select.value = visibleSessions.some((item) => item.id === selected)
    ? selected
    : visibleSessions[0].id;
}

async function refreshSession() {
  if (!state.session) return;
  if (state.refreshing) {
    state.refreshPending = true;
    return;
  }
  const requestedSession = state.session.id;
  const requestedEpoch = state.sessionEpoch;
  state.refreshing = true;
  try {
    const sessionBody = await api(`/api/sessions/${requestedSession}`);
    if (
      state.session?.id !== requestedSession
      || state.sessionEpoch !== requestedEpoch
    ) return;
    const session = sessionBody.session;
    const after = Math.max(0, session.revision - 500);
    const eventsBody = await api(
      `/api/sessions/${session.id}/events?after_revision=${after}&limit=500`,
    );
    if (
      state.session?.id !== requestedSession
      || state.sessionEpoch !== requestedEpoch
    ) return;
    updateSession(session);
    renderEvents(eventsBody.events);
    if (session.current_artifact_id && session.current_artifact_id !== state.artifact) {
      $("approve").disabled = true;
      $("request-changes").disabled = true;
      const loaded = await loadArtifact(
        session.current_artifact_id, requestedSession, requestedEpoch,
      );
      if (!loaded) return;
    } else if (!session.current_artifact_id && state.artifact) {
      clearArtifactView("Render or present an artifact for this session.");
    }
    if (session.latest_render_job_id && state.activeJob !== session.latest_render_job_id) {
      try {
        const {job} = await api(`/api/jobs/${session.latest_render_job_id}`);
        if (["queued", "running", "finalizing"].includes(job.state)) {
          pollJob(job.id);
        }
      } catch (error) {
        if (error.status !== 404) throw error;
      }
    }
    state.refreshTicks += 1;
    if (state.refreshTicks % 4 === 0) {
      const listed = await api("/api/sessions");
      renderSessionOptions(listed.sessions);
    }
  } finally {
    state.refreshing = false;
    if (state.refreshPending) {
      state.refreshPending = false;
      queueMicrotask(() => {
        refreshSession().catch((error) => toast(error.message, true));
      });
    }
  }
}

async function periodicRefresh() {
  if (state.session) {
    await refreshSession();
    return;
  }
  if (state.refreshing) return;
  state.refreshing = true;
  let discovered = null;
  try {
    const listed = await api("/api/sessions");
    renderSessionOptions(listed.sessions);
    discovered = listed.sessions[0] || null;
    if (discovered) activateSession(discovered);
  } finally {
    state.refreshing = false;
  }
  if (discovered) await refreshSession();
}

function renderEvents(events) {
  const host = $("events");
  host.replaceChildren();
  for (const event of events.slice().reverse()) {
    const item = document.createElement("li");
    const revision = document.createElement("span");
    revision.className = "event-rev";
    revision.textContent = `r${event.revision}`;
    const detail = document.createElement("span");
    detail.className = "event-detail";
    const kind = document.createElement("code");
    kind.textContent = event.kind;
    detail.append(kind);
    const facts = [`actor ${event.actor}`];
    if (event.artifact_id) facts.push(`artifact ${event.artifact_id}`);
    if (event.body.job_id) facts.push(`job ${event.body.job_id}`);
    if (event.body.request?.scenario_id) {
      facts.push(`scenario ${event.body.request.scenario_id}`);
    }
    if (event.body.status) facts.push(`status ${event.body.status}`);
    if (typeof event.body.promoted === "boolean") {
      facts.push(`promoted ${event.body.promoted ? "yes" : "no"}`);
    }
    if (typeof event.body.passed === "boolean") {
      facts.push(`checks ${event.body.passed ? "passed" : "failed"}`);
    }
    if (event.body.evidence_level) {
      facts.push(`evidence ${event.body.evidence_level}`);
    }
    const fields = document.createElement("span");
    fields.className = "event-fields";
    fields.textContent = facts.join(" · ");
    detail.append(fields);
    const description = event.body.message || event.body.note || "";
    if (description) detail.append(document.createTextNode(` — ${description}`));
    item.append(revision, detail);
    host.append(item);
  }
  if (!events.length) host.innerHTML = '<li class="muted">No events yet.</li>';
}

function previewAsset(display, mode) {
  const base = `/api/artifacts/${state.artifact}/`;
  if (mode === "native") return `${base}${display}.gif`;
  if (mode === "contact") return `${base}${display}-contact-sheet.png`;
  if (mode === "gap-contact") return `${base}${display}-gap-contact-sheet.png`;
  if (mode === "heatmap") return `${base}${display}-change-heatmap.png`;
  return `${base}${display}-gap.gif`;
}

function showPreviews() {
  const host = $("previews");
  if (!state.manifest) return;
  host.classList.remove("empty");
  host.replaceChildren();
  for (const [display, metadata] of Object.entries(state.manifest.displays)) {
    const card = document.createElement("div");
    card.className = "display-preview";
    const image = document.createElement("img");
    image.src = previewAsset(display, $("preview-mode").value);
    image.alt = `${display} display ${$("preview-mode").selectedOptions[0].textContent}`;
    const meta = document.createElement("p");
    meta.className = "display-meta";
    meta.textContent = `${display} · ${metadata.width}×${metadata.height} · ${metadata.frame_count} frames · ${metadata.fps} FPS · ${metadata.confidence}`;
    card.append(image, meta);
    host.append(card);
  }
  const audit = $("audit");
  audit.replaceChildren();
  for (const check of state.manifest.checks || []) {
    const row = document.createElement("div");
    row.className = "audit-check";
    const status = document.createElement("span");
    status.className = `audit-status ${check.status}`;
    status.textContent = check.status;
    const id = document.createElement("code");
    id.textContent = check.id;
    const message = document.createElement("span");
    message.textContent = check.message;
    row.append(status, id, message);
    audit.append(row);
  }
  if (!(state.manifest.checks || []).length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "This artifact declares no automated checks.";
    audit.append(empty);
  }
}

function hydrateControlsFromArtifact(manifest) {
  const request = manifest?.scenario;
  if (!request || typeof request.scenario_id !== "string") return false;
  const scenario = state.scenarios.find((item) => item.id === request.scenario_id);
  if (!scenario) return false;

  $("scenario").value = scenario.id;
  renderControls();
  const artifactParameters = request.parameters || {};
  for (const input of document.querySelectorAll("[data-control]")) {
    if (!Object.hasOwn(artifactParameters, input.dataset.control)) continue;
    const value = artifactParameters[input.dataset.control];
    input.value = input.tagName === "SELECT" ? JSON.stringify(value) : String(value);
  }
  const timeline = Array.isArray(request.inputs) ? request.inputs : [];
  $("inputs").value = JSON.stringify(timeline, null, 2);
  $("input-time").value = timeline.length
    ? String(timeline[timeline.length - 1].t_us)
    : "0";
  return true;
}

async function loadArtifact(artifactId, expectedSessionId, expectedEpoch) {
  const changed = artifactId !== state.artifact;
  let manifest;
  try {
    manifest = await api(`/api/artifacts/${artifactId}/manifest.json`);
  } catch (error) {
    if (
      state.session?.id !== expectedSessionId
      || state.sessionEpoch !== expectedEpoch
    ) return false;
    throw error;
  }
  if (
    state.session?.id !== expectedSessionId
    || state.sessionEpoch !== expectedEpoch
    || state.session.current_artifact_id !== artifactId
  ) return false;
  // Commit the pointer only after its immutable manifest is readable. A
  // transient GET failure must leave the id different from the session's
  // current artifact so the next periodic refresh retries the load.
  state.artifact = artifactId;
  state.manifest = manifest;
  if (changed) $("gap-reviewed").checked = false;
  hydrateControlsFromArtifact(state.manifest);
  $("approve").disabled = false;
  $("request-changes").disabled = false;
  showPreviews();
  $("evidence-note").textContent = state.manifest.passed
    ? "Automated renderer checks passed. Gap simulation still does not prove physical-panel legibility or animation feel."
    : "One or more renderer checks failed. Inspect the audit and frames before approval.";
  return true;
}

function clearArtifactView(message = "Render a scenario to create deterministic, inspectable evidence.") {
  state.artifact = null;
  state.manifest = null;
  $("gap-reviewed").checked = false;
  $("approve").disabled = true;
  $("request-changes").disabled = true;
  $("previews").className = "previews empty";
  $("previews").replaceChildren();
  const previewMessage = document.createElement("p");
  previewMessage.textContent = message;
  $("previews").append(previewMessage);
  $("audit").innerHTML = '<p class="muted">Automated checks appear here with the evidence.</p>';
  $("evidence-note").textContent = "A simulated gap preview is not a physical-panel observation.";
}

function activateSession(session) {
  state.sessionEpoch += 1;
  clearTimeout(state.poll);
  state.poll = null;
  state.activeJob = null;
  clearArtifactView();
  $("job-state").className = "job-state muted";
  $("job-state").textContent = "Ready for a deterministic render.";
  updateSession(session);
}

async function createSession(title) {
  const body = await api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({title, event_id: eventId()}),
  });
  activateSession(body.session);
  const listed = await api("/api/sessions");
  renderSessionOptions(listed.sessions);
  await refreshSession();
  toast("Draft session created");
}

async function requestRender() {
  if (!state.session) return;
  const requestedSession = state.session.id;
  const requestedEpoch = state.sessionEpoch;
  let inputs;
  try { inputs = JSON.parse($("inputs").value); }
  catch { toast("Input timeline must be valid JSON", true); return; }
  const scenario = selectedScenario();
  $("render").disabled = true;
  $("job-state").className = "job-state running";
  $("job-state").textContent = "Queueing deterministic render…";
  try {
    const body = await api(`/api/sessions/${requestedSession}/renders`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: state.session.revision,
        event_id: eventId(),
        request: {
          schema: "busybar.render-request/v1",
          scenario_id: scenario.id,
          parameters: parameters(),
          inputs,
        },
      }),
    });
    if (
      state.session?.id !== requestedSession
      || state.sessionEpoch !== requestedEpoch
    ) return;
    updateSession(body.session);
    if (body.job) {
      pollJob(body.job.id);
    } else {
      $("job-state").textContent = "Request is already durable; refreshing session events…";
      await refreshSession();
    }
  } catch (error) {
    if (
      state.session?.id !== requestedSession
      || state.sessionEpoch !== requestedEpoch
    ) return;
    $("job-state").className = "job-state failure";
    $("job-state").textContent = error.message;
    $("render").disabled = false;
    if (error.status === 409) await refreshSession();
  }
}

function pollJob(
  jobId,
  expectedSessionId = state.session?.id,
  expectedEpoch = state.sessionEpoch,
) {
  clearTimeout(state.poll);
  state.activeJob = jobId;
  const poll = async () => {
    try {
      const {job} = await api(`/api/jobs/${jobId}`);
      if (
        state.session?.id !== expectedSessionId
        || state.sessionEpoch !== expectedEpoch
      ) return;
      $("job-state").textContent = `${job.state}${job.error ? ` · ${job.error}` : ""}`;
      if (["queued", "running", "finalizing"].includes(job.state)) {
        state.poll = setTimeout(poll, 350);
        return;
      }
      state.activeJob = null;
      $("render").disabled = false;
      $("job-state").className = job.state === "succeeded" ? "job-state success" : "job-state failure";
      await refreshSession();
      if (job.state === "succeeded") toast(job.passed ? "Evidence rendered and checks passed" : "Evidence rendered with failed checks", !job.passed);
    } catch (error) {
      if (
        state.session?.id !== expectedSessionId
        || state.sessionEpoch !== expectedEpoch
      ) return;
      state.activeJob = null;
      $("job-state").className = "job-state failure";
      $("job-state").textContent = error.message;
      $("render").disabled = false;
    }
  };
  poll();
}

async function review(kind) {
  if (!state.session) return;
  const reviewedSession = state.session.id;
  const reviewedEpoch = state.sessionEpoch;
  const note = $("feedback").value.trim();
  let path = "feedback";
  let body = {
    expected_revision: state.session.revision,
    event_id: eventId(),
    message: note,
  };
  if (kind !== "feedback") {
    if (
      !state.artifact
      || state.artifact !== state.session.current_artifact_id
    ) {
      toast("Wait for the current artifact preview before recording a decision", true);
      await refreshSession();
      return;
    }
    path = "approval";
    body = {
      expected_revision: state.session.revision,
      event_id: eventId(),
      approved: kind === "approve",
      note,
      artifact_id: state.artifact,
      ...($("gap-reviewed").checked ? {evidence_level: "gap-previewed"} : {}),
    };
  } else if (!note) {
    toast("Write feedback first", true);
    return;
  }
  if (kind === "feedback" && state.artifact) body.artifact_id = state.artifact;
  try {
    const result = await api(`/api/sessions/${reviewedSession}/${path}`, {method: "POST", body: JSON.stringify(body)});
    if (
      state.session?.id !== reviewedSession
      || state.sessionEpoch !== reviewedEpoch
    ) return;
    updateSession(result.session);
    $("feedback").value = "";
    await refreshSession();
    toast(kind === "approve" ? "Artifact approved" : "Review recorded");
  } catch (error) {
    if (
      state.session?.id !== reviewedSession
      || state.sessionEpoch !== reviewedEpoch
    ) return;
    toast(error.message, true);
    if (error.status === 409) await refreshSession();
  }
}

async function initialize() {
  try {
    const [health, scenarioBody, sessionsBody] = await Promise.all([
      api("/api/health"), api("/api/scenarios"), api("/api/sessions"),
    ]);
    $("health").textContent = `${health.scenario_count} scenarios · offline · no device access`;
    $("health").classList.add("ready");
    state.scenarios = scenarioBody.scenarios;
    for (const scenario of state.scenarios) {
      const option = document.createElement("option");
      option.value = scenario.id;
      option.textContent = scenario.title;
      $("scenario").append(option);
    }
    renderControls();
    renderSessionOptions(sessionsBody.sessions);
    if (sessionsBody.sessions.length) {
      activateSession(sessionsBody.sessions[0]);
      await refreshSession();
    }
  } catch (error) {
    $("health").textContent = error.message;
    toast(error.message, true);
  }
}

$("scenario").addEventListener("change", renderControls);
$("session-select").addEventListener("change", async () => {
  const selected = state.sessions.find((item) => item.id === $("session-select").value);
  if (!selected) return;
  activateSession(selected);
  $("previews").innerHTML = "<p>Loading this session’s evidence…</p>";
  await refreshSession();
});
$("preview-mode").addEventListener("change", showPreviews);
$("render").addEventListener("click", requestRender);
$("send-feedback").addEventListener("click", () => review("feedback"));
$("request-changes").addEventListener("click", () => review("changes"));
$("approve").addEventListener("click", () => review("approve"));
$("new-session").addEventListener("click", () => $("session-dialog").showModal());
$("cancel-session").addEventListener("click", () => $("session-dialog").close());
$("session-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = $("new-title").value.trim();
  if (!title) return;
  $("session-dialog").close();
  await createSession(title);
  $("new-title").value = "";
});

initialize();
setInterval(() => {
  periodicRefresh().catch((error) => toast(error.message, true));
}, 1500);
