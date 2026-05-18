import os
import json
import httpx
from typing import Any, Dict, Optional, List
from pathlib import Path
import imaplib
import email
import email.message
from email.header import decode_header
import email.utils
from email.utils import parseaddr
import base64
import re
import time
from datetime import datetime

from .mcp_tools_queries import CREATE_FILE_MUTATION, GET_ACCOUNTS_QUERY, GET_DIMENSIONABLES_QUERY, GET_PROPERTIES_QUERY, GET_LEASES_QUERY, GET_TYPES_QUERY, GET_INPUT_TYPE_QUERY, GET_TYPE_DETAILS_QUERY

import portalocker

from dotenv import load_dotenv
load_dotenv()


# --------------------
# Config
# --------------------

BASE_URL = os.environ.get("BASE_URL")
COMPANY = os.environ.get("COMPANY")
INTEGRATION = os.environ.get("INTEGRATION")
API_KEY = os.environ.get("API_KEY")

IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", 993))
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")

N8N_BASE_URL = os.getenv("N8N_BASE_URL", "http://localhost:5678")
N8N_API_KEY = os.getenv("N8N_API_KEY")
TEMPLATE_PREFIX = "[TEMPLATE]"

CACHE_FILE = Path(__file__).parent / "query_cache.json"
CACHE_LOCK_FILE = Path(__file__).parent / "query_cache.lock"


# --------------------
# JsonQueryCache — implements QueryCache protocol
# --------------------

class JsonQueryCache:
    """File-backed JSON cache for GraphQL queries."""

    def load(self) -> Dict[str, Any]:
        """Load query cache from JSON file with file locking."""
        if CACHE_FILE.exists():
            with portalocker.Lock(str(CACHE_LOCK_FILE), "a", timeout=5):
                return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return {"version": 1, "queries": []}

    def save(self, cache: Dict[str, Any]) -> None:
        """Save query cache to JSON file with file locking."""
        with portalocker.Lock(str(CACHE_LOCK_FILE), "a", timeout=5):
            CACHE_FILE.write_text(
                json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
            )


# Module-level default instance — callers can inject a different one for tests
_default_cache = JsonQueryCache()


def load_cache() -> Dict[str, Any]:
    return _default_cache.load()


def save_cache(cache: Dict[str, Any]) -> None:
    _default_cache.save(cache)


# --------------------
# Query Cache Helpers
# --------------------

def _extract_operation_name(query: str) -> Optional[str]:
    match = re.search(r'(?:query|mutation)\s+(\w+)', query)
    return match.group(1) if match else None


def _query_hash(query: str) -> str:
    import hashlib
    normalized = re.sub(r'\s+', ' ', query.strip())
    return hashlib.md5(normalized.encode()).hexdigest()[:10]


def _is_query_cached(query: str, cache: Dict[str, Any]) -> bool:
    normalized = re.sub(r'\s+', ' ', query.strip())
    for q in cache["queries"]:
        if re.sub(r'\s+', ' ', q["query"].strip()) == normalized:
            return True
    return False


def auto_cache_query(query: str, variables: Optional[Dict[str, Any]], result: Dict[str, Any]) -> None:
    """Automatically cache a successful query if it's not already cached."""
    if "errors" in result or "error" in result:
        return
    if not result.get("data"):
        return

    cache = load_cache()
    if _is_query_cached(query, cache):
        return

    op_name = _extract_operation_name(query)
    query_id = op_name or f"auto-{_query_hash(query)}"

    data_keys = list(result.get("data", {}).keys())
    keywords = [k for k in data_keys if not k.startswith("__")]
    if op_name:
        keywords.append(op_name)

    if op_name:
        for i, q in enumerate(cache["queries"]):
            existing_op = _extract_operation_name(q["query"])
            if existing_op == op_name:
                old_use_count = q.get("use_count", 0)
                cache["queries"][i] = {
                    "id": query_id,
                    "description": f"Auto-cached: {op_name or ', '.join(data_keys)}",
                    "intent_keywords": keywords,
                    "query": query,
                    "variables": variables,
                    "created_at": q.get("created_at", datetime.now().strftime("%Y-%m-%d")),
                    "last_used": datetime.now().strftime("%Y-%m-%d"),
                    "use_count": old_use_count + 1,
                }
                save_cache(cache)
                return

    cache["queries"].append({
        "id": query_id,
        "description": f"Auto-cached: {op_name or ', '.join(data_keys)}",
        "intent_keywords": keywords,
        "query": query,
        "variables": variables,
        "created_at": datetime.now().strftime("%Y-%m-%d"),
        "last_used": datetime.now().strftime("%Y-%m-%d"),
        "use_count": 1,
    })
    save_cache(cache)


# --------------------
# n8n Helper Functions
# --------------------

async def call_n8n(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call n8n REST API."""
    if not N8N_API_KEY:
        return {"error": "Missing N8N_API_KEY environment variable"}
    if not N8N_BASE_URL:
        return {"error": "Missing N8N_BASE_URL environment variable"}

    url = f"{N8N_BASE_URL}/api/v1{path}"
    headers = {"X-N8N-API-KEY": N8N_API_KEY}

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        if method == "GET":
            response = await client.get(url)
        elif method == "POST":
            response = await client.post(url, json=body or {})
        elif method == "PUT":
            response = await client.put(url, json=body)
        elif method == "DELETE":
            response = await client.delete(url)
        else:
            return {"error": f"Unsupported method: {method}"}

        if response.status_code >= 400:
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text
            return {"error": f"n8n API error {response.status_code}", "details": error_body}
        if response.status_code == 204:
            return {"success": True}
        return response.json()


def _is_template(workflow: Dict[str, Any]) -> bool:
    return workflow.get("name", "").startswith(TEMPLATE_PREFIX)


# --------------------
# HttpBoligflowClient — implements BoligflowClient protocol
# --------------------

class HttpBoligflowClient:
    """Concrete Boligflow GraphQL client using httpx."""

    def __init__(self):
        self._headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Company": COMPANY,
            "X-Boligflow-Integration": INTEGRATION,
            "Content-Type": "application/json",
        }

    async def query(
        self, query: str, variables: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if not BASE_URL:
            return {"error": "Missing BASE_URL environment variable"}
        if not API_KEY:
            return {"error": "Missing API_KEY environment variable"}

        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            response = await client.post(BASE_URL, json=payload)
            response.raise_for_status()
            return response.json()

    async def upload_file(
        self,
        record_id: str,
        file_data: Dict[str, Any],
        fileable_type: str = "Case",
    ) -> Dict[str, Any]:
        """
        Upload a file to a Boligflow record.

        file_data keys:
            filename      (str)  - display name
            content_type  (str)  - MIME type
            content       (bytes | None) - raw bytes, OR
            data          (str | None)   - base64-encoded bytes (attachments)
            eml_content   (str | None)   - raw eml string (email uploads)
        """
        if not API_KEY:
            return {"error": "Missing API_KEY environment variable"}
        if not BASE_URL:
            return {"error": "Missing BASE_URL environment variable"}

        operations = {
            "query": CREATE_FILE_MUTATION,
            "variables": {
                "input": {
                    "file": None,
                    "type": "CUSTOM",
                    "fileable": {
                        "connect": {"type": fileable_type, "id": record_id}
                    },
                }
            },
        }
        map_data = {"0": ["variables.input.file"]}

        # Resolve raw bytes
        if "content" in file_data and file_data["content"] is not None:
            raw = file_data["content"]
        elif "eml_content" in file_data:
            raw = file_data["eml_content"].encode("utf-8")
        else:
            raw = base64.b64decode(file_data["data"])

        upload_headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Company": COMPANY,
            "X-Boligflow-Integration": INTEGRATION,
        }

        files = {
            "operations": (None, json.dumps(operations), "application/json"),
            "map": (None, json.dumps(map_data), "application/json"),
            "0": (file_data["filename"], raw, file_data["content_type"]),
        }

        async with httpx.AsyncClient(headers=upload_headers, timeout=30) as client:
            response = await client.post(BASE_URL, files=files)
            response_text = response.text
            response.raise_for_status()
            result = response.json()

        if "errors" in result:
            raise Exception(f"GraphQL errors: {json.dumps(result['errors'], indent=2)}")

        if "data" in result and result["data"].get("createFile") is None:
            raise Exception(f"createFile returned null — upload failed. Response: {response_text}")

        return result


# Module-level default instance
_default_boligflow = HttpBoligflowClient()


# Thin wrappers kept for backwards compatibility with existing callers
async def call_boligflow(query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return await _default_boligflow.query(query, variables)


async def upload_email_to_boligflow(
    record_id: str,
    email_data: Dict[str, Any],
    fileable_type: str = "Case",
) -> Dict[str, Any]:
    eml_content = create_eml_file(email_data)
    file_data = {
        "filename": email_data.get("eml_filename", "email.eml"),
        "content_type": "message/rfc822",
        "eml_content": eml_content,
    }
    return await _default_boligflow.upload_file(record_id, file_data, fileable_type)


async def upload_attachment_to_boligflow(
    record_id: str,
    attachment: Dict[str, Any],
    fileable_type: str = "Case",
) -> Dict[str, Any]:
    file_data = {
        "filename": attachment["filename"],
        "content_type": attachment["content_type"],
        "data": attachment["data"],  # base64
    }
    return await _default_boligflow.upload_file(record_id, file_data, fileable_type)


# --------------------
# Email helpers
# --------------------

def create_eml_file(email_data: Dict[str, Any]) -> str:
    """Create .eml string from email data dict."""
    boundary = f"_boundary_mcp_{int(time.time() * 1000)}"

    headers = [
        f"From: {email_data.get('from_email', 'unknown@example.com')}",
        f"To: {email_data.get('to_email', '')}",
        f"Subject: {email_data.get('subject', 'No Subject')}",
        f"Date: {email_data.get('date', '')}",
    ]
    if email_data.get("message_id"):
        headers.append(f"Message-ID: {email_data['message_id']}")
    headers += [
        "MIME-Version: 1.0",
        f'Content-Type: multipart/alternative; boundary="{boundary}"',
    ]

    body = ""
    if email_data.get("body"):
        body += f"--{boundary}\r\n"
        body += "Content-Type: text/plain; charset=\"utf-8\"\r\n"
        body += "Content-Transfer-Encoding: 8bit\r\n\r\n"
        body += email_data["body"] + "\r\n"
    if email_data.get("html_body"):
        body += f"--{boundary}\r\n"
        body += "Content-Type: text/html; charset=\"utf-8\"\r\n"
        body += "Content-Transfer-Encoding: 8bit\r\n\r\n"
        body += email_data["html_body"] + "\r\n"
    body += f"--{boundary}--\r\n"

    return "\r\n".join(headers) + "\r\n\r\n" + body


def _parse_email_message(msg: email.message.Message, raw_email: bytes) -> dict:
    """
    Parse a raw email.message.Message into a standardised dict.

    This is the single source of truth for email parsing — used by both
    get_unread_emails() and get_emails_by_sender() so the logic lives in
    exactly one place.
    """
    # Subject
    subject = ""
    subject_header = msg.get("Subject", "")
    if subject_header:
        for content, encoding in decode_header(subject_header):
            if isinstance(content, bytes):
                subject += content.decode(encoding or "utf-8", errors="ignore")
            else:
                subject += content

    # Addresses
    from_name, from_email_addr = parseaddr(msg.get("From", ""))
    to_name, to_email_addr = parseaddr(msg.get("To", ""))

    # Body + attachments
    body = ""
    html_body = ""
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        body = payload.decode(errors="ignore")
                except Exception:
                    pass
            elif content_type == "text/html" and "attachment" not in content_disposition:
                try:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        html_body = payload.decode(errors="ignore")
                except Exception:
                    pass
            elif "attachment" in content_disposition:
                filename = part.get_filename()
                if filename:
                    raw_attachment = part.get_payload(decode=True)
                    if isinstance(raw_attachment, bytes):
                        attachments.append({
                            "filename": filename,
                            "content_type": content_type,
                            "data": base64.b64encode(raw_attachment).decode(),
                        })
    else:
        try:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                if msg.get_content_type() == "text/html":
                    html_body = payload.decode(errors="ignore")
                else:
                    body = payload.decode(errors="ignore")
        except Exception:
            body = str(msg.get_payload())

    # Safe filename
    safe_subject = re.sub(r'[^a-z0-9\s-]', '', subject, flags=re.IGNORECASE)
    safe_subject = re.sub(r'\s+', '_', safe_subject)[:50]

    date_str = msg.get("Date", "")
    try:
        date_str = email.utils.parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
    except Exception:
        date_str = datetime.now().strftime("%Y-%m-%d")

    eml_filename = (
        f"{date_str}_{safe_subject}.eml" if safe_subject else f"{date_str}_email.eml"
    )

    return {
        "message_id": msg.get("Message-ID", ""),
        "subject": subject,
        "from_name": from_name,
        "from_email": from_email_addr,
        "to_name": to_name,
        "to_email": to_email_addr,
        "date": msg.get("Date", ""),
        "body": body,
        "html_body": html_body,
        "attachments": attachments,
        "raw_email": base64.b64encode(raw_email).decode(),
        "eml_filename": eml_filename,
    }


# --------------------
# ImapEmailProvider — implements EmailProvider protocol
# --------------------

class ImapEmailProvider:
    """
    Concrete EmailProvider backed by IMAP.

    Pass custom credentials for testing:
        provider = ImapEmailProvider(server="...", user="...", password="...")
    Otherwise falls back to environment variables.
    """

    def __init__(
        self,
        server: str = IMAP_SERVER,
        port: int = IMAP_PORT,
        user: str = EMAIL_USER,
        password: str = EMAIL_PASSWORD,
    ):
        self.server = server
        self.port = port
        self.user = user
        self.password = password

    def _connect(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(self.server, self.port)
        imap.login(self.user, self.password)
        imap.select("INBOX")
        return imap

    def get_emails(
        self,
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
        unread_only: bool = True,
        max_emails: int = 20,
        search_limit: int = 100,
    ) -> tuple[List[Dict[str, Any]], int]:
        """
        Unified email fetcher — replaces get_unread_emails and get_emails_by_sender.

        Args:
            sender_filter:  Substring match on From address (None = no filter)
            subject_filter: Substring match on Subject (None = no filter)
            unread_only:    True → UNSEEN only; False → ALL
            max_emails:     Max emails to return after filtering
            search_limit:   Max emails to scan before filtering

        Returns:
            (list of email dicts, number scanned)
        """
        try:
            imap = self._connect()
            criteria = "UNSEEN" if unread_only else "ALL"
            status, messages = imap.search(None, criteria)

            if status != "OK":
                imap.logout()
                return [], 0

            email_ids = list(reversed(messages[0].split()))
            results = []
            scanned = 0

            for email_id in email_ids:
                if scanned >= search_limit or len(results) >= max_emails:
                    break

                scanned += 1
                status, msg_data = imap.fetch(email_id, "(BODY.PEEK[])")
                if status != "OK" or not msg_data:
                    continue

                raw_data = msg_data[0]
                if not isinstance(raw_data, tuple):
                    continue
                raw_email = raw_data[1]
                msg = email.message_from_bytes(raw_email)

                parsed = _parse_email_message(msg, raw_email)
                parsed["id"] = email_id.decode()

                if sender_filter and sender_filter.lower() not in parsed["from_email"].lower():
                    continue
                if subject_filter and subject_filter.lower() not in parsed["subject"].lower():
                    continue

                results.append(parsed)

            imap.logout()
            return results, scanned

        except Exception as e:
            raise Exception(f"Error fetching emails: {str(e)}")

    def mark_as_read(self, email_id: str) -> None:
        try:
            imap = self._connect()
            status, response = imap.store(email_id, "+FLAGS", "\\Seen")
            imap.logout()
            if status != "OK":
                raise Exception(f"Failed to mark email as read. Status: {status}, Response: {response}")
        except Exception as e:
            raise Exception(f"Error marking email as read: {str(e)}")


# Module-level default instance
_default_email_provider = ImapEmailProvider()


# Backwards-compatible wrappers so existing callers keep working unchanged
def get_unread_emails(
    subject_filter: Optional[str] = None,
    max_emails: int = 10,
    search_limit: int = 100,
) -> tuple[List[Dict[str, Any]], int]:
    return _default_email_provider.get_emails(
        subject_filter=subject_filter,
        unread_only=True,
        max_emails=max_emails,
        search_limit=search_limit,
    )


def get_emails_by_sender(
    sender_filter: Optional[str] = None,
    subject_filter: Optional[str] = None,
    max_emails: int = 20,
    search_limit: int = 200,
) -> tuple[List[Dict[str, Any]], int]:
    return _default_email_provider.get_emails(
        sender_filter=sender_filter,
        subject_filter=subject_filter,
        unread_only=False,
        max_emails=max_emails,
        search_limit=search_limit,
    )


def mark_email_as_read(email_id: str) -> None:
    _default_email_provider.mark_as_read(email_id)


# --------------------
# Financial Journal Helpers
# --------------------

async def get_accounts(code: int) -> str:
    variables = {"code": code}
    result = await call_boligflow(GET_ACCOUNTS_QUERY, variables)
    return result.get("accounts", [])


async def get_dimensionables(dimension_type: str):
    variables = {"type": dimension_type}
    result = await call_boligflow(GET_DIMENSIONABLES_QUERY, variables)
    return result.get("dimensionables", [])


async def get_properties(filter: dict) -> str:
    variables = {"filter": filter} if filter else {}
    result = await call_boligflow(GET_PROPERTIES_QUERY, variables)
    return result.get("properties", [])


async def get_leases(filter: dict) -> str:
    variables = {"filter": filter} if filter else {}
    result = await call_boligflow(GET_LEASES_QUERY, variables)
    return result.get("leases", [])


# --------------------
# Debug Helpers
# --------------------

async def verify_record(record_id: str, record_type: str = "Lease") -> str:
    query = f"""
    query {{
        {record_type.lower()}(id: "{record_id}") {{
            id
            __typename
        }}
    }}
    """
    try:
        result = await call_boligflow(query)

        if "errors" in result:
            return json.dumps({"exists": False, "error": "GraphQL errors", "details": result["errors"]}, indent=2, ensure_ascii=False)

        if "data" in result:
            record_data = result["data"].get(record_type.lower())
            if record_data is None:
                return json.dumps({"exists": False, "message": f"{record_type} with ID {record_id} not found"}, indent=2, ensure_ascii=False)
            return json.dumps({
                "exists": True,
                "record_type": record_data.get("__typename"),
                "record_id": record_data.get("id"),
                "message": f"{record_type} exists and is accessible",
            }, indent=2, ensure_ascii=False)

        return json.dumps({"exists": False, "message": "Unexpected response format", "response": result}, indent=2, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"exists": False, "error": str(e)}, indent=2, ensure_ascii=False)


def debug_env_vars() -> str:
    return json.dumps({
        "IMAP_SERVER": os.getenv("IMAP_SERVER"),
        "IMAP_PORT": os.getenv("IMAP_PORT"),
        "EMAIL_USER": os.getenv("EMAIL_USER"),
        "EMAIL_PASSWORD": "***" if os.getenv("EMAIL_PASSWORD") else None,
    }, indent=2)