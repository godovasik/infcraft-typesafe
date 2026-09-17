# Handoff: TypeSafe plays an AI alchemy game

## Mission

Build a polished public demo showing an AI browser agent playing an Infinite Craft-style word alchemy game.

The demo should make the loop obvious:

```text
natural-language goal
  -> TypeSafe chooses one observed element
  -> browser performs a drag
  -> the game produces a new element
  -> the agent observes the new state and chooses again
```

The primary visual target is [Infinite Craft](https://neal.fun/infinite-craft/). The project should use the real site for the showcase, while keeping a local deterministic fixture for tests and reproducible development.

## Product decision

Use a thin semantic adapter for the existing game. Do not build a complete replacement game unless the external site becomes unusable or the project deliberately changes scope.

The existing site already provides the visual quality, animation, inventory, persistence, and AI-generated discoveries. A replacement would duplicate all of that and would make the demo less convincing.

## Important observations about the target site

- Inventory elements are DOM nodes such as `.item[data-item-text][data-item-id]`.
- Board elements are DOM nodes such as `#instances .instance` with `.instance-text` and `.instance-emoji`.
- `canvas#particles` is decorative; the game pieces are not painted only into a canvas.
- The cards report `draggable=false`, but the site uses custom pointer/mouse handling.
- A real CDP mouse sequence successfully combined `Water + Fire` into `Steam`.
- The current generic snapshot only sees the link and Search input; it does not expose the item cards as actions. The adapter must add semantic item observation.

## Agent protocol

Keep the user's input as one natural-language goal. Do not provide a hardcoded recipe or a site-specific plan to the model.

Avoid the quadratic pair action space. The model chooses elements one at a time:

1. In `pick_first`, choose one inventory element.
2. Observe the board and create `pending` state.
3. In `pick_second`, choose one inventory element.
4. The adapter drops it on the pending board instance and waits for the result.

Each decision is still one TypeSafe request containing the operation and its operation-specific target. One combination therefore takes two decisions, but each target list is `O(n)`, not `O(n^2)`.

Suggested semantic operation:

```text
ADD_ELEMENT
```

It means “add this observed inventory element to the current crafting interaction.” The browser adapter decides whether that means dropping onto an empty board area or onto the pending instance. The model must not decide coordinates or selectors.

## Model input shape

Send a compact semantic state, not raw DOM, CSS, JavaScript, or a screenshot:

```json
{
  "goal": "Получи Steam",
  "state": {
    "phase": "pick_first",
    "inventory": [
      {"id": "el_water", "name": "Water", "emoji": "💧"},
      {"id": "el_fire", "name": "Fire", "emoji": "🔥"},
      {"id": "el_wind", "name": "Wind", "emoji": "🌬️"},
      {"id": "el_earth", "name": "Earth", "emoji": "🌍"}
    ],
    "pending": null,
    "known_results": [],
    "recent_actions": []
  },
  "targets": {
    "ADD_ELEMENT": {
      "el_water": {"label": "💧 Water"},
      "el_fire": {"label": "🔥 Fire"},
      "el_wind": {"label": "🌬️ Wind"},
      "el_earth": {"label": "🌍 Earth"}
    }
  }
}
```

When the first element has been placed, the next state should contain an observed board instance:

```json
{
  "phase": "pick_second",
  "pending": {
    "instance_id": "inst_17",
    "element_id": "el_water",
    "name": "Water",
    "emoji": "💧"
  }
}
```

Keep observed IDs opaque and server-side mapped to live DOM nodes. Do not deduplicate by name if the game allows combining an element with itself.

## Model response shape

The normalized decision consumed by the executor should be equivalent to:

```json
{
  "operation": "ADD_ELEMENT",
  "target": "el_fire"
}
```

The TypeSafe response may additionally contain confidence and probabilities for the inspector, but only the selected operation and selected target may be executed. Free-form reasoning is not part of the execution contract.

## Browser adapter requirements

- Observe inventory items and board instances from the live page.
- Give every usable element an observed identity and a semantic label.
- Implement a generic CDP drag primitive using mouse press, intermediate moves, and release.
- Resolve source and drop target from observed node identities immediately before input.
- Use the empty board/drop area for the first element and the observed pending instance for the second.
- Log the intended mutation before sending mouse input.
- Never retry a browser mutation. If the page becomes stale before input, observe again; if the outcome is uncertain after input, observe and resolve it.
- Independently verify the result by observing the newly added/changed board or inventory element.
- Treat a model `DONE` choice as a suggestion only; independently verify that the goal is actually present.
- The model must never emit selectors, coordinates, executable code, or arbitrary DOM references.

## Result and history shape

Record observable outcomes, including failures:

```json
{
  "status": "success",
  "selected": "el_fire",
  "pending_before": "el_water",
  "produced": {
    "id": "el_steam",
    "name": "Steam",
    "emoji": "💨"
  },
  "new_discovery": true
}
```

The next observation is authoritative. Keep a bounded recent history and known pair results so the agent does not repeatedly try a confirmed non-result.

## Demo requirements

The public demo should show:

- the natural-language goal;
- the current phase (`pick_first` / `pick_second`);
- the selected element and TypeSafe confidence/probabilities;
- the actual browser drag;
- the produced element and independent verification;
- request count and latency.

Start with a short reliable scenario such as `Water + Fire -> Steam`. Add a longer `Cloud`-style chain only after the two-step flow is reliable.

The README should contain a short video or GIF recorded at natural speed, a link and credit to Infinite Craft, the architecture summary, and a minimal run command. Never put API keys in the repository or in a hosted demo.

The alchemy path should not call `TYPE_TEXT`. If a future scenario requires text entry, use only the configured OpenAI `gpt-5.6-luna` helper; do not add another text provider or model.

## Local fixture and tests

Create a small local HTML fixture with the same semantic concepts and deterministic recipes. It exists for tests, not as the main visual showcase.

Tests must run without paid APIs and should cover:

- semantic inventory/board observation;
- `pick_first -> pick_second -> result` state transitions;
- drag event ordering and observed target resolution;
- stale/covered/disconnected target rejection;
- no mutation retry;
- independent result verification;
- repeated-element combinations if supported;
- invalid model target rejection.

The live Infinite Craft run is a smoke/demo target, not the offline test oracle.

## Implementation plan

### Phase 1 — New repository and baseline

- Create the new repository and copy only the reusable Browser Use/TypeSafe loop.
- Add README, `.env.example`, and this handoff.
- Keep credentials server-side and `.env` ignored.
- Add a local fixture before relying on the live site.

### Phase 2 — Semantic observation

- Extend the page snapshot with inventory elements and board instances.
- Normalize them into the state shape above.
- Confirm that the model receives `O(n)` element targets and no selectors or coordinates.

### Phase 3 — Drag execution

- Add the generic CDP drag primitive.
- Add `ADD_ELEMENT` execution with phase-aware drop targets.
- Run a manual live smoke for `Water + Fire -> Steam`.

### Phase 4 — Sequential agent loop

- Add `pick_first`, `pick_second`, `pending`, and result transitions.
- Make each choice a fresh observe/choose/validate/execute cycle.
- Add independent verification and bounded history.

### Phase 5 — Showcase UI

- Add the phase, decision, action, result, latency, and verification overlays.
- Capture a short real-site demo at normal speed.
- Polish README and add troubleshooting for Chrome/CDP and API keys.

### Phase 6 — Hardening

- Complete fixture-based tests and offline model mocks.
- Run lint, tests, JavaScript checks, and build.
- Re-record the demo only after the final behavior is stable.

## Definition of done

- From a fresh/clean game state, a live run can reach `Steam` by selecting elements one at a time.
- No `n^2` pair list is sent to TypeSafe.
- The model output contains only observed opaque IDs and supported operations.
- Every mutation is logged before execution and is never blindly retried.
- The produced result is independently verified from the page.
- Offline tests make no paid API calls.
- README includes a compelling real-site demo, setup instructions, and no secrets.
