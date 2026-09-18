import json

import pytest

from jev_ultrafast import alchemy, model
from jev_ultrafast.browser import StalePage


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.is_error = status_code >= 400

    def json(self):
        return self._payload


def observation(*, instances=None, inventory=None):
    inventory = inventory or [
        {"id": "el_water", "name": "Water", "emoji": "💧", "label": "💧 Water"},
        {"id": "el_fire", "name": "Fire", "emoji": "🔥", "label": "🔥 Fire"},
    ]
    state = {
        "url": "https://example.test/alchemy",
        "title": "TypeSafe Alchemy Fixture",
        "inventory": inventory,
        "instances": instances or [],
        "drop_zone": {"id": "board_1", "label": "Crafting board"},
        "marker": "fixture-marker",
    }
    state["fingerprint"] = alchemy.fingerprint(state)
    return state


def choice(options, selected):
    return {
        "choice": selected,
        "confidence": 0.9,
        "probabilities": {option: (0.9 if option == selected else 0.1 / (len(options) - 1)) for option in options},
    }


def test_semantic_state_and_targets_contain_only_observed_opaque_choices():
    state = alchemy.semantic_state(observation(), phase="pick_first")
    targets = alchemy.action_targets(observation())

    assert state["phase"] == "pick_first"
    assert state["pending"] is None
    assert set(targets["ADD_ELEMENT"]) == {"el_water", "el_fire"}
    assert all(set(item) == {"id", "name", "emoji"} for item in state["inventory"])
    assert state["exploration"]["unexplored_combinations"] == 3
    assert "selector" not in json.dumps(state)
    assert "coordinate" not in json.dumps(state)


def test_attempted_pairs_are_directionless_and_self_pairs_are_consumed():
    known_results = [
        {"first_id": "el_fire", "second_id": "el_water", "status": "success"},
        {"first_id": "el_water", "second_id": "el_water", "status": "no_result"},
    ]

    second_targets = alchemy.action_targets(
        observation(),
        phase="pick_second",
        pending={"element_id": "el_water"},
        known_results=[known_results[0]],
    )
    first_targets = alchemy.action_targets(
        observation(),
        phase="pick_first",
        known_results=known_results,
    )

    assert set(second_targets["ADD_ELEMENT"]) == {"el_water"}
    assert set(first_targets["ADD_ELEMENT"]) == {"el_fire"}


def test_adapter_rejects_an_already_attempted_pair_before_drag():
    before = observation()

    class FakeBrowser:
        def __init__(self):
            self.actions = []

        def observe(self, **_kwargs):
            return before

        def act(self, action, _page):
            self.actions.append(action)

        def close(self):
            pass

    browser = FakeBrowser()
    adapter = alchemy.AlchemyAdapter("unused", "Steam", browser=browser)
    adapter.phase = "pick_second"
    adapter.pending = {"instance_id": "inst_1", "element_id": "el_water", "name": "Water", "emoji": "💧"}
    adapter.known_results = [
        {"first_id": "el_fire", "second_id": "el_water", "status": "success"}
    ]

    with pytest.raises(ValueError, match="repeat"):
        adapter.execute("el_fire")

    assert browser.actions == []


def test_alchemy_goal_accepts_a_bare_name_or_get_prefix():
    assert alchemy.normalize_alchemy_goal("Human") == "Human"
    assert alchemy.normalize_alchemy_goal("Get Human") == "Human"
    assert alchemy.normalize_alchemy_goal("получи Человека") == "Человека"


def test_build_drag_action_uses_board_then_pending_instance_and_allows_self_pair():
    first = alchemy.build_drag_action(
        observation(), phase="pick_first", target_id="el_water"
    )
    pending = {"instance_id": "inst_1", "element_id": "el_water", "name": "Water", "emoji": "💧"}
    second = alchemy.build_drag_action(
        observation(), phase="pick_second", target_id="el_water", pending=pending
    )

    assert first["drop_id"] == "board_1"
    assert second["drop_id"] == "inst_1"
    assert second["source_id"] == "el_water"


def test_first_and_second_observation_transitions_are_independent():
    before = observation()
    after_first = observation(
        instances=[{"id": "inst_1", "name": "Water", "emoji": "💧", "label": "💧 Water"}]
    )
    pending = alchemy.pending_from_first(before, after_first, "el_water")
    after_second = observation(
        instances=[{"id": "inst_2", "name": "Steam", "emoji": "💨", "label": "💨 Steam"}],
        inventory=[
            *before["inventory"],
            {"id": "el_steam", "name": "Steam", "emoji": "💨", "label": "💨 Steam"},
        ],
    )

    produced = alchemy.produced_from_second(after_first, after_second, pending)

    assert pending["instance_id"] == "inst_1"
    assert pending["element_id"] == "el_water"
    assert produced["name"] == "Steam"
    assert alchemy.goal_is_visible("Получи Steam", after_second)


def test_no_result_is_recorded_when_pending_disappears_without_new_element():
    before = observation(
        instances=[{"id": "inst_1", "name": "Water", "emoji": "💧", "label": "💧 Water"}]
    )
    after = observation()
    pending = {"instance_id": "inst_1", "element_id": "el_water", "name": "Water", "emoji": "💧"}

    assert alchemy.produced_from_second(before, after, pending) is None


def test_still_pending_after_second_drag_is_not_treated_as_no_result():
    before = observation(
        instances=[{"id": "inst_1", "name": "Water", "emoji": "💧", "label": "💧 Water"}]
    )
    pending = {"instance_id": "inst_1", "element_id": "el_water", "name": "Water", "emoji": "💧"}

    with pytest.raises(alchemy.AlchemyVerificationError, match="still present"):
        alchemy.produced_from_second(before, before, pending)


def test_model_sends_one_semantic_request_and_validates_only_offered_target(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        operations = body["questions"]["operation"]["criteria"]
        targets = body["questions"]["add_element_target"]["criteria"]
        return {
            "model": "jev-latest",
            "answers": {
                "operation": choice(list(operations), "ADD_ELEMENT"),
                "add_element_target": choice(list(targets), "el_fire"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose_alchemy(
        {
            "phase": "pick_second",
            "inventory": observation()["inventory"],
            "pending": {"instance_id": "inst_1", "element_id": "el_water", "name": "Water", "emoji": "💧"},
            "known_results": [
                {
                    "first_id": "el_water",
                    "second_id": "el_earth",
                    "first": "Water",
                    "second": "Earth",
                    "status": "no_result",
                }
            ],
            "available_target_ids": ["el_water", "el_fire"],
            "recent_actions": [],
        },
        "Получи Steam",
    )

    assert len(calls) == 1
    assert decision["operation"] == "ADD_ELEMENT"
    assert decision["target"] == "el_fire"
    assert set(calls[0]["targets"]["ADD_ELEMENT"]) == {"el_water", "el_fire"}
    assert "BLOCKED" not in calls[0]["questions"]["operation"]["criteria"]
    assert "combined with itself" in calls[0]["questions"]["operation"]["instructions"]["rules"]
    assert all(key not in calls[0]["state"] for key in ("selector", "coordinate", "outerHTML"))
    assert all(key not in json.dumps(calls[0]["state"]) for key in ("outerHTML", "getBoundingClientRect"))


def test_model_done_does_not_require_a_target_head(monkeypatch):
    def post(_url, _key, body):
        operations = body["questions"]["operation"]["criteria"]
        return {
            "model": "jev-latest",
            "answers": {"operation": choice(list(operations), "DONE")},
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose_alchemy(
        {"phase": "pick_first", "inventory": observation()["inventory"]},
        "Получи Steam",
    )

    assert decision["choice"] == "DONE"
    assert decision["target"] is None


def test_terminal_model_request_asks_only_for_an_offered_element(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        targets = body["questions"]["add_element_target"]["criteria"]
        return {
            "model": "jev-latest",
            "answers": {"add_element_target": choice(list(targets), "el_fire")},
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose_alchemy_target(
        {
            "phase": "pick_second",
            "inventory": observation()["inventory"],
            "pending": {"instance_id": "inst_1", "element_id": "el_water", "name": "Water", "emoji": "💧"},
            "available_target_ids": ["el_water", "el_fire"],
            "known_results": [],
        },
        "Steam",
    )

    assert decision["operation"] == "ADD_ELEMENT"
    assert decision["target"] == "el_fire"
    assert set(calls[0]["questions"]) == {"add_element_target"}
    assert "DONE" not in json.dumps(calls[0])


def test_invalid_terminal_choice_is_retried(monkeypatch):
    calls = []
    sleeps = []

    def post(_url, _key, body):
        calls.append(body)
        targets = body["questions"]["add_element_target"]["criteria"]
        if len(calls) == 1:
            return {
                "model": "jev-latest",
                "answers": {
                    "add_element_target": {
                        "choice": "el_fire",
                        "confidence": 1.0,
                        "probabilities": {"el_fire": 1.0},
                    }
                },
            }
        return {
            "model": "jev-latest",
            "answers": {"add_element_target": choice(list(targets), "el_fire")},
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setattr(model.time, "sleep", sleeps.append)
    decision = model.choose_alchemy_target(
        {
            "phase": "pick_second",
            "inventory": observation()["inventory"],
            "pending": {"element_id": "el_water", "name": "Water", "emoji": "💧"},
            "available_target_ids": ["el_water", "el_fire"],
            "known_results": [],
        },
        "Steam",
    )

    assert decision["target"] == "el_fire"
    assert decision["request_attempts"] == 2
    assert len(calls) == 2
    assert sleeps == [0.5]


def test_model_provider_503_is_retried_before_returning_a_retryable_error(monkeypatch):
    responses = iter(
        [
            FakeResponse(503),
            FakeResponse(503, headers={"Retry-After": "0"}),
            FakeResponse(200, {"answers": {}}),
        ]
    )
    calls = []
    sleeps = []

    def post(*_args, **_kwargs):
        calls.append(True)
        return next(responses)

    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model.time, "sleep", sleeps.append)

    assert model.post_json("https://example.test", "key", {}) == {"answers": {}}
    assert len(calls) == 3
    assert sleeps == [0.5, 0.0]


def test_model_provider_503_is_distinguished_after_retries(monkeypatch):
    monkeypatch.setattr(model.CLIENT, "post", lambda *_args, **_kwargs: FakeResponse(503))
    monkeypatch.setattr(model.time, "sleep", lambda _seconds: None)

    with pytest.raises(model.TransientModelError, match="HTTP 503"):
        model.post_json("https://example.test", "key", {})


def test_drag_event_order_has_no_retry(monkeypatch):
    calls = []

    def fake_cdp(method, **params):
        calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"source": {"x": 10, "y": 20}, "drop": {"x": 100, "y": 120}}}}
        return {}

    monkeypatch.setattr(alchemy, "cdp", fake_cdp)
    monkeypatch.setattr(alchemy.time, "sleep", lambda _seconds: None)
    result = alchemy.alchemy_browser_operation(
        {
            "operation": "drag",
            "session": "session",
            "expected_marker": "marker",
            "action": {
                "id": "add:el_fire",
                "kind": "drag",
                "phase": "pick_second",
                "source_id": "el_fire",
                "drop_id": "inst_1",
            },
        }
    )

    events = [params["type"] for method, params in calls if method == "Input.dispatchMouseEvent"]
    assert result["drag_events"] == 11
    assert events[0:2] == ["mouseMoved", "mousePressed"]
    assert events[-1] == "mouseReleased"
    assert events.count("mouseMoved") == 9
    assert len([call for call in calls if call[0] == "Runtime.evaluate"]) == 1


def test_stale_drag_is_rejected_before_any_mouse_input(monkeypatch):
    calls = []

    def fake_cdp(method, **params):
        calls.append((method, params))
        return {"result": {"value": {"error": "stale"}}}

    monkeypatch.setattr(alchemy, "cdp", fake_cdp)
    with pytest.raises(StalePage):
        alchemy.alchemy_browser_operation(
            {
                "operation": "drag",
                "session": "session",
                "expected_marker": "marker",
                "action": {
                    "id": "add:el_fire",
                    "kind": "drag",
                    "phase": "pick_second",
                    "source_id": "el_fire",
                    "drop_id": "inst_1",
                },
            }
        )

    assert [method for method, _params in calls] == ["Runtime.evaluate"]


def test_adapter_records_success_and_no_result_history():
    before = observation()
    after_first = observation(
        instances=[{"id": "inst_1", "name": "Water", "emoji": "💧", "label": "💧 Water"}]
    )
    after_second = observation(
        instances=[{"id": "inst_2", "name": "Steam", "emoji": "💨", "label": "💨 Steam"}],
        inventory=[
            *before["inventory"],
            {"id": "el_steam", "name": "Steam", "emoji": "💨", "label": "💨 Steam"},
        ],
    )

    class FakeBrowser:
        def __init__(self):
            self.observations = iter([before, after_first, after_second])
            self.actions = []

        def observe(self, **_kwargs):
            return next(self.observations)

        def act(self, action, _page):
            self.actions.append(action)

        def close(self):
            pass

    browser = FakeBrowser()
    adapter = alchemy.AlchemyAdapter("unused", "Получи Steam", browser=browser)
    adapter.execute("el_water")
    outcome = adapter.execute("el_fire")

    assert outcome["status"] == "success"
    assert outcome["produced"]["name"] == "Steam"
    assert outcome["new_discovery"] is True
    assert adapter.phase == "pick_first"
    assert adapter.pending is None
    assert adapter.known_results[0]["first"] == "Water"
    assert adapter.known_results[0]["status"] == "success"
    assert adapter.known_results[0]["new_discovery"] is True
    assert [action["phase"] for action in browser.actions] == ["pick_first", "pick_second"]


def test_agent_rechecks_done_and_does_not_trust_the_model(monkeypatch):
    class FakeBrowser:
        def fresh(self, _page):
            return True

        def observe(self, **_kwargs):
            return observation()

        def close(self):
            pass

    class FakeAdapter:
        def __init__(self, _url, goal, **_kwargs):
            self.goal = goal
            self.browser = FakeBrowser()
            self.observation = observation()
            self.phase = "pick_first"
            self.pending = None
            self.known_results = []
            self.history = []
            self.satisfied = False

        def snapshot(self):
            return {"state": {"phase": self.phase, "inventory": self.observation["inventory"]}}

        def goal_satisfied(self):
            return self.satisfied

        def close(self):
            self.browser.close()

    monkeypatch.setattr(alchemy, "AlchemyAdapter", FakeAdapter)
    monkeypatch.setattr(
        alchemy,
        "choose_alchemy",
        lambda *_args: {
            "choice": "DONE",
            "operation": "DONE",
            "target": None,
            "confidence": 1.0,
            "probabilities": {"DONE": 1.0},
            "operation_probabilities": {"DONE": 1.0},
            "target_probabilities": {},
            "target_confidence": None,
            "model": "mock-jev",
            "usage": {},
            "latency_ms": 1,
            "request": {},
        },
    )
    agent = alchemy.AlchemyAgent("unused", "Get Steam")

    agent.predict()
    rejected = agent.act()
    assert rejected["status"] == "ready"
    assert rejected["last_error"] == "Jev suggested DONE, but the goal is not visible"
    assert agent.adapter.history[-1]["status"] == "unverified_done"

    agent.adapter.satisfied = True
    agent.predict()
    finished = agent.act()
    assert finished["status"] == "done"


def test_agent_pauses_for_a_transient_model_error_without_a_decision_or_drag(monkeypatch):
    class FakeBrowser:
        def fresh(self, _page):
            return True

        def close(self):
            pass

    class FakeAdapter:
        def __init__(self, _url, goal, **_kwargs):
            self.goal = goal
            self.browser = FakeBrowser()
            self.observation = observation()
            self.phase = "pick_first"
            self.pending = None
            self.known_results = []
            self.history = []

        def snapshot(self):
            return {"state": {"phase": self.phase, "inventory": self.observation["inventory"]}}

        def goal_satisfied(self):
            return False

        def close(self):
            self.browser.close()

    def fail(*_args, **_kwargs):
        raise model.TransientModelError("Model provider is busy (HTTP 503)")

    monkeypatch.setattr(alchemy, "AlchemyAdapter", FakeAdapter)
    monkeypatch.setattr(alchemy, "choose_alchemy", fail)
    agent = alchemy.AlchemyAgent("unused", "Human")

    paused = agent.command("tick")

    assert paused["status"] == "retryable"
    assert paused["last_error"] == "Model provider is busy (HTTP 503)"
    assert paused["decision"] is None
    assert agent.adapter.history == []
