"""Transient-failure retry in db.py.

Mirror of agent-kalamata's tests/test_db_transient_retry.py, adapted to this
repo's SupabaseClient (which routes reads AND writes through _request, where
agent-kalamata's select/count have their own requests calls).

Two halves, deliberately:
  * transient failures DO retry -- TLS drop, read timeout, 503, and the
    count() path;
  * nothing else moves -- writes take exactly one attempt unless they opt in,
    a 404 and a ValueError pass straight through untouched, the backoff cap
    survives an absurd Retry-After, and the budget stops the loop with
    attempts still left.
"""

import sys
import types

import pytest
import requests

import db as sx_db


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.headers = headers or {}
        self.text = text if text is not None else ('[]' if payload is None else 'x')

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


def _ssl_drop():
    return requests.exceptions.SSLError(
        "EOF occurred in violation of protocol (_ssl.c:1077)")


def _read_timeout():
    return requests.exceptions.ReadTimeout("timed out")


def _client():
    return sx_db.SupabaseClient('https://example.supabase.co', 'test-key')


@pytest.fixture
def slept(monkeypatch):
    """Record sleeps instead of taking them, so the suite stays fast."""
    calls = []
    monkeypatch.setattr(sx_db.time, 'sleep', lambda s: calls.append(s))
    return calls


def _script(monkeypatch, outcomes):
    """Make requests.request replay `outcomes` in order.

    An outcome that is an Exception instance is raised; anything else is
    returned. Records every call so a test can assert the attempt count.
    """
    calls = []
    queue = list(outcomes)

    def fake_request(method, url, **kwargs):
        calls.append({'method': method, 'url': url, 'kwargs': kwargs})
        outcome = queue.pop(0) if queue else _FakeResponse(200, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sx_db.requests, 'request', fake_request)
    return calls


# --------------------------------------------------------------------------
# HALF ONE -- transient failures retry
# --------------------------------------------------------------------------

def test_a_tls_drop_on_a_read_is_retried_and_then_succeeds(monkeypatch, slept):
    """The exact failure that cost agent-kalamata a run on 2026-09-07."""
    calls = _script(monkeypatch, [_ssl_drop(), _FakeResponse(200, [{'id': 1}])])
    rows = _client().select('profiles', '*', limit=10)
    assert rows == [{'id': 1}]
    assert len(calls) == 2
    assert len(slept) == 1


def test_a_read_timeout_is_retried(monkeypatch, slept):
    calls = _script(monkeypatch, [_read_timeout(), _FakeResponse(200, [])])
    _client().select('profiles', '*', limit=10)
    assert len(calls) == 2


def test_a_503_is_retried(monkeypatch, slept):
    calls = _script(monkeypatch, [_FakeResponse(503), _FakeResponse(200, [])])
    _client().select('profiles', '*', limit=10)
    assert len(calls) == 2


def test_a_429_is_retried(monkeypatch, slept):
    calls = _script(monkeypatch, [_FakeResponse(429), _FakeResponse(200, [])])
    _client().select('profiles', '*', limit=10)
    assert len(calls) == 2


def test_count_retries_too(monkeypatch, slept):
    calls = _script(monkeypatch, [
        _ssl_drop(),
        _FakeResponse(200, [], headers={'Content-Range': '0-9/42'}),
    ])
    assert _client().count('profiles') == 42
    assert len(calls) == 2


def test_get_usage_logs_retries(monkeypatch, slept):
    """Codex round 1 [P2]: this module-level read called requests.get directly
    and so bypassed the layer. It matters more than most, because its own
    `except` turns any failure into an empty list -- a one-second blip would
    render an empty usage table as if there had been no usage."""
    calls = _script(monkeypatch, [_ssl_drop(), _FakeResponse(200, [{'id': 7}])])
    assert sx_db.get_usage_logs(_client()) == [{'id': 7}]
    assert len(calls) == 2


def test_get_search_history_retries(monkeypatch, slept):
    """Same [P2], same swallow-into-empty-list shape."""
    calls = _script(monkeypatch, [_read_timeout(), _FakeResponse(200, [{'id': 9}])])
    assert sx_db.get_search_history(_client()) == [{'id': 9}]
    assert len(calls) == 2


def test_every_supabase_read_in_this_module_goes_through_the_retry_helper():
    """Structural guard so a NEW read added later cannot quietly bypass the
    layer the way these two did. Writes are exempt on purpose -- they must not
    replay (see the block comment in db.py)."""
    import inspect
    src = inspect.getsource(sx_db)
    stray = [
        line.strip()
        for line in src.splitlines()
        if 'requests.get(' in line and not line.strip().startswith('#')
    ]
    assert stray == [], f"read(s) bypassing _request_with_retry: {stray}"


def test_the_attempt_budget_is_three_and_then_it_re_raises(monkeypatch, slept):
    calls = _script(monkeypatch, [_ssl_drop(), _ssl_drop(), _ssl_drop()])
    with pytest.raises(requests.exceptions.SSLError):
        _client().select('profiles', '*', limit=10)
    assert len(calls) == sx_db._RETRY_MAX_ATTEMPTS == 3
    assert len(slept) == 2


def test_a_5xx_that_never_clears_is_handed_back_not_swallowed(monkeypatch, slept):
    """On the last attempt the helper returns exactly what one attempt would,
    so the caller's own raise_for_status still decides."""
    calls = _script(monkeypatch, [_FakeResponse(500)] * 3)
    with pytest.raises(requests.exceptions.HTTPError):
        _client().select('profiles', '*', limit=10)
    assert len(calls) == 3


# --------------------------------------------------------------------------
# HALF TWO -- nothing else moves
# --------------------------------------------------------------------------

def test_insert_is_never_retried_because_a_replay_would_duplicate(monkeypatch, slept):
    calls = _script(monkeypatch, [_ssl_drop()])
    with pytest.raises(requests.exceptions.SSLError):
        _client().insert('profiles', {'linkedin_url': 'x'})
    assert len(calls) == 1
    assert slept == []


def test_update_takes_exactly_one_attempt_by_default(monkeypatch, slept):
    calls = _script(monkeypatch, [_ssl_drop()])
    with pytest.raises(requests.exceptions.SSLError):
        _client().update('profiles', {'a': 1}, {'id': 1})
    assert len(calls) == 1
    assert slept == []


def test_update_retries_when_a_call_site_opts_in(monkeypatch, slept):
    """No caller opts in today; the mechanism still has to work when one does."""
    calls = _script(monkeypatch, [_ssl_drop(), _FakeResponse(200, [{'id': 1}])])
    assert _client().update('profiles', {'a': 1}, {'id': 1},
                            retry_transient=True) == [{'id': 1}]
    assert len(calls) == 2


def test_delete_takes_exactly_one_attempt(monkeypatch, slept):
    calls = _script(monkeypatch, [_ssl_drop()])
    with pytest.raises(requests.exceptions.SSLError):
        _client().delete('profiles', {'id': 1})
    assert len(calls) == 1


def test_a_404_passes_straight_through_without_retrying(monkeypatch, slept):
    """A 4xx is a logical answer, not a transient failure."""
    calls = _script(monkeypatch, [_FakeResponse(404)])
    with pytest.raises(requests.exceptions.HTTPError):
        _client().select('profiles', '*', limit=10)
    assert len(calls) == 1
    assert slept == []


def test_a_409_conflict_is_not_retried(monkeypatch, slept):
    """agent-kalamata answers a 409 + Postgres 23505 as its one-run lock. The
    status must stay outside the retryable set in both files."""
    assert 409 not in sx_db._RETRY_STATUS_CODES
    calls = _script(monkeypatch, [_FakeResponse(409)])
    with pytest.raises(requests.exceptions.HTTPError):
        _client().insert('pipeline_runs', {'position_id': 'p'})
    assert len(calls) == 1


def test_a_non_transport_exception_is_not_caught(monkeypatch, slept):
    calls = _script(monkeypatch, [ValueError('boom')])
    with pytest.raises(ValueError):
        _client().select('profiles', '*', limit=10)
    assert len(calls) == 1


def test_the_retryable_set_is_an_allowlist_not_anything_over_400():
    assert sx_db._RETRY_STATUS_CODES == frozenset({429, 500, 502, 503, 504})
    for logical in (400, 401, 403, 404, 409, 416, 422):
        assert logical not in sx_db._RETRY_STATUS_CODES


# --------------------------------------------------------------------------
# backoff and budget
# --------------------------------------------------------------------------

def test_the_backoff_is_capped_even_against_an_absurd_retry_after():
    resp = _FakeResponse(429, headers={'Retry-After': '86400'})
    assert sx_db._retry_delay_seconds(1, resp) <= sx_db._RETRY_MAX_DELAY_SECONDS * 1.25


def test_a_server_supplied_retry_after_raises_the_floor():
    resp = _FakeResponse(429, headers={'Retry-After': '3'})
    assert sx_db._retry_delay_seconds(1, resp) >= 3.0


def test_a_malformed_retry_after_is_ignored():
    resp = _FakeResponse(429, headers={'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'})
    assert sx_db._retry_after_seconds(resp) is None


def test_the_budget_stops_the_loop_even_with_attempts_left(monkeypatch, slept):
    """The RETRYING is bounded: once the budget is gone the loop stops and
    re-raises rather than working through its remaining attempts.

    Scope, stated precisely: this bounds the sleeps and extra attempts this
    layer adds, NOT any single attempt's own duration. `requests`' timeout is
    per-socket, so a slow-drip peer can outlive the budget inside one attempt
    -- exactly as it could before this layer existed."""
    monkeypatch.setattr(sx_db, '_RETRY_MAX_ATTEMPTS', 50)
    monkeypatch.setattr(sx_db, '_RETRY_DEADLINE_SECONDS', 0.0)
    calls = _script(monkeypatch, [_ssl_drop()])
    with pytest.raises(requests.exceptions.SSLError):
        _client().select('profiles', '*', limit=10)
    assert len(calls) == 1
    assert slept == []


def test_a_retry_is_refused_when_too_little_budget_remains(monkeypatch, slept):
    """Checked before the attempt starts, not only before the sleep, so an
    attempt cannot begin inside the budget and finish far outside it."""
    monkeypatch.setattr(sx_db, '_RETRY_DEADLINE_SECONDS',
                        sx_db._RETRY_MIN_ATTEMPT_SECONDS / 2)
    calls = _script(monkeypatch, [_ssl_drop(), _FakeResponse(200, [])])
    with pytest.raises(requests.exceptions.SSLError):
        _client().select('profiles', '*', limit=10)
    assert len(calls) == 1


def test_a_retrys_timeout_is_capped_to_the_budget_left():
    capped = sx_db._timeout_capped_to({'timeout': 90}, 7.5)
    assert capped['timeout'] == 7.5


def test_a_timeout_already_inside_the_budget_is_left_alone():
    original = {'timeout': 5}
    assert sx_db._timeout_capped_to(original, 60.0) is original


def test_a_tuple_timeout_is_left_alone():
    original = {'timeout': (3, 30)}
    assert sx_db._timeout_capped_to(original, 1.0) is original


def test_the_first_attempt_keeps_the_callers_own_timeout(monkeypatch, slept):
    calls = _script(monkeypatch, [_FakeResponse(200, [])])
    _client().select('profiles', '*', limit=10)
    assert calls[0]['kwargs']['timeout'] == 90


# --------------------------------------------------------------------------
# the shared-file contract with agent-kalamata
# --------------------------------------------------------------------------

def test_the_constants_match_agent_kalamatas_core_db():
    """These two files share one Supabase database and must behave
    identically. If a constant here is changed, change it there too."""
    assert sx_db._RETRY_MAX_ATTEMPTS == 3
    assert sx_db._RETRY_BASE_DELAY_SECONDS == 0.5
    assert sx_db._RETRY_MAX_DELAY_SECONDS == 4.0
    assert sx_db._RETRY_DEADLINE_SECONDS == 120.0
    assert sx_db._RETRY_MIN_ATTEMPT_SECONDS == 5.0
    assert sx_db._RETRY_STATUS_CODES == frozenset({429, 500, 502, 503, 504})
    assert sx_db._RETRYABLE_EXCEPTIONS == (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )


def test_a_transient_failure_is_logged_to_stderr_without_the_query_string(monkeypatch, slept, capsys):
    """The endpoint only -- never headers (they carry the service key) and
    never the query string (it carries candidate LinkedIn URLs)."""
    _script(monkeypatch, [_ssl_drop(), _FakeResponse(200, [])])
    _client().select('profiles', '*', filters={'linkedin_url': 'eq.secret-person'},
                     limit=10)
    err = capsys.readouterr().err
    assert '/profiles' in err
    assert 'secret-person' not in err
    assert 'test-key' not in err
