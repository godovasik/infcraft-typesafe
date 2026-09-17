"""Play an Infinite Craft-style game with Jev choosing one element at a time."""

import argparse
import json

from jev_ultrafast import AlchemyAgent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://neal.fun/infinite-craft/")
    parser.add_argument("--goal", default="Steam", help="Target element name, for example Human")
    parser.add_argument("--max-model-calls", type=int, default=1000)
    args = parser.parse_args()

    with AlchemyAgent(
        args.url,
        args.goal,
        screenshots=False,
        max_model_calls=args.max_model_calls,
    ) as agent:
        for state in agent.run():
            decision = state.get("decision") or {}
            outcome = state.get("outcome") or {}
            event = {
                "status": state["status"],
                "phase": state["phase"],
                "model_calls": state["model_calls"],
                "elapsed_ms": state["elapsed_ms"],
            }
            if decision:
                event["decision"] = {
                    "operation": decision.get("operation"),
                    "target": decision.get("target"),
                    "confidence": decision.get("target_confidence", decision.get("confidence")),
                }
            if outcome:
                event["outcome"] = {
                    "status": outcome.get("status"),
                    "selected": outcome.get("selected"),
                    "produced": outcome.get("produced"),
                    "new_discovery": outcome.get("new_discovery"),
                }
            print(json.dumps(event, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
