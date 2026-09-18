const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  automatic = false;
const goals = {
  flights: 'Find one-way flights from Zurich to London on September 20, 2026, for one adult in economy. Stop when matching flight options are visible. Do not select or book a flight.',
  travel: 'Find a Design stay in Lisbon with Free cancellation and open Casa Flora.',
  research:
    "Open the article about using finite choices to control browser agents.",
  alchemy: "Steam",
  alchemy_fixture: "Steam",
};
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  state = data;
  render();
  return data;
}
function controls() {
  const live = state?.page && !["done", "blocked", "uncertain"].includes(state.status);
  $("start").disabled = busy;
  $("scenario").disabled = busy;
  $("goal").disabled = busy;
  $("choose").disabled = busy || !live;
  $("execute").disabled = busy || !state?.decision || !live;
  $("auto").disabled = busy || !live;
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("download").disabled = !state?.history?.length;
}
async function perform(fn, label) {
  if (busy) return;
  busy = true;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  try {
    await fn();
  } catch (error) {
    automatic = false;
    try {
      state = await fetch("/api/state").then((r) => r.json());
      render();
    } catch {
      /* Preserve the original failure if the server disconnected. */
    }
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "Paused · needs attention";
  } finally {
    busy = false;
    controls();
  }
}
function render() {
  if (state?.mode === "alchemy") return renderAlchemy();
  return renderGeneric();
}
function renderGeneric() {
  if (!state) return;
  $("overlays").disabled = false;
  $("aside-note").innerHTML = "Operation and target are separate choices in one request.<br />Text is generated only for TYPE_TEXT.";
  $("helper").textContent = `Text helper · ${state.text_model}`;
  $("plan").innerHTML = (state.plan || [])
    .map(
      (goal, i) =>
        `<div class="plan-step ${i === state.plan_index ? "current" : ""}"><span>${i < state.plan_index ? "✓" : i + 1}</span>${escape(goal)}</div>`,
    )
    .join("");
  const page = state.page,
    d =
      state.decision ||
      (state.status === "done" ? state.decisions?.at(-1) : null);
  const labels = {
    idle: "Ready to explore",
    ready: "Page observed · ready for a decision",
    predicted: "Choice ready · inspect or execute",
    done: "Jev reports complete · inspect the page",
    blocked: "Stopped · no supported next action",
  };
  $("status").textContent = labels[state.status] || state.status;
  if (!page) {
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = false;
  $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${state.elements.length} elements`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d
    ? chosen?.label || d.choice
    : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? "Ranked by Jev" : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]);
  $("operation-choices").innerHTML = op.map(([name,p]) =>
    `<span class="operation-choice ${name === d.operation ? 'best' : ''}">${escape(name)} <b>${percent(p)}</b></span>`).join('');
  const probability = e => d?.target_probabilities[e.index] ??
    Math.max(-1, ...(e.options || []).map(o=>d?.target_probabilities[o.index] ?? -1));
  const selectedIndex = d?.target?.split(':')[0];
  const elements = [...state.elements];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $("choices").innerHTML = elements.map(e => {
    const p = probability(e);
    return `<div class="choice ${selectedIndex === e.index ? 'best' : ''}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(' / '))}${e.value ? ' · '+escape(e.value) : ''}${e.checked !== undefined ? ' · checked '+escape(e.checked) : ''}</small>${p >= 0 ? `<div class="bar" style="--probability:${p*100}%"></div>` : ''}</div><span class="probability">${p >= 0 ? percent(p) : '—'}</span></div>`;
  }).join('');
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()].map((a,i) => {
    const index=String(i+1);
    return `<div class="target ${index === selectedIndex ? 'selected' : ''}" data-action="${index}" style="left:${100*a.rect.x/page.w}%;top:${100*a.rect.y/page.h}%;width:${100*a.rect.w/page.w}%;height:${100*a.rect.h/page.h}%"><span>${index}</span></div>`;
  }).join('');
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = state.history.length
    ? state.history
        .map(
          (h) =>
            `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(h.action)}${h.text ? ` <b>“${escape(h.text)}”</b><small>${escape(h.text_helper)}</small>` : ""}</div><span class="time">${h.latency_ms} ms · ${percent(h.probability)}</span><span class="effect">${h.page_changed ? "Page changed" : "No change observed"}</span></div>`,
        )
        .join("")
    : '<p class="muted">Each executed action leaves an observed result.</p>';
  $("step-count").textContent = `${state.history.length} actions · ${(state.elapsed_ms / 1000).toFixed(2)} s`;
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      url: page.url,
      text: page.text,
      actions: page.actions.map(({ rect, node, ...rest }) => rest),
    },
    null,
    2,
  );
  controls();
}
function renderAlchemy() {
  if (!state) return;
  const observation = state.observation || {};
  const page = state.page;
  const d = state.decision || state.last_decision || (state.status === "done" ? state.decisions?.at(-1) : null);
  const inventory = observation.inventory || [];
  const instances = observation.instances || [];
  const itemsById = new Map(
    [...inventory, ...instances].map((item) => [item.id, item]),
  );
  const labels = new Map(
    [...inventory, ...instances].map((item) => [item.id, `${item.emoji || ""} ${item.name || ""}`.trim()]),
  );
  $("overlays").disabled = true;
  $("targets").hidden = true;
  $("helper").textContent = "TypeSafe Jev · semantic alchemy · no TYPE_TEXT";
  $("plan").innerHTML = `<div class="plan-step current"><span>↻</span>Phase · ${escape(state.phase)}</div>${
    state.pending
      ? `<div class="plan-step"><span>•</span>Pending · ${escape(`${state.pending.emoji || ""} ${state.pending.name || ""}`.trim())}</div>`
      : ""
  }`;
  const statusLabels = {
    idle: "Ready to explore",
    ready: "Semantic state observed · ready for a decision",
    predicted: "Element choice ready · inspect or execute",
    done: "Goal independently verified",
    blocked: state.last_error?.includes("model-call budget")
      ? "Stopped · exploration budget reached"
      : "Stopped · Jev found no offered next element",
    uncertain: "Input uncertain · observe before continuing",
    retryable: "TypeSafe is busy · retry available",
  };
  $("status").textContent = statusLabels[state.status] || state.status;
  if (!page) {
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = !page.screenshot;
  if (page.screenshot) $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  const offeredIds = new Set(Object.keys(state.targets?.ADD_ELEMENT || {}));
  const elements = offeredIds.size ? inventory.filter((item) => offeredIds.has(item.id)) : inventory;
  $("action-count").textContent = offeredIds.size && offeredIds.size !== inventory.length
    ? `${offeredIds.size} available · ${inventory.length} total`
    : `${inventory.length} elements`;
  $("choice-title").textContent = d
    ? d.operation === "ADD_ELEMENT"
      ? labels.get(d.target) || d.target
      : d.operation
    : "Choose an element";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d
    ? percent(d.target_confidence ?? d.confidence)
    : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? "Ranked by Jev" : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a, b) => b[1] - a[1]);
  $("operation-choices").innerHTML = op
    .map(
      ([name, p]) =>
        `<span class="operation-choice ${name === d.operation ? "best" : ""}">${escape(name)} <b>${percent(p)}</b></span>`,
    )
    .join("");
  const probability = (item) => d?.target_probabilities?.[item.id] ?? d?.probabilities?.[item.id] ?? -1;
  if (d) elements.sort((a, b) => probability(b) - probability(a));
  $("choices").innerHTML = elements.length
    ? elements
        .map((item) => {
          const p = probability(item);
          const selected = d?.target === item.id;
          return `<div class="choice ${selected ? "best" : ""}" data-action="${escape(item.id)}"><span class="choice-id">${escape(item.emoji || "")}</span><div class="choice-label">${escape(item.name)}<small>${escape(item.id)} · ADD_ELEMENT</small>${p >= 0 ? `<div class="bar" style="--probability:${p * 100}%"></div>` : ""}</div><span class="probability">${p >= 0 ? percent(p) : "—"}</span></div>`;
        })
        .join("")
    : '<p class="muted">No visible inventory elements.</p>';
  const crafts = state.state?.known_results || [];
  const elementLabel = (emoji, name, id) => {
    const observed = itemsById.get(id);
    return `${emoji || observed?.emoji || ""} ${name || observed?.name || id || "?"}`.trim();
  };
  $("history").innerHTML = crafts.length
    ? crafts
        .map((craft, index) => {
          const first = elementLabel(craft.first_emoji, craft.first, craft.first_id);
          const second = elementLabel(craft.second_emoji, craft.second, craft.second_id);
          const result = craft.produced
            ? elementLabel(craft.produced_emoji, craft.produced)
            : "∅";
          const effect = craft.status === "success"
            ? craft.new_discovery
              ? "New discovery"
              : "Verified"
            : "No result";
          return `<div class="trace-row craft-row"><span class="number">${String(index + 1).padStart(2, "0")}</span><div class="craft-line"><span>${escape(first)}</span> <b class="craft-operator">+</b> <span>${escape(second)}</span> <b class="craft-operator">=</b> <span class="craft-result">${escape(result)}</span></div><span class="time">${escape(craft.status || "observed")}</span><span class="effect">${effect}</span></div>`;
        })
        .join("")
    : '<p class="muted">Each pair leaves one independently verified craft.</p>';
  $("step-count").textContent = `${state.model_calls || 0} model requests · ${crafts.length} crafts · ${(state.elapsed_ms / 1000).toFixed(2)} s`;
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      state: state.state,
      targets: state.targets,
      history: state.history?.slice(-10),
    },
    null,
    2,
  );
  $("aside-note").innerHTML = "Jev chooses one observed element at a time.<br />The adapter runs the craft and verifies the result.";
  controls();
}
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  perform(
    () =>
      call("reset", { scenario: $("scenario").value, goal: $("goal").value }),
    "Opening a fresh browser…",
  );
});
$("scenario").addEventListener("change", () => {
  $("goal").value = goals[$("scenario").value];
  const alchemy = $("scenario").value.startsWith("alchemy");
  $("goal-label").textContent = alchemy ? "Target element" : "Give it a task";
  $("goal").placeholder = alchemy ? "Element name, e.g. Human" : "Describe the task";
  $("goal").rows = alchemy ? 1 : 5;
});
$("choose").addEventListener("click", () =>
  perform(() => call("predict"), "Jev is comparing the actions…"),
);
$("execute").addEventListener("click", () =>
  perform(
    () => call("act", { fingerprint: state.page.fingerprint }),
    "Executing the choice…",
  ),
);
$("auto").addEventListener("click", () =>
  perform(async () => {
    automatic = true;
    controls();
    const limit = state.max_model_calls ?? state.max_steps * 2;
    for (let i = 0; i < limit && automatic; i++) {
      $("status").textContent = "Running…";
      if ($("pace").checked) {
        await call("predict");
        await new Promise(resolve => setTimeout(resolve, 450));
        if (!automatic) break;
        await call("act", {fingerprint: state.page.fingerprint});
      } else {
        await call("tick");
      }
      if (["done", "blocked", "retryable", "uncertain"].includes(state.status)) break;
    }
    automatic = false;
  }, "Running the browser…"),
);
$("stop").addEventListener("click", () => {
  automatic = false;
  $("status").textContent = "Pausing after the current request…";
  controls();
});
$("overlays").addEventListener("change", () => {
  if (state?.mode !== "alchemy") $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    );
});
$("choices").addEventListener("pointerleave", () =>
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    ),
);
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob(
    [
      JSON.stringify(
        { ...rest, page: { ...page, screenshot: undefined } },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "typesafe-browser-trace.json";
  a.click();
  URL.revokeObjectURL(url);
});
fetch("/api/state")
  .then((r) => r.json())
  .then((s) => {
    state = s;
    render();
  })
  .catch(() => {
    $("status").textContent = "Cannot reach local demo server";
  });
