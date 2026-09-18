"""Line-oriented Infinite Craft runner: Jev chooses, Neal.fun combines."""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from .alchemy import (
    goal_is_visible,
    normalize_alchemy_goal,
    semantic_state,
)
from .alchemy_api import InfiniteCraftApi, InfiniteCraftApiError
from .model import TransientModelError, choose_alchemy_target
from .questions import ALCHEMY_MAX_MODEL_CALLS

STARTERS = (
    ("el_1", "Water", "💧"),
    ("el_2", "Fire", "🔥"),
    ("el_3", "Wind", "🌬️"),
    ("el_4", "Earth", "🌍"),
)
DEFAULT_RUNS_FILE = "jev-craft-runs.jsonl"
PROBABILISTIC_SELECTION_MODES = {"probabilistic", "probabilistic_squared"}


def label(item):
    return f"{item.get('emoji', '')} {item.get('name', '')}".strip()


def append_run(path, record):
    run_path = Path(path)
    run_path.parent.mkdir(parents=True, exist_ok=True)
    with run_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class TerminalAlchemyAgent:
    """Keep Jev's semantic loop while using Neal.fun's pair API as the executor."""

    def __init__(
        self,
        goal,
        *,
        api,
        max_model_calls=ALCHEMY_MAX_MODEL_CALLS,
        selection_mode="deterministic",
        rng=None,
        seed=None,
    ):
        self.goal = normalize_alchemy_goal(goal)
        if not self.goal:
            raise ValueError("Supply an alchemy goal")
        selection_mode = selection_mode.replace("-", "_")
        if selection_mode not in {"deterministic", *PROBABILISTIC_SELECTION_MODES}:
            raise ValueError(f"Unknown selection mode: {selection_mode}")
        self.api = api
        self.max_model_calls = max_model_calls
        self.selection_mode = selection_mode
        self.rng = rng if rng is not None else random.Random()
        self.seed = seed
        self.inventory = [{"id": id_, "name": name, "emoji": emoji} for id_, name, emoji in STARTERS]
        self.next_id = len(self.inventory) + 1
        self.phase = "pick_first"
        self.pending = None
        self.known_results = []
        self.history = []
        self.model_calls = 0
        self.attempts = 0
        self.status = "ready"

    def _observation(self):
        return {"inventory": self.inventory}

    def state(self):
        return semantic_state(
            self._observation(),
            phase=self.phase,
            pending=self.pending,
            known_results=self.known_results,
            recent_actions=self.history,
        )

    def _find(self, element_id):
        return next(item for item in self.inventory if item["id"] == element_id)

    def _add_result(self, name, emoji):
        existing = next((item for item in self.inventory if item["name"].casefold() == name.casefold()), None)
        if existing:
            return existing, False
        item = {"id": f"el_{self.next_id}", "name": name, "emoji": emoji}
        self.next_id += 1
        self.inventory.append(item)
        return item, True

    def _select_target(self, decision):
        """Return (element id, selection probability) for this model decision."""
        probabilities = decision.get("target_probabilities") or decision.get("probabilities") or {}
        fallback_probability = probabilities.get(decision["target"])
        if self.selection_mode not in PROBABILISTIC_SELECTION_MODES or not probabilities:
            return decision["target"], fallback_probability

        offered_ids = {item["id"] for item in self.inventory}
        candidates = []
        for element_id, probability in probabilities.items():
            if element_id not in offered_ids:
                continue
            try:
                probability = float(probability)
            except (TypeError, ValueError):
                continue
            if probability > 0:
                if self.selection_mode == "probabilistic_squared":
                    probability **= 2
                candidates.append((element_id, probability))

        total = sum(probability for _element_id, probability in candidates)
        if not candidates or total <= 0:
            return decision["target"], fallback_probability

        threshold = self.rng.random() * total
        cumulative = 0.0
        for element_id, probability in candidates:
            cumulative += probability
            if threshold < cumulative:
                return element_id, probability / total
        element_id, probability = candidates[-1]
        return element_id, probability / total

    def _format_selected(self, item, probability):
        rendered = label(item)
        if self.selection_mode in PROBABILISTIC_SELECTION_MODES and probability is not None:
            rendered += f" ({float(probability):.0%})"
        return rendered

    def _progress(self):
        attempt_word = "attempt" if self.attempts == 1 else "attempts"
        request_word = "request" if self.model_calls == 1 else "requests"
        return f"{self.attempts} {attempt_word} ({self.model_calls} Jev {request_word})"

    def run_record(self, *, status, started_at, finished_at, error=None):
        record = {
            "target": self.goal,
            "mode": self.selection_mode,
            "seed": self.seed,
            "status": status,
            "attempts": self.attempts,
            "jev_requests": self.model_calls,
            "started_at": started_at,
            "finished_at": finished_at,
        }
        if error:
            record["error"] = error
        return record

    def run(self, emit=print):
        if self.selection_mode in PROBABILISTIC_SELECTION_MODES:
            mode_label = self.selection_mode.replace("_", " ")
            emit(f"target: {self.goal} [{mode_label} selection]")
        else:
            emit(f"target: {self.goal}")
        while True:
            if goal_is_visible(self.goal, self._observation()):
                self.status = "done"
                emit(f"found: {self.goal} after {self._progress()}")
                return self.status
            if self.model_calls >= self.max_model_calls:
                self.status = "blocked"
                emit(f"stopped after {self._progress()}: model-call budget reached")
                return self.status

            state = self.state()
            if not state["available_target_ids"]:
                self.status = "blocked"
                emit(f"stopped after {self._progress()}: no unexplored combination remains")
                return self.status

            decision = choose_alchemy_target(state, self.goal, self.history)
            self.model_calls += decision.get("request_attempts", 1)
            target_id, target_probability = self._select_target(decision)
            target = self._find(target_id)
            if self.phase == "pick_first":
                self.pending = {
                    "element_id": target["id"],
                    "name": target["name"],
                    "emoji": target.get("emoji", ""),
                    "selection_probability": target_probability,
                }
                self.phase = "pick_second"
                self.history.append(
                    {
                        "phase": "pick_first",
                        "selected": target["id"],
                        "selection_probability": target_probability,
                    }
                )
                continue

            first = self.pending
            self.attempts += 1
            result = self.api.pair(first["name"], target["name"])
            produced, new_discovery = (None, False)
            if result.name:
                produced, new_discovery = self._add_result(result.name, result.emoji)
            result_label = label(produced) if produced else "∅"
            suffix = " ✦" if new_discovery else ""
            emit(
                f"{self._format_selected(first, first.get('selection_probability'))} + "
                f"{self._format_selected(target, target_probability)} = {result_label}{suffix}"
            )
            self.known_results.append(
                {
                    "first_id": first["element_id"],
                    "first": first["name"],
                    "first_emoji": first.get("emoji", ""),
                    "second_id": target["id"],
                    "second": target["name"],
                    "second_emoji": target.get("emoji", ""),
                    "first_probability": first.get("selection_probability"),
                    "second_probability": target_probability,
                    "status": "success" if produced else "no_result",
                    "produced": produced["name"] if produced else None,
                    "produced_emoji": produced.get("emoji", "") if produced else "",
                    "new_discovery": new_discovery,
                }
            )
            self.history.append(
                {
                    "phase": "pick_second",
                    "pending": first["element_id"],
                    "selected": target["id"],
                    "selection_probability": target_probability,
                    "status": "success" if produced else "no_result",
                }
            )
            self.pending = None
            self.phase = "pick_first"


def main():
    parser = argparse.ArgumentParser(description="Let Jev explore Infinite Craft from the terminal.")
    parser.add_argument("--goal", default="Steam", help="Target element name, for example Horse")
    parser.add_argument("--max-model-calls", type=int, default=ALCHEMY_MAX_MODEL_CALLS)
    parser.add_argument("--rate-limit", type=int, default=60, help="Neal.fun pair requests per minute")
    parser.add_argument(
        "--selection",
        "--mode",
        dest="selection",
        choices=("deterministic", "probabilistic", "probabilistic-squared"),
        default="deterministic",
        help="Choose Jev's highest-probability target or sample its distribution",
    )
    parser.add_argument("--seed", type=int, help="Random seed for reproducible probabilistic runs")
    parser.add_argument(
        "--runs-file",
        default=DEFAULT_RUNS_FILE,
        help=f"Append one JSON record per run (default: {DEFAULT_RUNS_FILE})",
    )
    args = parser.parse_args()
    if not os.environ.get("TYPESAFE_API_KEY"):
        parser.error("TYPESAFE_API_KEY is required")
    selection_mode = args.selection.replace("-", "_")
    run_seed = args.seed
    if selection_mode in PROBABILISTIC_SELECTION_MODES and run_seed is None:
        run_seed = random.SystemRandom().randrange(2**63)
    started_at = datetime.now(timezone.utc).isoformat()
    agent = None
    status = "error"
    error_message = None
    exit_code = 2
    try:
        with InfiniteCraftApi(rate_limit=args.rate_limit) as api:
            agent = TerminalAlchemyAgent(
                args.goal,
                api=api,
                max_model_calls=args.max_model_calls,
                selection_mode=selection_mode,
                rng=random.Random(run_seed),
                seed=run_seed,
            )
            status = agent.run()
            exit_code = 0 if status == "done" else 2
    except KeyboardInterrupt:
        status = "interrupted"
        error_message = "interrupted"
        print("stopped: interrupted", file=sys.stderr)
        exit_code = 130
    except (InfiniteCraftApiError, TransientModelError, ValueError) as error:
        status = "error"
        error_message = str(error)
        print(f"stopped: {error}", file=sys.stderr)
        exit_code = 2
    finally:
        if agent is not None:
            try:
                append_run(
                    args.runs_file,
                    agent.run_record(
                        status=status,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        error=error_message,
                    ),
                )
            except OSError as error:
                print(f"warning: could not write run record: {error}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
