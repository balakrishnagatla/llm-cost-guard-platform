"""Access control for the proxy and dashboard.

Proxy keys (COST_GUARD_KEYS): give each team its own key, e.g.
    COST_GUARD_KEYS=search:k_8f3a...,support:k_91bc...
Callers send it as `X-Cost-Guard-Key`. The team is taken from the key, so it
can't be faked with an X-Team header. If COST_GUARD_KEYS is empty the proxy runs
in open "dev mode" (fine on localhost only).

Dashboard login (DASHBOARD_PASSWORD): protects /, /dashboard and /stats with
HTTP Basic auth (user: DASHBOARD_USER, default "admin").
"""
import base64
import functools
import hashlib
import hmac
import os
import re
import secrets

from fastapi import HTTPException, Request

_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _parse_keys(raw: str) -> dict:
    keys = {}
    for item in filter(None, (p.strip() for p in raw.split(","))):
        team, sep, key = item.partition(":")
        if not sep or not team.strip() or len(key.strip()) < 16:
            raise ValueError(f"Bad COST_GUARD_KEYS entry for team '{team}': use team:key with a key of 16+ characters")
        keys[key.strip()] = clean_label(team.strip())
    return keys


def proxy_keys() -> dict:
    return _parse_keys(os.getenv("COST_GUARD_KEYS", ""))


def clean_label(value: str | None, default: str = "unknown") -> str:
    """Team/feature names end up in the database and dashboard; keep them boring."""
    if not value:
        return default
    value = value.strip()
    return value if _LABEL_RE.match(value) else "invalid-label"


def resolve_team(request: Request) -> str:
    """Authenticate the caller and return its team."""
    keys = proxy_keys()
    if not keys:  # dev mode
        return clean_label(request.headers.get("x-team"))
    supplied = request.headers.get("x-cost-guard-key", "")
    for key, team in keys.items():
        if hmac.compare_digest(supplied.encode(), key.encode()):
            return team
    raise HTTPException(401, "Missing or invalid X-Cost-Guard-Key")


_FINGERPRINT_ITERATIONS = 100_000
_salt: bytes | None = None


def _fingerprint_salt() -> bytes:
    """Random per-deployment salt, kept next to the database so cache keys survive restarts.

    If the data directory isn't writable, a per-process salt is used (cached answers then
    don't survive a restart, which is safe).
    """
    global _salt
    if _salt is None:
        path = os.path.join(os.path.dirname(os.getenv("DB_PATH", "data/costguard.db")) or ".", "fingerprint.salt")
        try:
            with open(path, "rb") as f:
                _salt = f.read()
        except FileNotFoundError:
            _salt = secrets.token_bytes(32)
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(_salt)
            except FileExistsError:  # another worker created it first
                with open(path, "rb") as f:
                    _salt = f.read()
            except OSError:
                pass
    return _salt


@functools.lru_cache(maxsize=1024)
def _derive(credential: str) -> str:
    # A slow, salted key derivation, so a leaked cache table can't be used to test guesses
    # of the credential offline. Computed once per credential per process.
    return hashlib.pbkdf2_hmac("sha256", credential.encode(), _fingerprint_salt(),
                               _FINGERPRINT_ITERATIONS).hex()[:32]


def credential_fingerprint(headers: dict) -> str:
    """Opaque per-credential tag, so cached answers never cross API keys."""
    credential = headers.get("Authorization") or headers.get("x-api-key") or ""
    return _derive(credential) if credential else "none"


def require_dashboard_login(request: Request) -> None:
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not password:
        return
    user = os.getenv("DASHBOARD_USER", "admin")
    auth = request.headers.get("authorization", "")
    ok = False
    if auth.lower().startswith("basic "):
        try:
            got_user, _, got_pw = base64.b64decode(auth[6:]).decode().partition(":")
            ok = hmac.compare_digest(got_user.encode(), user.encode()) & hmac.compare_digest(
                got_pw.encode(), password.encode())
        except (ValueError, UnicodeDecodeError):
            ok = False
    if not ok:
        raise HTTPException(401, "Login required", headers={"WWW-Authenticate": 'Basic realm="LLM Cost Guard"'})


def parse_budgets(raw: str) -> dict:
    """BUDGETS=search:5,support:2.50  -> daily USD limit per team."""
    budgets = {}
    for item in filter(None, (p.strip() for p in raw.split(","))):
        team, _, amount = item.partition(":")
        try:
            budgets[clean_label(team)] = float(amount)
        except ValueError:
            raise ValueError(f"Bad BUDGETS entry '{item}': use team:dollars")
    return budgets
