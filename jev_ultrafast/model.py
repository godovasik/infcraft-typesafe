"""TypeSafe makes choices; OpenAI GPT-5.6 Luna writes field values."""

import json
import math
import os
import time

import httpx

from .questions import (
    ALCHEMY_HISTORY_LIMIT,
    ALCHEMY_NEXT_ACTION,
    ALCHEMY_TARGET,
    NEXT_ACTION,
    TARGET,
    TEXT_VALUE,
)

CLIENT = httpx.Client(http2=True, timeout=25)
OPENAI_TEXT_MODEL = "gpt-5.6-luna"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
RETRYABLE_MODEL_STATUSES = {429, 500, 502, 503, 504, 529}
MODEL_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
INVALID_RESPONSE_RETRY_DELAYS = (0.5, 1.0, 2.0)


class TransientModelError(RuntimeError):
    """A model provider is temporarily unavailable; no browser mutation was attempted."""


class InvalidTypeSafeResponse(ValueError):
    """The provider answered, but its choice/probability contract was invalid."""


def post_json(url, key, body):
    for attempt in range(len(MODEL_RETRY_DELAYS) + 1):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            if attempt < len(MODEL_RETRY_DELAYS):
                time.sleep(MODEL_RETRY_DELAYS[attempt])
                continue
            raise TransientModelError("Model connection failed temporarily; no browser action executed.") from None
        if response.status_code in RETRYABLE_MODEL_STATUSES:
            if attempt < len(MODEL_RETRY_DELAYS):
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 10.0)
                except (TypeError, ValueError):
                    delay = MODEL_RETRY_DELAYS[attempt]
                time.sleep(max(delay, 0.0))
                continue
            raise TransientModelError(
                f"Model provider is busy (HTTP {response.status_code}); retry without browser input."
            )
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise TransientModelError("Model provider is temporarily unavailable; no browser action executed.")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise InvalidTypeSafeResponse("Invalid TypeSafe response; no action executed.")
    return answer


def _response_answers(result):
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise InvalidTypeSafeResponse("Invalid TypeSafe response; missing answers; no action executed.")
    return result["answers"]


def post_json_with_validation(url, key, body, parse):
    """Retry a valid HTTP response when its TypeSafe choice payload is malformed."""

    for attempt in range(len(INVALID_RESPONSE_RETRY_DELAYS) + 1):
        result = post_json(url, key, body)
        try:
            parsed = parse(result)
        except InvalidTypeSafeResponse as error:
            if attempt < len(INVALID_RESPONSE_RETRY_DELAYS):
                time.sleep(INVALID_RESPONSE_RETRY_DELAYS[attempt])
                continue
            raise InvalidTypeSafeResponse(
                f"{error} Retried {len(INVALID_RESPONSE_RETRY_DELAYS)} times."
            ) from None
        return result, parsed, attempt + 1
    raise InvalidTypeSafeResponse("Invalid TypeSafe response; no action executed.")


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def choose(state, goal, history):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    def parse(result):
        answers = _response_answers(result)
        operation_answer = validate_choice(answers.get("operation", {}), operations)
        operation = operation_answer["choice"]
        target = None
        target_answer = None
        probabilities = {}
        if operation in targets:
            # Unused target heads cannot cause an action. Validate the head selected by the operation.
            target_answer = validate_choice(answers.get(operation.lower() + "_target", {}), targets[operation])
            target = target_answer["choice"]
            choice = targets[operation][target]["id"]
            probabilities = {
                a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()
            }
        else:
            choice = controls[operation]["id"] if operation in controls else operation
            probabilities[choice] = operation_answer["probabilities"][operation]
        return {
            "answers": answers,
            "operation_answer": operation_answer,
            "operation": operation,
            "target": target,
            "target_answer": target_answer,
            "choice": choice,
            "probabilities": probabilities,
        }

    started = time.perf_counter()
    result, parsed, request_attempts = post_json_with_validation(
        "https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body, parse
    )
    operation_answer = parsed["operation_answer"]
    target_answer = parsed["target_answer"]
    return {
        "choice": parsed["choice"],
        "operation": parsed["operation"],
        "target": parsed["target"],
        "confidence": operation_answer["confidence"],
        "probabilities": parsed["probabilities"],
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": parsed["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
        "request_attempts": request_attempts,
    }


def _alchemy_targets(state):
    offered_ids = (
        set(state["available_target_ids"])
        if "available_target_ids" in state
        else {item.get("id") for item in state.get("inventory", [])}
    )
    targets = {
        item["id"]: {"label": f"{item.get('emoji', '')} {item['name']}".strip()}
        for item in state.get("inventory", [])
        if item.get("id") in offered_ids and item.get("name")
    }
    return targets


def _alchemy_semantic_state(state, exploration, recent_actions):
    return {
        "phase": state["phase"],
        "inventory": [
            {key: item.get(key, "") for key in ("id", "name", "emoji")}
            for item in state.get("inventory", [])
        ],
        "pending": state.get("pending"),
        "exploration": exploration,
        "known_results": list(state.get("known_results", []))[-ALCHEMY_HISTORY_LIMIT:],
        "recent_actions": list(recent_actions or state.get("recent_actions", []))[-10:],
    }


def choose_alchemy(state, goal, recent_actions=None):
    """Choose one semantic alchemy operation and, when needed, one observed element."""

    targets = _alchemy_targets(state)
    exploration = state.get("exploration") or {"unexplored_combinations": 1}
    operations = {"DONE": "The requested result is visibly present."}
    if targets:
        operations = {"ADD_ELEMENT": "Add one observed element to the current crafting interaction.", **operations}
        if exploration.get("unexplored_combinations", 0) == 0:
            operations["BLOCKED"] = "All currently available combinations have been tested without reaching the target."
    else:
        operations["BLOCKED"] = "No untested combination is available for the current crafting phase."
    semantic = _alchemy_semantic_state(state, exploration, recent_actions)
    questions = {
        "operation": {
            "type": "choice",
            "criteria": operations,
            "instructions": {"goal": goal, "rules": ALCHEMY_NEXT_ACTION},
        }
    }
    if targets:
        questions["add_element_target"] = {
            "type": "choice",
            "criteria": targets,
            "instructions": {"goal": goal, "rules": ALCHEMY_TARGET},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "goal": goal,
        "state": semantic,
        "targets": {"ADD_ELEMENT": targets} if targets else {},
        "questions": questions,
    }
    def parse(result):
        answers = _response_answers(result)
        operation_answer = validate_choice(answers.get("operation", {}), operations)
        operation = operation_answer["choice"]
        target = None
        target_answer = None
        if operation == "ADD_ELEMENT":
            target_answer = validate_choice(answers.get("add_element_target", {}), targets)
            target = target_answer["choice"]
            choice = target
            probabilities = target_answer["probabilities"]
        else:
            choice = operation
            probabilities = {operation: operation_answer["probabilities"][operation]}
        return {
            "answers": answers,
            "operation_answer": operation_answer,
            "operation": operation,
            "target": target,
            "target_answer": target_answer,
            "choice": choice,
            "probabilities": probabilities,
        }

    started = time.perf_counter()
    result, parsed, request_attempts = post_json_with_validation(
        "https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body, parse
    )
    operation_answer = parsed["operation_answer"]
    target_answer = parsed["target_answer"]
    return {
        "choice": parsed["choice"],
        "operation": parsed["operation"],
        "target": parsed["target"],
        "confidence": operation_answer["confidence"],
        "probabilities": parsed["probabilities"],
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": parsed["answers"],
        "model": result.get("model", body["model"]),
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
        "request_attempts": request_attempts,
    }


def choose_alchemy_target(state, goal, recent_actions=None):
    """Choose only an offered element; program code owns stop conditions."""

    targets = _alchemy_targets(state)
    if not targets:
        raise ValueError("No untested alchemy target is offered")
    exploration = state.get("exploration") or {"unexplored_combinations": 1}
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "goal": goal,
        "state": _alchemy_semantic_state(state, exploration, recent_actions),
        "targets": {"ADD_ELEMENT": targets},
        "questions": {
            "add_element_target": {
                "type": "choice",
                "criteria": targets,
                "instructions": {"goal": goal, "rules": ALCHEMY_TARGET},
            }
        },
    }
    def parse(result):
        answers = _response_answers(result)
        return answers, validate_choice(answers.get("add_element_target", {}), targets)

    started = time.perf_counter()
    result, parsed, request_attempts = post_json_with_validation(
        "https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body, parse
    )
    answers, target_answer = parsed
    target = target_answer["choice"]
    return {
        "choice": target,
        "operation": "ADD_ELEMENT",
        "target": target,
        "confidence": target_answer["confidence"],
        "probabilities": target_answer["probabilities"],
        "operation_probabilities": {"ADD_ELEMENT": 1.0},
        "target_probabilities": target_answer["probabilities"],
        "target_confidence": target_answer["confidence"],
        "raw_answers": answers,
        "model": result.get("model", body["model"]),
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
        "request_attempts": request_attempts,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def response_text(result):
    """Read text from a raw Responses API payload without assuming output ordering."""
    direct = result.get("output_text")
    if isinstance(direct, str):
        return direct
    for item in result.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if (
                isinstance(content, dict)
                and content.get("type") == "output_text"
                and isinstance(content.get("text"), str)
            ):
                return content["text"]
    return None


def field_text(context):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs OPENAI_API_KEY; no text is hardcoded or guessed by the executor.")
    started = time.perf_counter()
    result = post_json(
        OPENAI_RESPONSES_URL,
        key,
        {
            "model": OPENAI_TEXT_MODEL,
            "instructions": TEXT_VALUE,
            "input": json.dumps(context, ensure_ascii=False),
            "max_output_tokens": 256,
            "reasoning": {"effort": "none"},
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "field_value",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                }
            },
        },
    )
    try:
        output = json.loads(response_text(result))
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": OPENAI_TEXT_MODEL,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
