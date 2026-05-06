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
import logging
from functools import lru_cache

import httpx

logger = logging.getLogger(__name__)

CREDENTIALS_SERVICE_URL = os.environ.get(
    "CREDENTIALS_SERVICE_URL",
    "http://credentials-service:8001"
)


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


async def get_user_env(jwt_token: str) -> dict[str, str]:
    """
    Fetch all credentials for the user identified by jwt_token.
    Returns a dict of { KEY_NAME: decrypted_value }.

    Kald dette én gang ved session-start og cache resultatet i sessionen.
    """
    sub = _extract_sub(jwt_token)
    if not sub:
        logger.error("Could not extract sub from token – returning empty credentials")
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
    async def create(cls, jwt_token: str) -> "UserSession":
        env = await get_user_env(jwt_token)
        return cls(env)

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