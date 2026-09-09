"""Tests for api/api_auth.py's load_auth_state()/is_protected() - the
bootstrap and lockout-avoidance logic behind the optional password
protection feature added in PR #63, plus two later additions that both
touch this same bootstrap path: the P1 #5 stabilization follow-up
(config.example.py now defaults AUTH_ENABLED=True with no hash, so a
fresh install with no data/auth.json yet must generate and persist a
real one-time password instead of the old "stays silently disabled"
behavior - see load_auth_state()'s own docstring/comments), and
_is_safe_redirect_target()/the login route's `next` handling (open-
redirect fix, see that function's own docstring for the live-reproduced
vulnerability this replaced). No server.py import needed; this module
has no hardware/CLI dependencies of its own.
"""

import json
import os
import stat
import sys
import threading
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.security import check_password_hash, generate_password_hash

import api.api_auth as api_auth
from api.api_auth import (
    _is_safe_redirect_target,
    _login_throttle_delay,
    is_protected,
    load_auth_state,
    register_auth_routes,
)


_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _make_app(tmp_path, password="realpassword123", enabled=True):
    auth_file = tmp_path / "auth.json"
    app = Flask(__name__, template_folder=_TEMPLATE_DIR)
    app.secret_key = "test-secret-key"
    state_lock = threading.RLock()
    auth_state = {"enabled": enabled, "password_hash": generate_password_hash(password)}
    register_auth_routes(app, state_lock, auth_state, str(auth_file), lambda f: f)
    return app, auth_state


def test_load_auth_state_first_run_defaults_to_disabled(tmp_path):
    auth_file = tmp_path / "auth.json"  # does not exist yet
    state = load_auth_state(str(auth_file))
    assert state == {"enabled": False, "password_hash": ""}


def test_load_auth_state_no_bootstrap_args_never_calls_the_generator(tmp_path, monkeypatch):
    # AUTH_ENABLED=False in config.py (bootstrap_enabled defaults to
    # False) must not just "not activate" generation - the generator must
    # never even run. A spy proves that, not just the resulting state.
    generator = MagicMock(wraps=api_auth._generate_initial_password)
    monkeypatch.setattr(api_auth, "_generate_initial_password", generator)

    auth_file = tmp_path / "auth.json"
    state = load_auth_state(str(auth_file))

    assert state == {"enabled": False, "password_hash": ""}
    generator.assert_not_called()
    assert not auth_file.exists()
    assert not (tmp_path / "initial_password.txt").exists()


def test_load_auth_state_bootstrap_enabled_without_hash_generates_a_password(tmp_path):
    # New behavior (P1 #5): config.py's AUTH_ENABLED=True with an empty
    # AUTH_PASSWORD_HASH - config.example.py's new default - must
    # generate a real password and persist it, not stay silently
    # disabled. The old "never lock you out with no password set"
    # invariant is preserved differently now: the generated hash always
    # corresponds to a password the operator is shown, not "no password
    # required at all".
    auth_file = tmp_path / "auth.json"
    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")

    assert state["enabled"] is True
    assert state["password_hash"], "must generate a real hash, not stay empty"

    # Persisted to disk immediately - unlike the plain in-memory bootstrap
    # path, nothing else would ever write auth.json for this case.
    on_disk = json.loads(auth_file.read_text(encoding="utf-8"))
    assert on_disk == state

    # The plaintext copy is written next to auth.json, and check_password_hash
    # against it must match what got persisted - proves the printed/saved
    # plaintext is the actual password behind the stored hash, not a
    # decoy or a different generation call.
    password_file = tmp_path / "initial_password.txt"
    assert password_file.exists()
    plaintext = password_file.read_text(encoding="utf-8").strip()
    assert check_password_hash(state["password_hash"], plaintext)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits not meaningful on Windows")
def test_load_auth_state_generated_password_file_is_owner_only(tmp_path):
    auth_file = tmp_path / "auth.json"
    load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")

    password_file = tmp_path / "initial_password.txt"
    mode = stat.S_IMODE(password_file.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)} - plaintext password file must be owner-only"


def test_load_auth_state_generation_never_writes_config_py_hash_back(tmp_path):
    # Documented recovery path relies on this: config.py's own
    # AUTH_PASSWORD_HASH must stay empty after generation, so deleting
    # auth.json and restarting re-bootstraps to disabled (see
    # test_load_auth_state_no_bootstrap_args_never_calls_the_generator),
    # not back to the same generated password. load_auth_state() only
    # ever receives config.py's values as function arguments - it has no
    # way to write back to config.py, but this test pins that this stays
    # true even if that changes.
    auth_file = tmp_path / "auth.json"
    load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")

    # Re-running with the ORIGINAL (still-empty) bootstrap hash, as
    # config.py itself would supply on every restart, must now hit the
    # "auth.json already exists" early-return branch, not generate again.
    state_again = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")
    first_hash = json.loads(auth_file.read_text(encoding="utf-8"))["password_hash"]
    assert state_again["password_hash"] == first_hash


def test_load_auth_state_bootstrap_enabled_with_hash_is_enabled(tmp_path):
    auth_file = tmp_path / "auth.json"
    password_hash = generate_password_hash("correct horse battery staple")
    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash=password_hash)
    assert state == {"enabled": True, "password_hash": password_hash}


def test_load_auth_state_existing_file_ignores_bootstrap_args(tmp_path):
    # Once auth.json exists (set via the Settings UI), it's the source of
    # truth - config.py's AUTH_ENABLED/AUTH_PASSWORD_HASH must not override
    # a value the user already changed at runtime.
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"enabled": True, "password_hash": "stored-hash"}), encoding="utf-8")

    state = load_auth_state(str(auth_file), bootstrap_enabled=False, bootstrap_password_hash="")
    assert state == {"enabled": True, "password_hash": "stored-hash"}


def test_load_auth_state_existing_file_never_calls_the_generator(tmp_path, monkeypatch):
    # Same scenario as test_load_auth_state_existing_file_ignores_bootstrap_args,
    # but proving the generation branch is physically unreachable once
    # auth.json exists - not just "produces the same result as if it had
    # run and then been overridden". bootstrap_enabled=True here
    # specifically to make sure an existing file wins even when the
    # config.py values that WOULD trigger generation are also present.
    generator = MagicMock(wraps=api_auth._generate_initial_password)
    monkeypatch.setattr(api_auth, "_generate_initial_password", generator)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"enabled": True, "password_hash": "stored-hash"}), encoding="utf-8")

    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")

    assert state == {"enabled": True, "password_hash": "stored-hash"}
    generator.assert_not_called()
    assert not (tmp_path / "initial_password.txt").exists()


def test_load_auth_state_tolerates_corrupt_file(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{not valid json", encoding="utf-8")

    # safe_read_json() falls back to {} on a JSON decode error - load_auth_state()
    # must then treat that the same as "no file yet" (bootstrap path), not crash.
    state = load_auth_state(str(auth_file))
    assert state == {"enabled": False, "password_hash": ""}


def test_is_protected_requires_both_enabled_and_a_real_hash():
    assert is_protected({"enabled": True, "password_hash": "somehash"}) is True
    assert is_protected({"enabled": True, "password_hash": ""}) is False
    assert is_protected({"enabled": True, "password_hash": "   "}) is False  # whitespace-only
    assert is_protected({"enabled": False, "password_hash": "somehash"}) is False
    assert is_protected({"enabled": False, "password_hash": ""}) is False


def test_is_protected_tolerates_missing_keys():
    assert is_protected({}) is False
    assert is_protected({"enabled": True}) is False
    assert is_protected({"password_hash": "somehash"}) is False


def test_is_safe_redirect_target_accepts_ordinary_same_origin_paths():
    assert _is_safe_redirect_target("/") is True
    assert _is_safe_redirect_target("/map") is True
    assert _is_safe_redirect_target("/settings?tab=general") is True
    assert _is_safe_redirect_target("/chat#bottom") is True


def test_is_safe_redirect_target_rejects_open_redirects():
    # Already blocked by the old startswith("/")/startswith("//") check.
    assert _is_safe_redirect_target("//evil.example") is False
    assert _is_safe_redirect_target("http://evil.example") is False
    assert _is_safe_redirect_target("https://evil.example") is False
    assert _is_safe_redirect_target("javascript:alert(1)") is False
    assert _is_safe_redirect_target("") is False
    assert _is_safe_redirect_target(None) is False

    # Backslash - independently verified NOT exploitable against this
    # project's Werkzeug version (percent-encoded to %5C before the
    # Location header is sent - see the function's own docstring), but
    # rejected explicitly anyway since urlsplit() alone does not catch it
    # and relying solely on Werkzeug's current encoding behavior would be
    # fragile.
    assert _is_safe_redirect_target("/\\evil.example") is False
    assert _is_safe_redirect_target("/\\/evil.example") is False

    # The actual live vulnerability, independently reproduced via a real
    # Flask test client + real browser navigation against the OLD check
    # (see this module's git history / the investigation report): a tab
    # character is silently stripped while Werkzeug builds the Location
    # header, turning "/\t/evil.example" into "//evil.example" - a
    # protocol-relative absolute URL - *after* a naive prefix check
    # already let it through. Both the raw tab and its percent-encoded
    # form (as it would actually arrive in a query string) must be
    # rejected.
    assert _is_safe_redirect_target("/\t/evil.example") is False

    # Control characters (ASCII 0-31 and 127) must be rejected to prevent
    # URL manipulation, open redirects, or HTTP header injection.
    assert _is_safe_redirect_target("/\x0b/evil.example") is False
    assert _is_safe_redirect_target("/\x0c/evil.example") is False
    assert _is_safe_redirect_target("/\r\n/evil.example") is False
    assert _is_safe_redirect_target("/\x7f/evil.example") is False
    assert _is_safe_redirect_target("/\x00/evil.example") is False


def test_login_redirect_rejects_the_live_reproduced_open_redirect(tmp_path):
    # End-to-end regression test through the real route (not just the
    # helper function in isolation): a real login POST with the tab-
    # stripping payload URL-encoded in the query string exactly as an
    # attacker-supplied link would deliver it, against the actual
    # register_auth_routes() code, not a hand-copied replica.
    auth_file = tmp_path / "auth.json"
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    state_lock = threading.RLock()
    auth_state = {"enabled": True, "password_hash": generate_password_hash("realpassword123")}
    register_auth_routes(app, state_lock, auth_state, str(auth_file), lambda f: f)

    client = app.test_client()
    resp = client.post(
        "/login?next=/%09/evil.example/marker",
        data={"password": "realpassword123"},
    )
    assert resp.status_code == 302
    assert resp.headers.get("Location") == "/"


def test_login_redirect_still_honors_a_legitimate_next_url(tmp_path):
    auth_file = tmp_path / "auth.json"
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    state_lock = threading.RLock()
    auth_state = {"enabled": True, "password_hash": generate_password_hash("realpassword123")}
    register_auth_routes(app, state_lock, auth_state, str(auth_file), lambda f: f)

    client = app.test_client()
    resp = client.post("/login?next=/map", data={"password": "realpassword123"})
    assert resp.status_code == 302
    assert resp.headers.get("Location") == "/map"


# --- P1 #6: MIN_PASSWORD_LENGTH ---------------------------------------------

def test_min_password_length_is_12_not_the_old_4():
    # Pins the actual constant, not just behavior derived from it - a
    # regression here (e.g. someone "simplifying" back to 4) must fail
    # loudly on this line alone.
    assert api_auth.MIN_PASSWORD_LENGTH == 12


def test_generated_initial_password_passes_the_real_min_length_validation(tmp_path):
    # P1 #6 explicitly asked not to assume the 16-hex-char generated
    # password (P1 #5) clears the raised MIN_PASSWORD_LENGTH - prove it by
    # running the actual generator output through the actual validation
    # route (api_update_security), not a hand-copied len() check.
    app, _ = _make_app(tmp_path)
    client = app.test_client()

    generated = api_auth._generate_initial_password()
    assert len(generated) >= api_auth.MIN_PASSWORD_LENGTH, (
        f"generated password is {len(generated)} chars, below MIN_PASSWORD_LENGTH="
        f"{api_auth.MIN_PASSWORD_LENGTH}"
    )

    with client.session_transaction() as sess:
        sess["authenticated"] = True  # /api/security itself needs a session
        # /api/security is a mutating /api/ route, so it also needs the
        # project-wide CSRF token (Step 1.6A.0) alongside the auth session.
        sess["csrf_token"] = "test-csrf-token"

    resp = client.post(
        "/api/security",
        json={"password": generated},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    data = resp.get_json()
    assert data["ok"] is True, data
    assert data.get("error_code") != "password_too_short"


# --- P1 #6: login throttling -------------------------------------------------

def test_login_throttle_delay_boundary_between_5th_and_6th_failure():
    # The exact off-by-one the task called out: with 5 free attempts, the
    # 5th failed *attempt* must still render as a plain error (not 429) -
    # which requires the lockout to already be armed by the time the 5th
    # failure is *recorded*, so it blocks attempt #6. delay(fail_count) is
    # "the delay this many recorded failures impose on the NEXT attempt",
    # not on the attempt that just happened.
    assert _login_throttle_delay(1) == 0
    assert _login_throttle_delay(4) == 0  # attempt 5 still goes through unthrottled
    assert _login_throttle_delay(5) == 2  # ...but now attempt 6 is blocked
    assert _login_throttle_delay(6) == 4
    assert _login_throttle_delay(7) == 8


def test_login_throttle_delay_caps_at_max_seconds():
    assert _login_throttle_delay(100) == api_auth._LOGIN_THROTTLE_MAX_SECONDS


def test_login_throttle_delay_stays_capped_for_very_large_fail_counts():
    # fail_count has no upper bound (a sustained attacker keeps refreshing
    # last_seen, so the TTL sweep never reclaims the entry) - correctness
    # must hold far past anything the boundary tests exercise.
    for fail_count in (1_000, 1_000_000, 10 ** 9):
        assert _login_throttle_delay(fail_count) == api_auth._LOGIN_THROTTLE_MAX_SECONDS


def test_login_throttle_max_exponent_constant_is_actually_sufficient():
    # The invariant _LOGIN_THROTTLE_MAX_EXPONENT relies on: that exponent
    # alone must already reach _LOGIN_THROTTLE_MAX_SECONDS, so the function
    # never needs (and the code never computes) a larger one - this is what
    # makes capping the exponent safe rather than just "usually right". If
    # someone tightens MAX_SECONDS or loosens BASE_SECONDS without touching
    # this constant, this test catches the cap becoming insufficient.
    assert (
        api_auth._LOGIN_THROTTLE_BASE_SECONDS * (2 ** api_auth._LOGIN_THROTTLE_MAX_EXPONENT)
        >= api_auth._LOGIN_THROTTLE_MAX_SECONDS
    )


def test_login_5th_wrong_attempt_still_plain_error_6th_is_throttled(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, _ = _make_app(tmp_path)
    client = app.test_client()
    overrides = {"REMOTE_ADDR": "10.0.0.5"}

    for attempt in range(1, 6):  # attempts 1..5 - all free
        resp = client.post(
            "/login", data={"password": "wrong"}, environ_overrides=overrides
        )
        assert resp.status_code == 200, f"attempt {attempt} should render the plain login form, got {resp.status_code}"
        assert b"auth.login_error" in resp.data or b"Incorrect password" in resp.data
        assert resp.status_code != 429

    # 6th failure - now throttled.
    resp = client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
    assert resp.status_code == 429
    assert b'id="loginError"' in resp.data  # throttled branch, not the plain login_error one

    # Correct password no longer helps while still locked out - proves the
    # lockout check runs before the password is even checked.
    resp = client.post("/login", data={"password": "realpassword123"}, environ_overrides=overrides)
    assert resp.status_code == 429


def test_login_throttle_lifts_once_the_backoff_window_elapses(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, _ = _make_app(tmp_path)
    client = app.test_client()
    overrides = {"REMOTE_ADDR": "10.0.0.6"}

    for _ in range(6):  # trip the throttle (6th failure -> 2s lockout)
        client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)

    resp = client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
    assert resp.status_code == 429

    fake_now[0] += 3  # past the 2s lockout window
    resp = client.post("/login", data={"password": "realpassword123"}, environ_overrides=overrides)
    assert resp.status_code == 302
    assert resp.headers.get("Location") == "/"


def test_login_throttle_is_scoped_per_source_ip(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, _ = _make_app(tmp_path)
    client = app.test_client()

    for _ in range(6):
        client.post("/login", data={"password": "wrong"}, environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    blocked = client.post("/login", data={"password": "wrong"}, environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    assert blocked.status_code == 429

    # A different source IP is unaffected by the first one's failures.
    resp = client.post(
        "/login", data={"password": "realpassword123"}, environ_overrides={"REMOTE_ADDR": "10.0.0.8"}
    )
    assert resp.status_code == 302


def test_login_success_resets_the_failure_counter(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, _ = _make_app(tmp_path)
    client = app.test_client()
    overrides = {"REMOTE_ADDR": "10.0.0.9"}

    for _ in range(4):  # under the free-attempt threshold
        client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)

    resp = client.post("/login", data={"password": "realpassword123"}, environ_overrides=overrides)
    assert resp.status_code == 302  # logged in - counter should now be cleared

    # A fresh session for the same IP fails again - if the counter had NOT
    # been reset, this would already be past the free-attempt threshold.
    client2 = app.test_client()
    resp = client2.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
    assert resp.status_code == 200
    assert b'id="loginError"' not in resp.data


def test_login_throttle_never_engages_while_auth_is_disabled(tmp_path, monkeypatch):
    # Explicit check requested for the interaction with `if not protected`:
    # while auth is disabled, login() returns its early redirect before the
    # throttle code ever runs (see api_auth.py's login(), the `if not
    # protected: return redirect("/")` line precedes the `if request.method
    # == "POST":` throttle block) - so failed attempts against a disabled
    # instance must never pre-warm a lockout for when protection is later
    # turned on from the UI.
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, auth_state = _make_app(tmp_path, enabled=False)
    client = app.test_client()
    overrides = {"REMOTE_ADDR": "10.0.0.10"}

    for _ in range(10):  # far past the free-attempt threshold, if it counted at all
        resp = client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
        assert resp.status_code == 302
        assert resp.headers.get("Location") == "/"

    # Now flip protection on, exactly as api_update_security() would via
    # the Settings UI, and confirm the very first attempt against this same
    # IP is treated as attempt #1, not #11.
    auth_state["enabled"] = True
    resp = client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
    assert resp.status_code == 200, "must render the ordinary error form, not be pre-throttled"
    assert b'id="loginError"' not in resp.data
