"""Entra ID (Azure AD) OIDC provider — Authorization Code + PKCE (auth design P3).

    /auth/login     -> authorize_url()  : mint state/nonce/verifier, 302 to Microsoft
    /auth/callback  -> exchange_code()  : swap code for tokens, verify, return identity

The identity is handed to ``auth_service.login()``, which owns provisioning,
sessions and audit. This module knows nothing about cookies or sessions.

Why confidential client + PKCE, not one or the other
----------------------------------------------------
The client secret proves *the backend* is the one redeeming the code; PKCE
proves the redemption belongs to *the same browser round trip* that started.
Only the pair closes both holes, and the design doc's §8.1 note applies here:
Entra's discovery metadata leaves ``code_challenge_methods_supported`` empty but
Entra v2 does support S256 — do not read that empty field as "no PKCE".

Endpoints are derived from the tenant id rather than read from the discovery
document. The v2 URL shapes are stable and contractual, and deriving them keeps
a Microsoft outage from turning "slow login" into "no login at all"; the one
document we genuinely cannot hardcode — the signing keys — is fetched and
cached below.

⚠ The email claim is mandatory and there is NO fallback to ``preferred_username``
(design doc §8.1, §7.6): Entra falls back to the UPN when ``mail`` is unset, and
the UPN here can be ``@*.onmicrosoft.com``, which would both fail the domain
allowlist and — worse — provision a second users row for a person who already
has one, splitting their history the day email OTP (P6) lands. A missing claim
is a directory misconfiguration and must surface as one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt
import requests
from jwt.algorithms import RSAAlgorithm

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.core.users_db import get_users_db

from .base import AuthenticatedIdentity, ProviderError

logger = get_logger(__name__)

SOURCE = "entra"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Microsoft rejects a token request that takes too long anyway; a bounded
# timeout keeps a hung IdP from parking a uvicorn worker for minutes.
_HTTP_TIMEOUT_SECONDS = 10

# Signing keys rotate on the order of weeks. Re-fetching every 6h is ample, and
# an unknown `kid` forces an immediate refresh regardless (see _signing_key),
# so a surprise rotation costs one extra HTTPS call rather than an outage.
_JWKS_TTL_SECONDS = 6 * 3600

# Small leeway for clock skew between this host and Microsoft.
_CLOCK_SKEW_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ── endpoints ────────────────────────────────────────────────────────────────

def _authority(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}"


def authorize_endpoint(tenant_id: str) -> str:
    return f"{_authority(tenant_id)}/oauth2/v2.0/authorize"


def token_endpoint(tenant_id: str) -> str:
    return f"{_authority(tenant_id)}/oauth2/v2.0/token"


def jwks_endpoint(tenant_id: str) -> str:
    return f"{_authority(tenant_id)}/discovery/v2.0/keys"


def expected_issuer(tenant_id: str) -> str:
    return f"{_authority(tenant_id)}/v2.0"


# ── OIDC transaction store ───────────────────────────────────────────────────
#
# These rows are provider-scoped and transient, which is why they live here and
# not in auth_service (whose whole point is to have no IdP knowledge). They must
# be in the DB rather than in memory: prod runs 4 uvicorn workers and /login and
# /callback are separate requests — see the users_db module docstring.

def _purge_expired_transactions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM oidc_transactions WHERE expires_at <= ?", (_fmt(_now()),))


def _store_transaction(
    *,
    state: str,
    nonce: str,
    code_verifier: str,
    redirect_uri: str,
    return_to: str | None,
    ip: str | None,
    user_agent: str | None,
) -> None:
    now = _now()
    ttl = get_settings().ENTRA_TRANSACTION_TTL_MINUTES
    with get_users_db() as conn:
        _purge_expired_transactions(conn)
        conn.execute(
            "INSERT INTO oidc_transactions "
            "(state, nonce, code_verifier, redirect_uri, return_to, created_at, "
            " expires_at, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state,
                nonce,
                code_verifier,
                redirect_uri,
                return_to,
                _fmt(now),
                _fmt(now + timedelta(minutes=ttl)),
                ip,
                (user_agent or "")[:300] or None,
            ),
        )


def _consume_transaction(state: str) -> sqlite3.Row:
    """Fetch and DELETE the transaction for ``state``. Single use, always.

    Deleting on read is what makes an authorization-code replay fail: the second
    callback carrying the same state finds nothing. Expiry is checked after the
    delete so a stale row cannot be retried either.
    """
    if not state:
        raise ProviderError("state_missing", "Callback carried no state parameter")

    with get_users_db() as conn:
        row = conn.execute(
            "SELECT * FROM oidc_transactions WHERE state = ?", (state,)
        ).fetchone()
        if row is not None:
            conn.execute("DELETE FROM oidc_transactions WHERE state = ?", (state,))

    if row is None:
        # Unknown state means one of: CSRF attempt, a replayed callback, a stale
        # bookmark, or a browser that sat on the login page past the TTL.
        raise ProviderError("state_unknown", "No pending login for this state")

    if _fmt(_now()) >= row["expires_at"]:
        raise ProviderError("state_expired", "The login attempt timed out")

    return row


# ── step 1: redirect to Microsoft ────────────────────────────────────────────

def authorize_url(
    *,
    return_to: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Start a login. Persists the transaction and returns the URL to 302 to."""
    settings = get_settings()
    if not settings.ENTRA_ENABLED:
        raise ProviderError("provider_disabled", "Entra OIDC is not configured")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    # 43 chars from 32 random bytes, inside RFC 7636's 43-128 range.
    code_verifier = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())

    _store_transaction(
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        redirect_uri=settings.ENTRA_REDIRECT_URI,
        return_to=return_to,
        ip=ip,
        user_agent=user_agent,
    )

    params = {
        "client_id": settings.ENTRA_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.ENTRA_REDIRECT_URI,
        "response_mode": "query",
        # No offline_access: we do not call Graph and hold no refresh token —
        # the session is ours, and its lifetime is governed by users.db, not by
        # Microsoft's token expiry.
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorize_endpoint(settings.ENTRA_TENANT_ID)}?{urlencode(params)}"


# ── JWKS ─────────────────────────────────────────────────────────────────────

_jwks_lock = threading.Lock()
_jwks_cache: dict[str, dict] = {}      # kid -> JWK dict
_jwks_fetched_at: float = 0.0


def _fetch_jwks(tenant_id: str) -> dict[str, dict]:
    resp = requests.get(jwks_endpoint(tenant_id), timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    keys = resp.json().get("keys") or []
    return {k["kid"]: k for k in keys if k.get("kid")}


def _signing_key(tenant_id: str, kid: str):
    """Return the public key for ``kid``, refreshing the cache if it is unknown.

    Refresh-on-miss is the part that matters: without it, the first token signed
    by a newly rotated key locks everyone out until the TTL happens to lapse.
    """
    global _jwks_fetched_at

    with _jwks_lock:
        age = _now().timestamp() - _jwks_fetched_at
        stale = age > _JWKS_TTL_SECONDS
        if kid not in _jwks_cache or stale:
            try:
                _jwks_cache.clear()
                _jwks_cache.update(_fetch_jwks(tenant_id))
                _jwks_fetched_at = _now().timestamp()
            except (requests.RequestException, ValueError, KeyError) as exc:
                raise ProviderError(
                    "jwks_unavailable", f"Could not fetch Entra signing keys: {exc}"
                ) from exc
        jwk = _jwks_cache.get(kid)

    if jwk is None:
        raise ProviderError("unknown_signing_key", f"Entra signed with unknown kid {kid!r}")
    return RSAAlgorithm.from_jwk(json.dumps(jwk))


def reset_jwks_cache() -> None:
    """Drop the cached signing keys. Used by tests; harmless in production."""
    global _jwks_fetched_at
    with _jwks_lock:
        _jwks_cache.clear()
        _jwks_fetched_at = 0.0


def _seed_jwks_cache(keys: dict[str, dict]) -> None:
    """Install signing keys without a network call. Tests only."""
    global _jwks_fetched_at
    with _jwks_lock:
        _jwks_cache.clear()
        _jwks_cache.update(keys)
        _jwks_fetched_at = _now().timestamp()


# ── step 2: redeem the code ──────────────────────────────────────────────────

def _post_token(*, code: str, redirect_uri: str, code_verifier: str) -> dict:
    settings = get_settings()
    try:
        resp = requests.post(
            token_endpoint(settings.ENTRA_TENANT_ID),
            data={
                "client_id": settings.ENTRA_CLIENT_ID,
                "client_secret": settings.ENTRA_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "scope": "openid profile email",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ProviderError("token_endpoint_unreachable", f"Token request failed: {exc}") from exc

    if resp.status_code != 200:
        # AADSTS7000215 (bad secret) and AADSTS700016 (unknown app) both land
        # here; logging the code is what makes an expired secret diagnosable in
        # seconds instead of looking like a generic 500 (design doc §8.4).
        detail = resp.text[:500]
        raise ProviderError(
            "token_exchange_failed", f"Entra returned {resp.status_code}: {detail}"
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError("token_response_malformed", "Token response was not JSON") from exc


def verify_id_token(id_token: str, *, nonce: str) -> dict:
    """Validate signature, issuer, audience, expiry and nonce. Returns claims."""
    settings = get_settings()

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise ProviderError("id_token_malformed", f"Unreadable id_token: {exc}") from exc

    # Pin the algorithm from our side. Trusting the header's `alg` is how the
    # classic "alg: none" / HS256-with-the-public-key forgeries work.
    if header.get("alg") != "RS256":
        raise ProviderError("id_token_bad_alg", f"Unexpected alg {header.get('alg')!r}")

    kid = header.get("kid")
    if not kid:
        raise ProviderError("id_token_no_kid", "id_token header carries no kid")

    key = _signing_key(settings.ENTRA_TENANT_ID, kid)

    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=settings.ENTRA_CLIENT_ID,
            issuer=expected_issuer(settings.ENTRA_TENANT_ID),
            leeway=_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ProviderError("id_token_expired", "id_token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise ProviderError("id_token_bad_audience", f"Wrong audience: {exc}") from exc
    except jwt.InvalidIssuerError as exc:
        raise ProviderError("id_token_bad_issuer", f"Wrong issuer: {exc}") from exc
    except jwt.PyJWTError as exc:
        raise ProviderError("id_token_invalid", f"id_token rejected: {exc}") from exc

    # Replay binding: the nonce ties this token to the /auth/login that we
    # started, which the state parameter alone does not guarantee.
    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise ProviderError("id_token_bad_nonce", "id_token nonce does not match")

    # Single-tenant app registration; a token minted in another tenant must not
    # be accepted even if it somehow satisfies issuer and audience.
    tid = str(claims.get("tid") or "")
    if tid and tid != settings.ENTRA_TENANT_ID:
        raise ProviderError("id_token_bad_tenant", f"Token from foreign tenant {tid!r}")

    return claims


def identity_from_claims(claims: dict) -> AuthenticatedIdentity:
    """Extract the person. NO fallback to preferred_username — see module docstring."""
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise ProviderError(
            "no_email_claim",
            "id_token carried no email claim — check the app registration's "
            "optional claims and that the directory user has a 'mail' attribute",
        )

    display_name = str(claims.get("name") or "").strip() or None
    return AuthenticatedIdentity(email=email, display_name=display_name, source=SOURCE)


def exchange_code(code: str, state: str) -> tuple[AuthenticatedIdentity, str | None]:
    """Complete the round trip. Returns ``(identity, return_to)``.

    Raises ProviderError for every failure mode; the route turns ``exc.code``
    into a short query parameter and never shows ``str(exc)`` to the browser.
    """
    if not code:
        raise ProviderError("code_missing", "Callback carried no authorization code")

    txn = _consume_transaction(state)

    payload = _post_token(
        code=code,
        redirect_uri=txn["redirect_uri"],
        code_verifier=txn["code_verifier"],
    )

    id_token = payload.get("id_token")
    if not id_token:
        raise ProviderError("no_id_token", "Token response carried no id_token")

    claims = verify_id_token(id_token, nonce=txn["nonce"])
    identity = identity_from_claims(claims)

    logger.info(
        "Entra OIDC verified %s (oid=%s)", identity.email, claims.get("oid", "?")
    )
    return identity, txn["return_to"]
