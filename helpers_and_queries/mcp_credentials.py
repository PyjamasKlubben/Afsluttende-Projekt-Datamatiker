"""
mcp_credentials.py

Drop dette fil i jeres MCP-server projekt.
Brug get_user_env() ved session-start for at hente brugerens credentials
som et dict der kan bruges direkte som miljøvariabler.

Eksempel:
    from mcp_credentials import get_user_env

    async def handle_session(jwt_token: str):
        env = await get_user_env(jwt_token)
        openai_key = env.get("OPENAI_API_KEY")
"""

import os
import time
import logging

import httpx

logger = logging.getLogger(__name__)

CREDENTIALS_SERVICE_URL = os.environ.get("CREDENTIALS_SERVICE_URL")
SYSTEM_USER_ID = os.environ.get("SYSTEM_USER_ID", "system")

_CACHE_TTL = 60  # sekunder

_system_cache: dict[str, str] = {}
_system_cache_ts: float = 0.0


async def get_system_credentials() -> dict[str, str]:
    """Hent system-credentials fra credentials-servicen (async). Caches i 60 sekunder."""
    global _system_cache, _system_cache_ts
    if _system_cache and time.monotonic() - _system_cache_ts < _CACHE_TTL:
        return _system_cache
    url = f"{CREDENTIALS_SERVICE_URL}/internal/credentials/{SYSTEM_USER_ID}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            _system_cache = resp.json()
            _system_cache_ts = time.monotonic()
            logger.info("Loaded %d system credentials", len(_system_cache))
        else:
            logger.error("Credentials service returned %d for system user", resp.status_code)
    except httpx.RequestError as e:
        logger.error("Could not reach credentials service: %s", e)
    return _system_cache


def get_system_credentials_sync() -> dict[str, str]:
    """Hent system-credentials fra credentials-servicen (sync). Caches i 60 sekunder."""
    global _system_cache, _system_cache_ts
    if _system_cache and time.monotonic() - _system_cache_ts < _CACHE_TTL:
        return _system_cache
    url = f"{CREDENTIALS_SERVICE_URL}/internal/credentials/{SYSTEM_USER_ID}"
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(url)
        if resp.status_code == 200:
            _system_cache = resp.json()
            _system_cache_ts = time.monotonic()
            logger.info("Loaded %d system credentials", len(_system_cache))
        else:
            logger.error("Credentials service returned %d for system user", resp.status_code)
    except httpx.RequestError as e:
        logger.error("Could not reach credentials service: %s", e)
    return _system_cache


def _extract_sub(token: str) -> str | None:
    """Decode the 'sub' claim from a JWT without verifying the signature.
    Verification already happened at Agentgateway / Keycloak level."""
    try:
        import base64, json
        payload_b64 = token.split(".")[1]
        # Add padding if needed
        padding = 4 - len(payload_b64) % 4
        payload_b64 += "=" * (padding % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        return payload.get("sub")
    except Exception as e:
        logger.warning("Could not decode JWT sub: %s", e)
        return None


async def get_user_env(jwt_token: str, x_user_sub: str = "") -> dict[str, str]:
    """
    Fetch all credentials for the user identified by jwt_token or x_user_sub.
    Returns a dict of { KEY_NAME: decrypted_value }.

    x_user_sub: value of the X-User-Sub header injected by agentgateway (preferred)
    jwt_token: raw JWT from Authorization header (fallback)
    """
    sub = x_user_sub or _extract_sub(jwt_token)
    print(f"[CREDS DEBUG] x_user_sub={x_user_sub!r:.30} jwt_len={len(jwt_token)} sub={sub!r}", flush=True)
    if not sub:
        logger.error("Could not extract sub – no X-User-Sub header and JWT decode failed. token_preview=%r", jwt_token[:40] if jwt_token else "(empty)")
        return {}

    url = f"{CREDENTIALS_SERVICE_URL}/internal/credentials/{sub}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)

        if resp.status_code == 200:
            creds = resp.json()
            logger.info("Loaded %d credentials for user %s", len(creds), sub[:8] + "…")
            return creds
        else:
            logger.error("Credentials service returned %d for user %s", resp.status_code, sub)
            return {}

    except httpx.RequestError as e:
        logger.error("Could not reach credentials service: %s", e)
        return {}


class UserSession:
    """
    Hjælpeklasse til at holde brugerens credentials i memory
    for sessionens varighed. Credentials hentes kun én gang.

    Eksempel:
        session = await UserSession.create(jwt_token)
        api_key = session.get("OPENAI_API_KEY")
        # eller brug session.env som et dict
    """

    def __init__(self, env: dict[str, str]):
        self.env = env

    @classmethod
    async def create(cls, jwt_token: str, x_user_sub: str = "") -> "UserSession":
        env = await get_user_env(jwt_token, x_user_sub)
        return cls(env)

    @classmethod
    async def from_headers(cls, headers: dict) -> "UserSession":
        """Create a UserSession from a request headers dict.
        Reads X-User-Sub (injected by agentgateway) with JWT parsing as fallback."""
        x_user_sub = headers.get("x-user-sub", "")
        # Ignore unevaluated agentgateway template strings (e.g. "%{jwt.sub}", "{{ jwt.sub }}")
        if x_user_sub.startswith("%{") or x_user_sub.startswith("{{"):
            print(f"[CREDS DEBUG] X-User-Sub contains unevaluated template: {x_user_sub!r} – check agentgateway config syntax", flush=True)
            x_user_sub = ""
        auth = headers.get("authorization", headers.get("Authorization", ""))
        token = auth.replace("Bearer ", "").replace("bearer ", "")
        return await cls.create(token, x_user_sub)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.env.get(key, default)

    def require(self, key: str) -> str:
        """Hent en nøgle – kaster ValueError hvis den ikke findes."""
        value = self.env.get(key)
        if value is None:
            raise ValueError(
                f"Credential '{key}' not found for this user. "
                f"Please add it at the credentials portal."
            )
        return value