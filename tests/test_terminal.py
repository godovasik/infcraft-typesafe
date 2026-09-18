import pytest

from jev_ultrafast import alchemy_api, terminal


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_api_caches_a_pair_in_both_directions():
    session = FakeSession(FakeResponse(200, {"result": "Steam", "emoji": "💨", "isNew": True}))
    client = alchemy_api.InfiniteCraftApi(session=session, rate_limit=100)

    with client:
        first = client.pair("Water", "Fire")
        second = client.pair("Fire", "Water")

    pair_calls = [url for url, _kwargs in session.calls if url.endswith("/api/infinite-craft/pair")]
    assert first == second
    assert first.name == "Steam"
    assert len(pair_calls) == 1


def test_api_stands_down_on_neal_rate_limit():
    session = FakeSession(FakeResponse(429))
    client = alchemy_api.InfiniteCraftApi(session=session, rate_limit=100)

    with client, pytest.raises(alchemy_api.NealRateLimitError):
        client.pair("Water", "Fire")

    pair_calls = [url for url, _kwargs in session.calls if url.endswith("/api/infinite-craft/pair")]
    assert len(pair_calls) == 1


def test_terminal_runner_prints_verified_pair(monkeypatch):
    class FakeApi:
        def __init__(self):
            self.pairs = []

        def pair(self, first, second):
            self.pairs.append((first, second))
            return alchemy_api.PairResult("Steam", "💨", True)

    def choose(state, _goal, _history):
        target = "el_1" if state["phase"] == "pick_first" else "el_2"
        return {"target": target}

    monkeypatch.setattr(terminal, "choose_alchemy_target", choose)
    api = FakeApi()
    output = []
    agent = terminal.TerminalAlchemyAgent("Steam", api=api)

    assert agent.run(output.append) == "done"
    assert output == [
        "target: Steam",
        "💧 Water + 🔥 Fire = 💨 Steam ✦",
        "found: Steam",
    ]
    assert api.pairs == [("Water", "Fire")]


def test_probabilistic_selection_samples_jev_distribution():
    class FakeRng:
        def random(self):
            return 0.9

    agent = terminal.TerminalAlchemyAgent(
        "Steam",
        api=None,
        selection_mode="probabilistic",
        rng=FakeRng(),
    )

    selected, probability = agent._select_target(
        {
            "target": "el_1",
            "target_probabilities": {"el_1": 0.75, "el_2": 0.25},
        }
    )

    assert selected == "el_2"
    assert probability == 0.25


def test_probabilistic_runner_prints_selected_probabilities(monkeypatch):
    class FakeApi:
        def pair(self, first, second):
            assert (first, second) == ("Water", "Fire")
            return alchemy_api.PairResult("Steam", "💨", True)

    class FakeRng:
        values = iter((0.1, 0.2))

        def random(self):
            return next(self.values)

    def choose(state, _goal, _history):
        if state["phase"] == "pick_first":
            return {
                "target": "el_1",
                "target_probabilities": {"el_1": 0.75, "el_2": 0.25},
            }
        return {
            "target": "el_2",
            "target_probabilities": {"el_1": 0.1, "el_2": 0.9},
        }

    monkeypatch.setattr(terminal, "choose_alchemy_target", choose)
    output = []
    agent = terminal.TerminalAlchemyAgent(
        "Steam",
        api=FakeApi(),
        selection_mode="probabilistic",
        rng=FakeRng(),
    )

    assert agent.run(output.append) == "done"
    assert output == [
        "target: Steam [probabilistic selection]",
        "💧 Water (75%) + 🔥 Fire (90%) = 💨 Steam ✦",
        "found: Steam",
    ]
