"""Entra ID OIDC provider + the two browser-facing routes (auth design P3).

No network. A throwaway RSA keypair is generated per test module, its public
half is injected straight into the provider's JWKS cache, and id_tokens are
signed locally — so signature verification runs for real against tokens we
control, including the ones we deliberately malform.

The token endpoint is monkeypatched at ``_post_token``: everything below that
line is Microsoft's, everything above it is ours, and ours is what is under test.

Timestamps are always computed from ``datetime.now`` — never written as literal
dates, which is how the OPT-0041 fixture cohort turned into time bombs that
started failing on a date nobody chose.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-000000000000"
REDIRECT_URI = "https://analysis.example.com/api/v1/auth/callback"
KID = "test-signing-key-1"

EMAIL = "kieran@kohleservices.com"


# ── signing material ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def public_jwk(rsa_key):
    from jwt.algorithms import RSAAlgorithm

    jwk = RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return jwk


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_id_token(
    rsa_key,
    *,
    nonce: str,
    email: str | None = EMAIL,
    name: str | None = "Kieran Xiang",
    audience: str = CLIENT_ID,
    issuer: str | None = None,
    tenant: str | None = TENANT,
    expires_in: timedelta = timedelta(minutes=10),
    kid: str = KID,
    algorithm: str = "RS256",
) -> str:
    now = _now()
    claims = {
        "iss": issuer or f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": audience,
        "sub": "some-stable-subject",
        "oid": "some-object-id",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "nonce": nonce,
    }
    if email is not None:
        claims["email"] = email
    if name is not None:
        claims["name"] = name
    if tenant is not None:
        claims["tid"] = tenant
    return jwt.encode(claims, rsa_key, algorithm=algorithm, headers={"kid": kid})


# ── environment ──────────────────────────────────────────────────────────────

@pytest.fixture
def entra_env(tmp_path, monkeypatch, public_jwk):
    """Configured provider, isolated users.db, JWKS pre-seeded (so no network)."""
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", "boss@kohleservices.com")
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("ENTRA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.setenv("ENTRA_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_ENABLED", "true")
    # TestClient speaks http://testserver, and httpx will not send a Secure
    # cookie back over http. backend/.env (which config.py load_dotenv()s) sets
    # Secure for production, so pin it here rather than inherit it.
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)

    from app.core import users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    from app.services.auth.providers import entra_oidc

    entra_oidc._seed_jwks_cache({KID: public_jwk})

    yield entra_oidc

    entra_oidc.reset_jwks_cache()
    users_db.reset_connection_cache()


def start_login(entra_oidc, **kwargs) -> tuple[str, str]:
    """Run authorize_url() and dig the state + nonce back out of the DB."""
    from urllib.parse import parse_qs, urlparse

    url = entra_oidc.authorize_url(**kwargs)
    params = parse_qs(urlparse(url).query)
    state = params["state"][0]

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        row = conn.execute(
            "SELECT nonce FROM oidc_transactions WHERE state = ?", (state,)
        ).fetchone()
    return state, row["nonce"]


# ── authorize_url ────────────────────────────────────────────────────────────

def test_authorize_url_carries_pkce_and_a_stored_state(entra_env):
    from urllib.parse import parse_qs, urlparse

    url = entra_env.authorize_url(return_to="/risk-monitor")
    parsed = urlparse(url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert parsed.netloc == "login.microsoftonline.com"
    assert parsed.path == f"/{TENANT}/oauth2/v2.0/authorize"
    assert params["client_id"] == CLIENT_ID
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == REDIRECT_URI
    # S256, never "plain" — a plain challenge is no protection at all, and Entra
    # accepts it, so nothing external would catch the mistake.
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"]
    assert "openid" in params["scope"]

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        row = conn.execute(
            "SELECT * FROM oidc_transactions WHERE state = ?", (params["state"],)
        ).fetchone()
    assert row is not None
    assert row["return_to"] == "/risk-monitor"
    assert row["nonce"] == params["nonce"]
    # The verifier stays server-side; only its hash goes to Microsoft.
    assert row["code_verifier"] not in url


def test_code_challenge_is_the_sha256_of_the_stored_verifier(entra_env):
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    url = entra_env.authorize_url()
    params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        row = conn.execute(
            "SELECT code_verifier FROM oidc_transactions WHERE state = ?",
            (params["state"],),
        ).fetchone()

    digest = hashlib.sha256(row["code_verifier"].encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert params["code_challenge"] == expected


def test_every_login_gets_a_fresh_state_and_nonce(entra_env):
    first = start_login(entra_env)
    second = start_login(entra_env)
    assert first[0] != second[0]
    assert first[1] != second[1]


def test_authorize_url_refuses_when_provider_is_unconfigured(entra_env, monkeypatch):
    from app.services.auth.providers.base import ProviderError

    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "")
    with pytest.raises(ProviderError) as exc:
        entra_env.authorize_url()
    assert exc.value.code == "provider_disabled"


# ── exchange_code: the happy path ────────────────────────────────────────────

def _patch_token_endpoint(monkeypatch, entra_oidc, id_token: str | None):
    payload = {"id_token": id_token} if id_token is not None else {}
    monkeypatch.setattr(entra_oidc, "_post_token", lambda **kwargs: payload)


def test_exchange_code_returns_the_identity(entra_env, monkeypatch, rsa_key):
    state, nonce = start_login(entra_env, return_to="/risk-watchlist")
    _patch_token_endpoint(monkeypatch, entra_env, make_id_token(rsa_key, nonce=nonce))

    identity, return_to = entra_env.exchange_code("the-code", state)

    assert identity.email == EMAIL
    assert identity.display_name == "Kieran Xiang"
    assert identity.source == "entra"
    assert return_to == "/risk-watchlist"


def test_email_is_lowercased(entra_env, monkeypatch, rsa_key):
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch,
        entra_env,
        make_id_token(rsa_key, nonce=nonce, email="Kieran.Xiang@KohleServices.com"),
    )

    identity, _ = entra_env.exchange_code("the-code", state)
    assert identity.email == "kieran.xiang@kohleservices.com"


def test_token_exchange_sends_back_the_stored_verifier_and_redirect_uri(
    entra_env, monkeypatch, rsa_key
):
    """PKCE only works if the verifier we stored is the one we redeem with."""
    state, nonce = start_login(entra_env)

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        stored = conn.execute(
            "SELECT code_verifier FROM oidc_transactions WHERE state = ?", (state,)
        ).fetchone()["code_verifier"]

    seen: dict = {}

    def fake_post(**kwargs):
        seen.update(kwargs)
        return {"id_token": make_id_token(rsa_key, nonce=nonce)}

    monkeypatch.setattr(entra_env, "_post_token", fake_post)
    entra_env.exchange_code("the-code", state)

    assert seen["code_verifier"] == stored
    assert seen["redirect_uri"] == REDIRECT_URI
    assert seen["code"] == "the-code"


# ── exchange_code: the refusals ──────────────────────────────────────────────

def _expect_code(entra_oidc, code: str, state: str, expected: str):
    from app.services.auth.providers.base import ProviderError

    with pytest.raises(ProviderError) as exc:
        entra_oidc.exchange_code(code, state)
    assert exc.value.code == expected


def test_missing_email_claim_is_an_error_not_a_upn_fallback(
    entra_env, monkeypatch, rsa_key
):
    """The single most consequential rule in P3 (design doc §8.1, §7.6).

    Entra falls back to the UPN when the directory's `mail` attribute is unset,
    and the UPN here can be @*.onmicrosoft.com. Accepting it would provision a
    SECOND users row for someone who already has one, splitting their role,
    status and audit history the day email OTP (P6) lands.
    """
    state, nonce = start_login(entra_env)
    token = make_id_token(rsa_key, nonce=nonce, email=None)
    # preferred_username present and tempting — must still be refused.
    _patch_token_endpoint(monkeypatch, entra_env, token)

    _expect_code(entra_env, "the-code", state, "no_email_claim")


def test_blank_email_claim_is_also_refused(entra_env, monkeypatch, rsa_key):
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch, entra_env, make_id_token(rsa_key, nonce=nonce, email="   ")
    )
    _expect_code(entra_env, "the-code", state, "no_email_claim")


def test_state_is_single_use(entra_env, monkeypatch, rsa_key):
    """Replaying a callback URL must not mint a second session."""
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(monkeypatch, entra_env, make_id_token(rsa_key, nonce=nonce))

    entra_env.exchange_code("the-code", state)
    _expect_code(entra_env, "the-code", state, "state_unknown")


def test_unknown_state_is_refused(entra_env):
    _expect_code(entra_env, "the-code", "never-issued-this", "state_unknown")


def test_missing_state_is_refused(entra_env):
    _expect_code(entra_env, "the-code", "", "state_missing")


def test_missing_code_is_refused(entra_env):
    state, _ = start_login(entra_env)
    _expect_code(entra_env, "", state, "code_missing")


def test_expired_state_is_refused(entra_env, monkeypatch, rsa_key):
    state, nonce = start_login(entra_env)

    # Age the transaction past its TTL rather than sleeping.
    from app.core.users_db import get_users_db

    past = (_now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_users_db() as conn:
        conn.execute(
            "UPDATE oidc_transactions SET expires_at = ? WHERE state = ?", (past, state)
        )

    _patch_token_endpoint(monkeypatch, entra_env, make_id_token(rsa_key, nonce=nonce))
    _expect_code(entra_env, "the-code", state, "state_expired")


def test_nonce_mismatch_is_refused(entra_env, monkeypatch, rsa_key):
    """Binds the id_token to OUR login attempt, which state alone does not."""
    state, _ = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch, entra_env, make_id_token(rsa_key, nonce="a-different-nonce")
    )
    _expect_code(entra_env, "the-code", state, "id_token_bad_nonce")


def test_wrong_audience_is_refused(entra_env, monkeypatch, rsa_key):
    """A token minted for another app in the same tenant is not ours."""
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch,
        entra_env,
        make_id_token(rsa_key, nonce=nonce, audience="some-other-app-id"),
    )
    _expect_code(entra_env, "the-code", state, "id_token_bad_audience")


def test_wrong_issuer_is_refused(entra_env, monkeypatch, rsa_key):
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch,
        entra_env,
        make_id_token(
            rsa_key, nonce=nonce, issuer="https://login.microsoftonline.com/evil/v2.0"
        ),
    )
    _expect_code(entra_env, "the-code", state, "id_token_bad_issuer")


def test_foreign_tenant_is_refused(entra_env, monkeypatch, rsa_key):
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch,
        entra_env,
        make_id_token(rsa_key, nonce=nonce, tenant="99999999-9999-9999-9999-999999999999"),
    )
    _expect_code(entra_env, "the-code", state, "id_token_bad_tenant")


def test_expired_id_token_is_refused(entra_env, monkeypatch, rsa_key):
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch,
        entra_env,
        # Beyond the 60s clock-skew leeway.
        make_id_token(rsa_key, nonce=nonce, expires_in=timedelta(minutes=-5)),
    )
    _expect_code(entra_env, "the-code", state, "id_token_expired")


def test_token_signed_by_an_unknown_key_is_refused(entra_env, monkeypatch):
    """A valid-looking token signed by someone else's key.

    The JWKS cache is seeded and the kid is unknown, so this also exercises the
    refresh-on-miss path — which fails closed here because there is no network.
    """
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch, entra_env, make_id_token(other_key, nonce=nonce, kid="not-our-kid")
    )

    from app.services.auth.providers.base import ProviderError

    with pytest.raises(ProviderError) as exc:
        entra_env.exchange_code("the-code", state)
    # Either outcome is a refusal: the refresh attempt fails without a network,
    # or (with one) the kid is still absent. Both fail closed, never open.
    assert exc.value.code in {"jwks_unavailable", "unknown_signing_key"}


def test_token_signed_with_the_wrong_key_for_a_known_kid_is_refused(
    entra_env, monkeypatch
):
    """Same kid, different key — i.e. an actual forgery attempt."""
    forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(monkeypatch, entra_env, make_id_token(forger, nonce=nonce))
    _expect_code(entra_env, "the-code", state, "id_token_invalid")


def test_unsigned_token_is_refused(entra_env, monkeypatch):
    """`alg: none` — the textbook JWT bypass. The header is never trusted."""
    state, nonce = start_login(entra_env)
    unsigned = jwt.encode(
        {"aud": CLIENT_ID, "nonce": nonce, "email": EMAIL},
        key="",
        algorithm="none",
        headers={"kid": KID},
    )
    _patch_token_endpoint(monkeypatch, entra_env, unsigned)
    _expect_code(entra_env, "the-code", state, "id_token_bad_alg")


def test_missing_id_token_in_the_token_response_is_refused(entra_env, monkeypatch):
    state, _ = start_login(entra_env)
    _patch_token_endpoint(monkeypatch, entra_env, None)
    _expect_code(entra_env, "the-code", state, "no_id_token")


# ── return_to open-redirect guard ────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/\\evil.example",
        "http://evil.example",
        "javascript:alert(1)",
        "evil.example",
        "/ok\r\nX-Injected: 1",
        "/" + "a" * 600,
        "",
        None,
    ],
)
def test_return_to_rejects_anything_not_a_local_path(raw):
    """/auth/login is reachable by anyone, so this is the open-redirect guard.

    Without it, a link to OUR domain would bounce the victim to an attacker's
    site carrying our credibility — the classic phishing amplifier.
    """
    from app.api.v1.routes.auth import sanitize_return_to

    assert sanitize_return_to(raw) is None


@pytest.mark.parametrize(
    "raw",
    ["/", "/risk-monitor", "/login-ips?tab=3", "/cs/ib-tree#anchor"],
)
def test_return_to_accepts_in_app_paths(raw):
    from app.api.v1.routes.auth import sanitize_return_to

    assert sanitize_return_to(raw) == raw


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(entra_env):
    from app.api.v1.routes.auth import router as auth_router
    from app.core.auth_middleware import AuthMiddleware

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    return TestClient(app, follow_redirects=False)


def test_login_route_redirects_to_microsoft(client):
    r = client.get("/api/v1/auth/login")
    assert r.status_code == 303
    assert r.headers["location"].startswith(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?"
    )


def test_login_route_drops_a_hostile_return_to(client):
    client.get("/api/v1/auth/login?return_to=https://evil.example")

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        row = conn.execute("SELECT return_to FROM oidc_transactions").fetchone()
    assert row["return_to"] is None


def test_callback_sets_the_session_cookie_and_returns_to_the_app(
    client, entra_env, monkeypatch, rsa_key
):
    state, nonce = start_login(entra_env, return_to="/risk-monitor")
    _patch_token_endpoint(monkeypatch, entra_env, make_id_token(rsa_key, nonce=nonce))

    r = client.get(f"/api/v1/auth/callback?code=abc&state={state}")

    assert r.status_code == 303
    assert r.headers["location"] == "/risk-monitor"
    assert "kcm_sid" in r.cookies
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie


def test_callback_defaults_to_the_app_root(client, entra_env, monkeypatch, rsa_key):
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(monkeypatch, entra_env, make_id_token(rsa_key, nonce=nonce))

    r = client.get(f"/api/v1/auth/callback?code=abc&state={state}")
    assert r.headers["location"] == "/"


def test_callback_creates_the_user_and_the_session(
    client, entra_env, monkeypatch, rsa_key
):
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(monkeypatch, entra_env, make_id_token(rsa_key, nonce=nonce))
    client.get(f"/api/v1/auth/callback?code=abc&state={state}")

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (EMAIL,)).fetchone()
        sessions = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
        event = conn.execute(
            "SELECT * FROM auth_events WHERE event = 'login_success'"
        ).fetchone()

    assert user["source"] == "entra"
    assert user["display_name"] == "Kieran Xiang"
    assert sessions == 1
    assert event["email"] == EMAIL


def test_callback_bounces_an_outside_domain_to_the_login_page(
    client, entra_env, monkeypatch, rsa_key
):
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch,
        entra_env,
        make_id_token(rsa_key, nonce=nonce, email="someone@gmail.com"),
    )

    r = client.get(f"/api/v1/auth/callback?code=abc&state={state}")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=not_authorized"
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_callback_reports_a_provider_error_as_a_short_code(
    client, entra_env, monkeypatch, rsa_key
):
    """The browser gets a code; the detail stays in the log."""
    state, _ = start_login(entra_env)
    _patch_token_endpoint(
        monkeypatch, entra_env, make_id_token(rsa_key, nonce="wrong-nonce")
    )

    r = client.get(f"/api/v1/auth/callback?code=abc&state={state}")
    assert r.headers["location"] == "/login?error=id_token_bad_nonce"


def test_callback_handles_microsoft_refusing(client):
    """e.g. the user is not assigned to the app (Assignment required = Yes)."""
    r = client.get(
        "/api/v1/auth/callback?error=access_denied&error_description=AADSTS50105"
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=idp_refused"

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        event = conn.execute(
            "SELECT * FROM auth_events WHERE event = 'login_failure'"
        ).fetchone()
    assert "idp_error:access_denied" in event["detail"]


def test_login_route_survives_an_unconfigured_provider(client, monkeypatch):
    """Never a 500: the user lands on the login page with a reason."""
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "")
    r = client.get("/api/v1/auth/login")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=provider_disabled"


def test_the_session_from_a_callback_actually_authenticates(
    client, entra_env, monkeypatch, rsa_key
):
    """End to end: the cookie the callback sets resolves on the next request."""
    state, nonce = start_login(entra_env)
    _patch_token_endpoint(monkeypatch, entra_env, make_id_token(rsa_key, nonce=nonce))
    client.get(f"/api/v1/auth/callback?code=abc&state={state}")

    me = client.get("/api/v1/auth/me").json()
    assert me["authenticated"] is True
    assert me["email"] == EMAIL
    assert me["role"] == "user"


