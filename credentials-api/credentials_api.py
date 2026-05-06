"""
Credentials Service - Per-user encrypted credential storage
Runs as a sidecar alongside Agentgateway in docker-compose.
"""

import os
import sqlite3
import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

ENCRYPTION_KEY_RAW = os.environ["CREDENTIALS_ENCRYPTION_KEY"]
KEYCLOAK_URL       = os.environ["KEYCLOAK_URL"]          # e.g. http://keycloak:8080
KEYCLOAK_REALM     = os.environ["KEYCLOAK_REALM"]        # e.g. myrealm
DB_PATH            = os.environ.get("DB_PATH", "/data/credentials.db")
BASE_DIR           = os.path.dirname(os.path.abspath(__file__))

# Derive a 32-byte AES key from the raw string
def _derive_key(raw: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"agentgateway-credentials-v1",
        iterations=100_000,
    )
    return kdf.derive(raw.encode())

AES_KEY = _derive_key(ENCRYPTION_KEY_RAW)

# ── Encryption helpers ────────────────────────────────────────────────────────

def encrypt(plaintext: str) -> str:
    aesgcm = AESGCM(AES_KEY)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()

def decrypt(ciphertext: str) -> str:
    aesgcm = AESGCM(AES_KEY)
    raw = base64.b64decode(ciphertext)
    nonce, ct = raw[:12], raw[12:]
    return aesgcm.decrypt(nonce, ct, None).decode()

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id    TEXT NOT NULL,
                key_name   TEXT NOT NULL,
                key_value  TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, key_name)
            )
        """)
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)

# ── JWT / Keycloak ────────────────────────────────────────────────────────────

security = HTTPBearer()

async def verify_jwt(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Validate token against Keycloak's userinfo endpoint and return claims."""
    token = credentials.credentials
    userinfo_url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/userinfo"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {token}"}
            )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return resp.json()
    except httpx.RequestError as e:
        logger.error("Keycloak unreachable: %s", e)
        raise HTTPException(status_code=503, detail="Auth service unavailable")

# ── Models ────────────────────────────────────────────────────────────────────

class CredentialIn(BaseModel):
    key_name: str
    key_value: str

class CredentialOut(BaseModel):
    key_name: str
    created_at: str

# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Credentials Service", lifespan=lifespan)

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/me/credentials", response_model=list[CredentialOut])
async def list_credentials(claims: dict = Depends(verify_jwt)):
    """List credential key names for the current user (values never returned)."""
    user_id = claims["sub"]
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key_name, created_at FROM user_credentials WHERE user_id = ?",
            (user_id,)
        ).fetchall()
    return [{"key_name": r["key_name"], "created_at": r["created_at"]} for r in rows]


@app.post("/me/credentials", status_code=201)
async def upsert_credential(body: CredentialIn, claims: dict = Depends(verify_jwt)):
    """Create or update a credential for the current user."""
    user_id = claims["sub"]
    encrypted = encrypt(body.key_value)
    with get_db() as conn:
        conn.execute("""
            INSERT INTO user_credentials (user_id, key_name, key_value)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, key_name) DO UPDATE SET
                key_value = excluded.key_value,
                created_at = CURRENT_TIMESTAMP
        """, (user_id, body.key_name, encrypted))
        conn.commit()
    return {"status": "ok", "key_name": body.key_name}


@app.delete("/me/credentials/{key_name}", status_code=200)
async def delete_credential(key_name: str, claims: dict = Depends(verify_jwt)):
    """Delete a credential for the current user."""
    user_id = claims["sub"]
    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM user_credentials WHERE user_id = ? AND key_name = ?",
            (user_id, key_name)
        )
        conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"status": "ok"}


@app.get("/internal/credentials/{user_id}", include_in_schema=False)
async def get_credentials_internal(user_id: str, request: Request):
    """
    Internal endpoint for MCP servers to fetch decrypted credentials.
    Only accessible from within the docker network (not exposed externally).
    """
    # Basic protection: only allow from internal network
    client_host = request.client.host
    if not (client_host.startswith("172.") or client_host.startswith("10.") or client_host == "127.0.0.1"):
        raise HTTPException(status_code=403, detail="Internal endpoint only")

    with get_db() as conn:
        rows = conn.execute(
            "SELECT key_name, key_value FROM user_credentials WHERE user_id = ?",
            (user_id,)
        ).fetchall()

    return {r["key_name"]: decrypt(r["key_value"]) for r in rows}


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def ui():
    with open(os.path.join(BASE_DIR, "templates", "index.html")) as f:
        return f.read()
    

# ── OIDC Callback & Config ────────────────────────────────────────────────────
 
KEYCLOAK_PUBLIC_URL = os.environ.get("KEYCLOAK_PUBLIC_URL", KEYCLOAK_URL)
UI_CLIENT_ID        = os.environ.get("UI_CLIENT_ID", "credentials-ui")
UI_REDIRECT_URI     = os.environ.get("UI_REDIRECT_URI", "https://auth.petermikkelsen.dk/callback")
 
 
@app.get("/config")
async def get_config():
    """Expose non-secret config to the UI so it can build the OIDC login URL."""
    return {
        "keycloak_url":   KEYCLOAK_PUBLIC_URL,
        "keycloak_realm": KEYCLOAK_REALM,
        "client_id":      UI_CLIENT_ID,
        "redirect_uri":   UI_REDIRECT_URI,
    }
 
 
@app.get("/callback")
async def oidc_callback(code: str, request: Request):
    """
    Keycloak redirects here after login with an authorization code.
    We exchange it for a token and redirect the browser to / with the token.
    """
    redirect_uri = str(request.base_url) + "callback"
    token_url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
 
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(token_url, data={
                "grant_type":   "authorization_code",
                "client_id":    UI_CLIENT_ID,
                "code":         code,
                "redirect_uri": redirect_uri,
            })
 
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Token exchange failed")
 
        access_token = resp.json()["access_token"]
        # Redirect til UI med tokenet som query param – JS gemmer det i sessionStorage
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/?token={access_token}")
 
    except httpx.RequestError as e:
        logger.error("Token exchange failed: %s", e)
        raise HTTPException(status_code=503, detail="Auth service unavailable")