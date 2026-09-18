"""Direct client for Infinite Craft's public pair endpoint."""

import time
from collections import deque
from dataclasses import dataclass

from curl_cffi.requests import Session

BASE_URL = "https://neal.fun"
PAIR_PATH = "/api/infinite-craft/pair"
IMPERSONATE = "chrome120"
RETRY_DELAYS = (0.5, 1.0, 2.0)
RETRYABLE_STATUSES = {500, 502, 503, 504}
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "priority": "u=1, i",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": '"Not_A Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/infinite-craft/",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
}


class InfiniteCraftApiError(RuntimeError):
    """The pair endpoint could not return a usable result."""


class NealRateLimitError(InfiniteCraftApiError):
    """Neal.fun returned 429; retrying can prolong the IP cooldown."""


@dataclass(frozen=True)
class PairResult:
    name: str | None
    emoji: str = ""
    is_new: bool | None = None


class InfiniteCraftApi:
    """Small synchronous client with local pair caching and a conservative rate limit."""

    def __init__(self, *, rate_limit=60, window_seconds=60.0, timeout=15.0, session=None):
        if rate_limit < 1:
            raise ValueError("rate_limit must be positive")
        self.rate_limit = rate_limit
        self.window_seconds = window_seconds
        self.timeout = timeout
        self._session = session
        self._owns_session = session is None
        self._timestamps = deque()
        self._cache = {}

    def __enter__(self):
        self._ensure_session()
        try:
            self._session.get(
                BASE_URL,
                allow_redirects=True,
                verify=True,
                impersonate=IMPERSONATE,
                timeout=self.timeout,
            )
        except Exception as error:
            raise InfiniteCraftApiError("Could not initialize a Neal.fun session") from error
        return self

    def __exit__(self, *_args):
        self.close()

    def _ensure_session(self):
        if self._session is None:
            self._session = Session(impersonate=IMPERSONATE, headers=HEADERS)

    def close(self):
        if self._owns_session and self._session is not None:
            self._session.close()
        self._session = None if self._owns_session else self._session

    def _acquire_slot(self):
        while True:
            now = time.monotonic()
            while self._timestamps and self._timestamps[0] + self.window_seconds <= now:
                self._timestamps.popleft()
            if len(self._timestamps) < self.rate_limit:
                self._timestamps.append(now)
                return
            wait = self._timestamps[0] + self.window_seconds - now
            time.sleep(min(max(wait, 0.01), 1.0))

    @staticmethod
    def _pair_key(first, second):
        return tuple(sorted((first.strip().casefold(), second.strip().casefold())))

    def pair(self, first, second):
        first = str(first).strip()
        second = str(second).strip()
        if not first or not second:
            raise ValueError("Both Infinite Craft elements are required")
        key = self._pair_key(first, second)
        if key in self._cache:
            return self._cache[key]

        self._ensure_session()
        self._acquire_slot()
        last_error = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                response = self._session.get(
                    f"{BASE_URL}{PAIR_PATH}",
                    params={"first": first, "second": second},
                    allow_redirects=True,
                    verify=True,
                    impersonate=IMPERSONATE,
                    timeout=self.timeout,
                )
            except Exception as error:
                last_error = error
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise InfiniteCraftApiError("Infinite Craft API connection failed") from error
            if response.status_code == 429:
                raise NealRateLimitError("Neal.fun rate limit reached; stop and retry later")
            if response.status_code in RETRYABLE_STATUSES and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            if response.status_code >= 400:
                raise InfiniteCraftApiError(f"Infinite Craft API returned HTTP {response.status_code}")
            try:
                payload = response.json()
            except Exception as error:
                raise InfiniteCraftApiError("Infinite Craft API returned invalid JSON") from error
            break
        else:
            raise InfiniteCraftApiError("Infinite Craft API request failed") from last_error

        result_name = payload.get("result")
        if result_name == "Nothing":
            result_name = None
        if result_name is not None and not isinstance(result_name, str):
            raise InfiniteCraftApiError("Infinite Craft API returned an invalid result name")
        result = PairResult(
            name=result_name,
            emoji=payload.get("emoji") or "",
            is_new=payload.get("isNew"),
        )
        self._cache[key] = result
        return result
