"""Semantic Infinite Craft observation and one-way drag execution."""

import hashlib
import json
import logging
import re
import time
from pathlib import Path

from browser_harness.helpers import cdp

from .browser import Browser as CdpBrowser
from .browser import StalePage
from .model import TransientModelError, choose_alchemy
from .questions import ALCHEMY_HISTORY_LIMIT, ALCHEMY_MAX_MODEL_CALLS

READ_STATE = Path(__file__).with_name("alchemy_snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
LOG = logging.getLogger(__name__)


class AlchemyVerificationError(ValueError):
    """The page changed, but the expected alchemy outcome was not observed."""


class DragInputError(RuntimeError):
    """Input was interrupted after the drag may already have reached the page."""


def fingerprint(observation):
    """Hash semantic page state, never screenshots or raw DOM."""

    content = {
        key: observation.get(key)
        for key in ("url", "title", "inventory", "instances", "drop_zone", "busy")
    }
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _label(item):
    return item.get("label") or f"{item.get('emoji', '')} {item.get('name', '')}".strip()


def normalize_alchemy_goal(goal):
    """Accept a bare element name while remaining compatible with ``Get Steam`` examples."""

    value = " ".join(str(goal).strip().split())
    value = re.sub(
        r"^(?:get|make|create|obtain|produce|получи|получить|сделай|создай)\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value.strip(" .!?\u3002")


def exploration_summary(inventory, known_results):
    """Summarize search coverage without sending a quadratic pair list to Jev."""

    inventory_ids = {item.get("id") for item in inventory if item.get("id")}
    possible = len(inventory_ids) * (len(inventory_ids) + 1) // 2
    attempted_pairs = set()
    successful = 0
    failed = 0
    for result in known_results:
        first_id, second_id = result.get("first_id"), result.get("second_id")
        if first_id and second_id:
            attempted_pairs.add(tuple(sorted((first_id, second_id))))
        if result.get("status") == "success":
            successful += 1
        elif result.get("status") == "no_result":
            failed += 1
    return {
        "attempted_combinations": len(attempted_pairs),
        "available_combinations": possible,
        "unexplored_combinations": max(possible - len(attempted_pairs), 0),
        "successful_combinations": successful,
        "failed_combinations": failed,
    }


def semantic_state(observation, *, phase, pending=None, known_results=None, recent_actions=None):
    """Build the compact state sent to TypeSafe."""

    if phase not in {"pick_first", "pick_second"}:
        raise ValueError("Unknown alchemy phase")
    all_results = list(known_results or [])
    return {
        "phase": phase,
        "inventory": [
            {key: item.get(key, "") for key in ("id", "name", "emoji")}
            for item in observation.get("inventory", [])
        ],
        "pending": pending,
        "busy": bool(observation.get("busy", False)),
        "known_results": all_results[-ALCHEMY_HISTORY_LIMIT:],
        "exploration": exploration_summary(observation.get("inventory", []), all_results),
        "recent_actions": list(recent_actions or [])[-10:],
    }


def action_targets(observation):
    """Return operation-specific opaque targets, with no selectors or coordinates."""

    return {
        "ADD_ELEMENT": {
            item["id"]: {"label": _label(item)}
            for item in observation.get("inventory", [])
            if item.get("id") and item.get("name")
        }
    }


def build_drag_action(observation, *, phase, target_id, pending=None):
    """Resolve only semantic IDs into an executor-owned drag description."""

    available = {item["id"] for item in observation.get("inventory", [])}
    if target_id not in available:
        raise ValueError("Unknown observed inventory target")
    if phase == "pick_first":
        drop_zone = observation.get("drop_zone")
        if not drop_zone or not drop_zone.get("id"):
            raise AlchemyVerificationError("The crafting board is not observed")
        drop_id = drop_zone["id"]
    elif phase == "pick_second":
        if not pending or not pending.get("instance_id"):
            raise ValueError("A pending board instance is required")
        drop_id = pending["instance_id"]
    else:
        raise ValueError("Unknown alchemy phase")
    return {
        "id": f"add:{target_id}",
        "kind": "drag",
        "phase": phase,
        "source_id": target_id,
        "drop_id": drop_id,
    }


def _changed(before, after, key, *, exclude=()):
    previous = {item.get("id"): item for item in before.get(key, [])}
    excluded = set(exclude)
    return [
        item
        for item in after.get(key, [])
        if item.get("id") not in excluded
        and (item.get("id") not in previous or previous[item["id"]] != item)
    ]


def pending_from_first(before, after, element_id):
    created = _changed(before, after, "instances")
    if len(created) != 1:
        raise AlchemyVerificationError(
            f"Expected one new board instance after the first drag; observed {len(created)}"
        )
    return {
        "instance_id": created[0]["id"],
        "element_id": element_id,
        "name": created[0]["name"],
        "emoji": created[0]["emoji"],
    }


def produced_from_second(before, after, pending):
    pending_id = pending.get("instance_id") if pending else None
    board_results = _changed(before, after, "instances", exclude={pending_id})
    if board_results:
        if len(board_results) != 1:
            raise AlchemyVerificationError(
                f"Expected one produced board instance; observed {len(board_results)}"
            )
        return board_results[0]
    inventory_results = _changed(before, after, "inventory")
    if len(inventory_results) == 1:
        return inventory_results[0]
    if after.get("busy"):
        raise AlchemyVerificationError("The crafting result is still settling")
    if pending_id and any(item.get("id") == pending_id for item in after.get("instances", [])):
        raise AlchemyVerificationError("The pending board instance is still present after the second drag")
    return None


def goal_is_visible(goal, observation):
    """Verify a named goal against observed element names, without trusting DONE."""

    normalized_goal = " ".join(str(goal).casefold().split())
    observed = [*observation.get("inventory", []), *observation.get("instances", [])]
    return any(
        re.search(
            rf"(?<!\w){re.escape(' '.join(item['name'].casefold().split()))}(?!\w)",
            normalized_goal,
        )
        for item in observed
        if item.get("name")
    )


class AlchemyBrowser(CdpBrowser):
    """A Browser Harness tab that exposes only semantic alchemy operations."""

    def __init__(self, url, **kwargs):
        super().__init__(url)
        self.mutation_log = []
        self.alchemy_screenshots = kwargs.get("screenshots", False)
        self.last_screenshot = ""
        self._initial_state_waited = False

    def _wait_for_initial_state(self):
        if self._initial_state_waited:
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self.evaluate("Boolean(document.querySelector('.item[data-item-text][data-item-id]'))"):
                    break
            except (RuntimeError, StalePage):
                pass
            time.sleep(0.05)
        self._initial_state_waited = True

    def _wait_for_settle(self, action):
        if action.get("phase") != "pick_second":
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""new Promise(resolve => {
                      let frames = 0;
                      const settle = () => {
                        if (++frames >= 3) resolve();
                        else requestAnimationFrame(settle);
                      };
                      requestAnimationFrame(settle);
                    })""",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
            return

        deadline = time.monotonic() + 8
        saw_busy = False
        quiet_reads = 0
        while time.monotonic() < deadline:
            try:
                busy = bool(
                    self.evaluate(
                        "Boolean(document.querySelector("
                        "'#instances .instance-disabled, #instances .instance-pinwheel'))"
                    )
                )
            except (RuntimeError, StalePage):
                time.sleep(0.05)
                continue
            if busy:
                saw_busy = True
                quiet_reads = 0
            elif saw_busy:
                quiet_reads += 1
                if quiet_reads >= 2:
                    return
            elif time.monotonic() + 7.7 < deadline:
                return
            time.sleep(0.05)

    def observe(self, screenshot=None):
        self._wait_for_initial_state()
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            self._wait_for_settle(action)
        if screenshot is None:
            screenshot = self.alchemy_screenshots
        for attempt in range(10):
            try:
                info = alchemy_browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
                if info.get("screenshot"):
                    self.last_screenshot = info["screenshot"]
                elif screenshot and self.last_screenshot:
                    info["screenshot"] = self.last_screenshot
                return info
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Alchemy page did not settle")

    def fresh(self, page, _action=None):
        return self.evaluate(MARKER) == page.get("marker")

    def act(self, action, page):
        if action.get("kind") != "drag":
            raise ValueError("Alchemy only supports drag actions")
        if not self.fresh(page):
            raise StalePage("Page changed since this decision. Observe again.")
        record = {
            "action": action["id"],
            "phase": action["phase"],
            "source_id": action["source_id"],
            "drop_id": action["drop_id"],
            "status": "planned",
            "logged_at": time.time(),
        }
        # This record is intentionally appended before any Input.dispatchMouseEvent call.
        self.mutation_log.append(record)
        try:
            result = alchemy_browser_operation(
                {
                    "operation": "drag",
                    "session": self.session,
                    "action": action,
                    "expected_marker": page["marker"],
                }
            )
        except StalePage:
            record["status"] = "rejected_before_input"
            raise
        except Exception as error:
            record["status"] = "uncertain_after_input"
            raise DragInputError("Drag input was interrupted; observe before continuing.") from error
        record["status"] = "input_sent"
        self.after_input = action
        return result


class AlchemyAdapter:
    """Stateful two-decision crafting adapter; model choice is deliberately separate."""

    def __init__(self, url, goal, *, browser=None, screenshots=False):
        self.goal = normalize_alchemy_goal(goal)
        if not self.goal:
            raise ValueError("Supply an alchemy goal")
        self.browser = browser or AlchemyBrowser(url, screenshots=screenshots)
        self.observation = self.browser.observe(screenshot=screenshots)
        self.phase = "pick_first"
        self.pending = None
        self.known_results = []
        self.history = []

    def snapshot(self):
        return {
            "goal": self.goal,
            "state": semantic_state(
                self.observation,
                phase=self.phase,
                pending=self.pending,
                known_results=self.known_results,
                recent_actions=self.history,
            ),
            "observation": self.observation,
            "targets": action_targets(self.observation),
            "phase": self.phase,
            "pending": self.pending,
            "history": self.history[-10:],
        }

    def goal_satisfied(self):
        return goal_is_visible(self.goal, self.observation)

    def execute(self, target_id):
        before = self.observation
        pending_before = self.pending
        action = build_drag_action(
            before,
            phase=self.phase,
            target_id=target_id,
            pending=pending_before,
        )
        self.browser.act(action, before)
        after = self.browser.observe()
        if self.phase == "pick_first":
            pending = pending_from_first(before, after, target_id)
            produced = None
            next_phase = "pick_second"
        else:
            pending = None
            produced = produced_from_second(before, after, pending_before)
            next_phase = "pick_first"
            selected = next(item for item in before["inventory"] if item["id"] == target_id)
            new_discovery = bool(
                produced
                and produced.get("name")
                not in {item.get("name") for item in before.get("inventory", [])}
            )
            self.known_results.append(
                {
                    "first_id": pending_before["element_id"],
                    "first": pending_before["name"],
                    "first_emoji": pending_before.get("emoji", ""),
                    "second_id": target_id,
                    "second": selected["name"],
                    "second_emoji": selected.get("emoji", ""),
                    "status": "success" if produced else "no_result",
                    "produced": produced["name"] if produced else None,
                    "produced_emoji": produced.get("emoji", "") if produced else "",
                    "new_discovery": new_discovery,
                }
            )
        outcome = {
            "status": "success" if produced is not None or self.phase == "pick_first" else "no_result",
            "selected": target_id,
            "pending_before": pending_before["element_id"] if pending_before else None,
            "produced": produced,
            "new_discovery": new_discovery if self.phase == "pick_second" else False,
            "phase": next_phase,
        }
        self.history.append(outcome)
        self.observation = after
        self.pending = pending
        self.phase = next_phase
        return outcome

    def close(self):
        self.browser.close()


class AlchemyAgent:
    """One Jev decision followed by one verified semantic browser mutation."""

    def __init__(self, url, goal, *, browser=None, screenshots=False, max_model_calls=None):
        self.adapter = AlchemyAdapter(url, goal, browser=browser, screenshots=screenshots)
        self.screenshots = screenshots
        self.max_model_calls = max_model_calls or ALCHEMY_MAX_MODEL_CALLS
        self.started_at = time.perf_counter()
        self.decision = None
        self.last_decision = None
        self.decisions = []
        self.status = "ready"
        self.last_error = None

    def _elapsed(self):
        return round((time.perf_counter() - self.started_at) * 1000)

    def snapshot(self):
        return {
            **self.adapter.snapshot(),
            "status": self.status,
            "decision": self.decision,
            "last_decision": self.last_decision,
            "decisions": self.decisions[-10:],
            "model_calls": len(self.decisions),
            "max_model_calls": self.max_model_calls,
            "elapsed_ms": self._elapsed(),
            "last_error": self.last_error,
        }

    def _observe_if_stale(self):
        page = self.adapter.observation
        if not self.adapter.browser.fresh(page):
            self.adapter.observation = self.adapter.browser.observe(screenshot=self.screenshots)

    def _mark_budget_exhausted(self):
        if self.status == "ready" and len(self.decisions) >= self.max_model_calls:
            self.status = "blocked"
            self.last_error = "Reached the alchemy model-call budget"

    def predict(self):
        if self.status in {"done", "blocked", "uncertain"}:
            raise ValueError("This alchemy run has stopped. Start a fresh run.")
        self._observe_if_stale()
        page = self.adapter.observation
        self.last_error = None
        if self.adapter.goal_satisfied():
            self.decision = {
                "choice": "DONE",
                "operation": "DONE",
                "target": None,
                "confidence": 1.0,
                "probabilities": {"DONE": 1.0},
                "operation_probabilities": {"DONE": 1.0},
                "target_probabilities": {},
                "target_confidence": None,
                "model": None,
                "usage": {},
                "latency_ms": 0,
                "source": "independent_verification",
                "fingerprint": page["fingerprint"],
            }
        else:
            if len(self.decisions) >= self.max_model_calls:
                self.status = "blocked"
                self.last_error = "Reached the alchemy model-call budget"
                return self.snapshot()
            state = self.adapter.snapshot()["state"]
            try:
                self.decision = choose_alchemy(state, self.adapter.goal, self.adapter.history)
            except TransientModelError as error:
                self.decision = None
                self.status = "retryable"
                self.last_error = str(error)
                return self.snapshot()
            self.decision["fingerprint"] = page["fingerprint"]
            self.decisions.append(
                {
                    **self.decision,
                    "elapsed_ms": self._elapsed(),
                }
            )
        self.status = "predicted"
        return self.snapshot()

    def act(self):
        decision = self.decision
        if not decision:
            raise ValueError("Observe and choose before acting")
        # Consume the decision before any model- or browser-side work.
        self.decision = None
        self.last_decision = decision
        self.last_error = None
        page = self.adapter.observation
        if decision.get("fingerprint") != page.get("fingerprint"):
            raise StalePage("Alchemy state changed since this decision. Observe again.")
        if not self.adapter.browser.fresh(page):
            raise StalePage("Alchemy page changed since this decision. Observe again.")

        operation = decision["operation"]
        if operation == "DONE":
            if not self.adapter.goal_satisfied():
                self.adapter.history.append({"status": "unverified_done", "operation": "DONE"})
                self.status = "ready"
                self.last_error = "Jev suggested DONE, but the goal is not visible"
                self._mark_budget_exhausted()
                return self.snapshot()
            self.status = "done"
            return self.snapshot()
        if operation == "BLOCKED":
            self.adapter.history.append({"status": "model_blocked", "operation": "BLOCKED"})
            self.status = "blocked"
            return self.snapshot()
        if operation != "ADD_ELEMENT" or not decision.get("target"):
            raise ValueError("Invalid alchemy decision; no action executed")

        try:
            outcome = self.adapter.execute(decision["target"])
        except StalePage:
            self.status = "ready"
            raise
        except DragInputError:
            self.status = "uncertain"
            raise
        self.status = "done" if self.adapter.goal_satisfied() else "ready"
        self._mark_budget_exhausted()
        return {**self.snapshot(), "outcome": outcome}

    def command(self, name, _body=None):
        if name == "predict":
            return self.predict()
        if name == "act":
            return self.act()
        if name == "tick":
            try:
                result = self.predict()
                if not self.decision:
                    return result
                return self.act()
            except StalePage:
                self.decision = None
                self.status = "ready"
                self.adapter.observation = self.adapter.browser.observe(screenshot=self.screenshots)
                return self.snapshot()
        raise ValueError("Unknown alchemy command")

    def run(self):
        while self.status not in {"done", "blocked", "uncertain", "retryable"}:
            yield self.command("tick")

    def close(self):
        self.adapter.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _resolve_drag(request, evaluate):
    payload = {"action": request["action"], "expected_marker": request["expected_marker"]}
    expression = """(payload => {
      const state = __READ_STATE__;
      if (!state || state.marker !== payload.expected_marker) return {error: 'stale'};
      const cache = window.__typeSafeAlchemy;
      const source = cache?.nodes.get(payload.action.source_id);
      const drop = cache?.nodes.get(payload.action.drop_id);
      const visible = element => {
        if (!element?.isConnected || element.closest('[aria-hidden="true"], [inert]')) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' &&
          rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 &&
          rect.top < innerHeight && rect.left < innerWidth;
      };
      const center = element => {
        if (!visible(element)) return null;
        const rect = element.getBoundingClientRect();
        const x = rect.left + rect.width / 2;
        const y = rect.top + rect.height / 2;
        const hit = document.elementFromPoint(x, y);
        if (hit !== element && !element.contains(hit)) return null;
        return {x, y};
      };
      const sourcePoint = center(source);
      if (!sourcePoint) return {error: 'source'};
      let dropPoint = null;
      if (payload.action.phase === 'pick_second') {
        dropPoint = center(drop);
      } else if (payload.action.phase === 'pick_first' && drop?.id === 'instances') {
        const sidebar = document.querySelector('#sidebar');
        const occupied = [...document.querySelectorAll('#instances .instance')]
          .map(element => element.getBoundingClientRect());
        const candidates = [
          [innerWidth * 0.50, innerHeight * 0.18],
          [innerWidth * 0.68, innerHeight * 0.25],
          [innerWidth * 0.38, innerHeight * 0.30],
          [innerWidth * 0.76, innerHeight * 0.42],
          [innerWidth * 0.24, innerHeight * 0.42],
        ];
        for (let row = 1; row < 8; row++) {
          for (let column = 1; column < 8; column++) {
            candidates.push([innerWidth * column / 8, innerHeight * row / 8]);
          }
        }
        for (const [x, y] of candidates) {
          if (x < 2 || y < 2 || x >= innerWidth - 2 || y >= innerHeight - 2) continue;
          if (sidebar && (() => { const r = sidebar.getBoundingClientRect(); return r.width && r.height &&
              x >= r.left && x <= r.right && y >= r.top && y <= r.bottom; })()) continue;
          if (occupied.some(rect => rect.width && rect.height &&
              x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom)) continue;
          const hit = document.elementFromPoint(x, y);
          if (hit?.closest('#sidebar')) continue;
          if (hit?.closest('#instances .instance, .item')) continue;
          dropPoint = {x, y};
          break;
        }
      }
      if (!dropPoint) return {error: 'drop'};
      return {source: sourcePoint, drop: dropPoint};
    })(__PAYLOAD__)"""
    expression = expression.replace("__READ_STATE__", READ_STATE).replace("__PAYLOAD__", json.dumps(payload))
    return evaluate(expression)


def alchemy_browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            raise StalePage("Document changed during alchemy evaluation")
        return result.get("result", {}).get("value")

    if operation == "observe":
        info = evaluate(READ_STATE)
        if info is None:
            raise StalePage("Document is navigating")
        info["fingerprint"] = fingerprint(info)
        if request.get("screenshot", False):
            try:
                info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
            except Exception as error:  # A screenshot is optional and must not invalidate semantic state.
                LOG.warning("Alchemy screenshot unavailable: %s", error)
        return info

    if operation != "drag":
        raise ValueError("Unknown alchemy browser operation")
    action = request["action"]
    if action.get("kind") != "drag":
        raise ValueError("Invalid alchemy action")
    resolved = _resolve_drag(request, evaluate)
    if not resolved or resolved.get("error"):
        error = (resolved or {}).get("error", "stale")
        if error in {"stale", "source", "drop"}:
            raise StalePage(f"Alchemy {error} target changed or is covered")
        raise RuntimeError("Alchemy drag target was not resolved")
    source, drop = resolved["source"], resolved["drop"]
    try:
        call(
            "Input.dispatchMouseEvent",
            type="mouseMoved",
            x=source["x"],
            y=source["y"],
            button="none",
            buttons=0,
        )
        call(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=source["x"],
            y=source["y"],
            button="left",
            buttons=1,
            clickCount=1,
        )
        steps = 8
        for index in range(1, steps + 1):
            progress = index / steps
            call(
                "Input.dispatchMouseEvent",
                type="mouseMoved",
                x=source["x"] + (drop["x"] - source["x"]) * progress,
                y=source["y"] + (drop["y"] - source["y"]) * progress,
                button="left",
                buttons=1,
            )
            time.sleep(0.02)
        call(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=drop["x"],
            y=drop["y"],
            button="left",
            buttons=0,
            clickCount=1,
        )
    except Exception as error:
        raise DragInputError("CDP drag input was interrupted") from error
    LOG.info("Alchemy drag sent: %s -> %s", action["source_id"], action["drop_id"])
    return {"executed": action["id"], "drag_events": steps + 3}
