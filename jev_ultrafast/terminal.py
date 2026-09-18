"""Line-oriented Infinite Craft runner: Jev chooses, Neal.fun combines."""

import argparse
import os
import sys

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


def label(item):
    return f"{item.get('emoji', '')} {item.get('name', '')}".strip()


class TerminalAlchemyAgent:
    """Keep Jev's semantic loop while using Neal.fun's pair API as the executor."""

    def __init__(self, goal, *, api, max_model_calls=ALCHEMY_MAX_MODEL_CALLS):
        self.goal = normalize_alchemy_goal(goal)
        if not self.goal:
            raise ValueError("Supply an alchemy goal")
        self.api = api
        self.max_model_calls = max_model_calls
        self.inventory = [{"id": id_, "name": name, "emoji": emoji} for id_, name, emoji in STARTERS]
        self.next_id = len(self.inventory) + 1
        self.phase = "pick_first"
        self.pending = None
        self.known_results = []
        self.history = []
        self.model_calls = 0
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

    def run(self, emit=print):
        emit(f"target: {self.goal}")
        while True:
            if goal_is_visible(self.goal, self._observation()):
                self.status = "done"
                emit(f"found: {self.goal}")
                return self.status
            if self.model_calls >= self.max_model_calls:
                self.status = "blocked"
                emit("stopped: model-call budget reached")
                return self.status

            state = self.state()
            if not state["available_target_ids"]:
                self.status = "blocked"
                emit("stopped: no unexplored combination remains")
                return self.status

            decision = choose_alchemy_target(state, self.goal, self.history)
            self.model_calls += 1
            target = self._find(decision["target"])
            if self.phase == "pick_first":
                self.pending = {
                    "element_id": target["id"],
                    "name": target["name"],
                    "emoji": target.get("emoji", ""),
                }
                self.phase = "pick_second"
                self.history.append({"phase": "pick_first", "selected": target["id"]})
                emit(f"{label(target)} ...")
                continue

            first = self.pending
            result = self.api.pair(first["name"], target["name"])
            produced, new_discovery = (None, False)
            if result.name:
                produced, new_discovery = self._add_result(result.name, result.emoji)
            result_label = label(produced) if produced else "∅"
            suffix = " ✦" if new_discovery else ""
            emit(f"{label(first)} + {label(target)} = {result_label}{suffix}")
            self.known_results.append(
                {
                    "first_id": first["element_id"],
                    "first": first["name"],
                    "first_emoji": first.get("emoji", ""),
                    "second_id": target["id"],
                    "second": target["name"],
                    "second_emoji": target.get("emoji", ""),
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
    args = parser.parse_args()
    if not os.environ.get("TYPESAFE_API_KEY"):
        parser.error("TYPESAFE_API_KEY is required")
    try:
        with InfiniteCraftApi(rate_limit=args.rate_limit) as api:
            result = TerminalAlchemyAgent(
                args.goal,
                api=api,
                max_model_calls=args.max_model_calls,
            ).run()
    except KeyboardInterrupt:
        print("stopped: interrupted", file=sys.stderr)
        return 130
    except (InfiniteCraftApiError, TransientModelError, ValueError) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 2
    return 0 if result == "done" else 2


if __name__ == "__main__":
    raise SystemExit(main())
