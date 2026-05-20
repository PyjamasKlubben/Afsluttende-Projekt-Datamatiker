import os
import json
import httpx
from typing import Any, Dict, Optional, List
from pathlib import Path
import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr
import email.utils
import base64
import re
import time
from datetime import datetime

from .mcp_tools_queries import CREATE_FILE_MUTATION, GET_ACCOUNTS_QUERY, GET_DIMENSIONABLES_QUERY, GET_PROPERTIES_QUERY, GET_LEASES_QUERY, GET_TYPES_QUERY, GET_INPUT_TYPE_QUERY, GET_TYPE_DETAILS_QUERY
from .interfaces import BoligflowClient, EmailProvider, QueryCache

import portalocker

from dotenv import load_dotenv
load_dotenv()

# #TODO interface til helpers og queries

# --------------------
# Config
# --------------------

BASE_URL = os.environ.get("BASE_URL") 
COMPANY = os.environ.get("COMPANY")
INTEGRATION = os.environ.get("INTEGRATION")

# Email Config
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", 993))

# n8n Config
N8N_BASE_URL = os.getenv("N8N_BASE_URL", "http://localhost:5678")
TEMPLATE_PREFIX = "[TEMPLATE]"

# Query Cache
CACHE_FILE = Path(__file__).parent / "query_cache.json"



# --------------------
# Query Cache Helpers
# --------------------

CACHE_LOCK_FILE = Path(__file__).parent / "query_cache.lock"


def load_cache() -> Dict[str, Any]:
    """Load query cache from JSON file with file locking."""
    if CACHE_FILE.exists():
        with portalocker.Lock(str(CACHE_LOCK_FILE), "a", timeout=5) as _:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {"version": 1, "queries": []}


def save_cache(cache: Dict[str, Any]) -> None:
    """Save query cache to JSON file with file locking."""
    with portalocker.Lock(str(CACHE_LOCK_FILE), "a", timeout=5) as _:
        CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _extract_operation_name(query: str) -> Optional[str]:
    """Extract operation name from a GraphQL query string."""
    match = re.search(r'(?:query|mutation)\s+(\w+)', query)
    return match.group(1) if match else None


def _query_hash(query: str) -> str:
    """Generate a short deterministic ID from a query string."""
    import hashlib
    normalized = re.sub(r'\s+', ' ', query.strip())
    return hashlib.md5(normalized.encode()).hexdigest()[:10]


def _is_query_cached(query: str, cache: Dict[str, Any]) -> bool:
    """Check if a query (by content) is already in the cache."""
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

    # Extract top-level field names from result as keywords
    data_keys = list(result.get("data", {}).keys())
    keywords = [k for k in data_keys if not k.startswith("__")]
    if op_name:
        keywords.append(op_name)

    # Check if a query with the same operation name already exists -> replace it
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
                    "use_count": old_use_count + 1
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
        "use_count": 1
    })
    save_cache(cache)


# --------------------
# n8n Helper Functions
# --------------------

async def call_n8n(method: str, path: str, body: Optional[Dict[str, Any]] = None, user_credentials: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call n8n REST API."""
    n8n_api_key = (user_credentials or {}).get("N8N_API_KEY")
    if not n8n_api_key:
        return {"error": "Missing N8N_API_KEY in credentials service"}

    url = f"{N8N_BASE_URL}/api/v1{path}"
    headers = {"X-N8N-API-KEY": n8n_api_key}

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
    """Check if a workflow is a protected template."""
    return workflow.get("name", "").startswith(TEMPLATE_PREFIX)

# #TODO - undersøg hvad forskellen er på denne og call_graphql
#call_graphql kalder denne som helper - derfor begge er nødvendige
#logikken til caching flyttes ind i helpers og kalder andre metoder gennem denne
async def call_boligflow(query: str, variables: Optional[Dict[str, Any]] = None, user_credentials: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call Boligflow GraphQL API"""
    if not BASE_URL:
        return {"error": "Missing BASE_URL environment variable"}

    api_key = (user_credentials or {}).get("BOLIGFLOW_API_KEY")
    if not api_key:
        return {"error": "Missing BOLIGFLOW_API_KEY in credentials service"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Company": COMPANY,
        "X-Boligflow-Integration": INTEGRATION,
        "Content-Type": "application/json",
    }

    payload: Dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        response = await client.post(BASE_URL, json=payload) 
        return response.json()


# --------------------
# HttpBoligflowClient — implements BoligflowClient protocol
# --------------------

#TODO - fungerer ikke som den er nu

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
        if not API_KEY:
            return {"error": "Missing API_KEY environment variable"}
        if not BASE_URL:
            return {"error": "Missing BASE_URL environment variable"}

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



# --------------------
# Email Helper Functions
# --------------------

def create_eml_file(email_data: Dict[str, Any]) -> str:
    """
    Create .eml file from email data (matches n8n JS code logic)
    """
    boundary = f"_boundary_mcp_{int(time.time() * 1000)}"
    
    # Build RFC822 headers
    eml_headers = [
        f"From: {email_data.get('from_email', 'unknown@example.com')}",
        f"To: {email_data.get('to_email', '')}",
        f"Subject: {email_data.get('subject', 'No Subject')}",
        f"Date: {email_data.get('date', '')}"
    ]
    
    if email_data.get('message_id'):
        eml_headers.append(f"Message-ID: {email_data['message_id']}")
    
    eml_headers.append("MIME-Version: 1.0")
    eml_headers.append(f'Content-Type: multipart/alternative; boundary="{boundary}"')
    
    # Build multipart body
    eml_body = ""
    
    # Text plain part
    text_content = email_data.get('body', '')
    if text_content:
        eml_body += f"--{boundary}\r\n"
        eml_body += "Content-Type: text/plain; charset=\"utf-8\"\r\n"
        eml_body += "Content-Transfer-Encoding: 8bit\r\n\r\n"
        eml_body += text_content + "\r\n"
    
    # HTML part (if available)
    html_content = email_data.get('html_body', '')
    if html_content:
        eml_body += f"--{boundary}\r\n"
        eml_body += "Content-Type: text/html; charset=\"utf-8\"\r\n"
        eml_body += "Content-Transfer-Encoding: 8bit\r\n\r\n"
        eml_body += html_content + "\r\n"
    
    eml_body += f"--{boundary}--\r\n"
    
    # Build complete .eml
    eml_content = "\r\n".join(eml_headers) + "\r\n\r\n" + eml_body
    
    return eml_content


def get_unread_emails(email_user: str, email_password: str, subject_filter: Optional[str] = None, max_emails: int = 10, search_limit: int = 100) -> tuple[List[Dict[str, Any]], int]:
    """
    Fetch unread emails from IMAP inbox

    Args:
        email_user: Email address to log in with
        email_password: Email app password
        subject_filter: Text that should be in subject (None = all unread)
        max_emails: Maximum number of emails to return (after filtering)
        search_limit: Maximum number of unread emails to search through (before filtering)

    Returns:
        Tuple of (list of email objects, number of emails searched)
    """
    try:
        if not email_user or not email_password:
            raise Exception("Missing email credentials")

        # Connect to IMAP
        imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        imap.login(email_user, email_password)
        imap.select("INBOX")
        
        # Search for unread emails
        status, messages = imap.search(None, "UNSEEN")
        
        if status != "OK":
            imap.logout()
            return [], 0
        
        email_ids = messages[0].split()
        
        # Reverser listen så vi får de NYESTE emails først
        email_ids = list(reversed(email_ids))
        
        emails = []
        emails_checked = 0
        
        # Søg gennem op til search_limit emails eller indtil vi har max_emails matches
        for email_id in email_ids:
            if emails_checked >= search_limit:
                break
            if len(emails) >= max_emails:
                break
                
            emails_checked += 1
            status, msg_data = imap.fetch(email_id, "(BODY.PEEK[])")
            
            if status != "OK":
                continue
            
            # Parse email
            raw_data = msg_data[0]
            if not isinstance(raw_data, tuple):
                continue
            raw_email = raw_data[1]
            msg = email.message_from_bytes(raw_email)
            
            # Decode subject
            subject = ""
            subject_header = msg.get("Subject", "")
            if subject_header:
                decoded = decode_header(subject_header)
                for content, encoding in decoded:
                    if isinstance(content, bytes):
                        subject += content.decode(encoding or "utf-8", errors="ignore")
                    else:
                        subject += content
            
            # Check subject filter
            if subject_filter and subject_filter.lower() not in subject.lower():
                continue
            
            # Extract email metadata
            from_name, from_email = parseaddr(msg.get("From", ""))
            to_name, to_email = parseaddr(msg.get("To", ""))
            
            # Extract body and attachments
            body = ""
            html_body = ""
            attachments = []
            
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition", ""))
                    
                    # Text plain body
                    if content_type == "text/plain" and "attachment" not in content_disposition:
                        try:
                            p = part.get_payload(decode=True)
                            if isinstance(p, bytes):
                                body = p.decode(errors="ignore")
                        except:
                            pass

                    # HTML body
                    elif content_type == "text/html" and "attachment" not in content_disposition:
                        try:
                            p = part.get_payload(decode=True)
                            if isinstance(p, bytes):
                                html_body = p.decode(errors="ignore")
                        except:
                            pass

                    # Attachments
                    elif "attachment" in content_disposition:
                        filename = part.get_filename()
                        if filename:
                            raw_attachment = part.get_payload(decode=True)
                            if isinstance(raw_attachment, bytes):
                                attachments.append({
                                    "filename": filename,
                                    "content_type": content_type,
                                    "data": base64.b64encode(raw_attachment).decode()
                                })
            else:
                try:
                    content_type = msg.get_content_type()
                    payload = msg.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        if content_type == "text/html":
                            html_body = payload.decode(errors="ignore")
                        else:
                            body = payload.decode(errors="ignore")
                except:
                    body = str(msg.get_payload())
            
            # Generate filename (matches n8n logic)
            safe_subject = re.sub(r'[^a-z0-9\s-]', '', subject, flags=re.IGNORECASE)
            safe_subject = re.sub(r'\s+', '_', safe_subject)[:50]
            
            date_str = msg.get('Date', '')
            try:
                date_obj = email.utils.parsedate_to_datetime(date_str)
                date_str = date_obj.strftime('%Y-%m-%d')
            except:
                date_str = datetime.now().strftime('%Y-%m-%d')
            
            eml_filename = f"{date_str}_{safe_subject}.eml" if safe_subject else f"{date_str}_email.eml"
            
            emails.append({
                "id": email_id.decode(),
                "message_id": msg.get("Message-ID", ""),
                "subject": subject,
                "from_name": from_name,
                "from_email": from_email,
                "to_name": to_name,
                "to_email": to_email,
                "date": msg.get("Date", ""),
                "body": body,
                "html_body": html_body,
                "attachments": attachments,
                "raw_email": base64.b64encode(raw_email).decode(),
                "eml_filename": eml_filename
            })
        
        imap.logout()
        return emails, emails_checked
        
    except Exception as e:
        raise Exception(f"Error fetching emails: {str(e)}")


def mark_email_as_read(email_id: str, email_user: str = "", email_password: str = ""):
    """Mark an email as read"""
    try:
        if not email_user or not email_password:
            raise Exception("Missing email credentials")

        imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        imap.login(email_user, email_password)
        imap.select("INBOX")
        
        # Marker som læst
        status, response = imap.store(email_id, '+FLAGS', '\\Seen')
        
        if status != 'OK':
            raise Exception(f"Failed to mark email as read. Status: {status}, Response: {response}")
        
        imap.logout()
        
    except Exception as e:
        raise Exception(f"Error marking email as read: {str(e)}")


async def upload_email_to_boligflow(
    record_id: str,
    email_data: Dict[str, Any],
    fileable_type: str = "Case",
    user_credentials: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Upload email to Boligflow using multipart/form-data (matches n8n implementation)

    Args:
        record_id: ID of record to attach email to
        email_data: Email data dictionary
        fileable_type: Type of record (Case, Project, etc.)
        user_credentials: User credentials dict from get_user_env()

    Returns:
        API response
    """
    if not BASE_URL:
        return {"error": "Missing BASE_URL environment variable"}

    api_key = (user_credentials or {}).get("BOLIGFLOW_API_KEY")
    if not api_key:
        return {"error": "Missing BOLIGFLOW_API_KEY in credentials service"}

    # Prepare operations object
    operations = {
        "query": CREATE_FILE_MUTATION,
        "variables": {
            "input": {
                "file": None,
                "type": "CUSTOM",
                "fileable": {
                    "connect": {
                        "type": fileable_type,
                        "id": record_id
                    }
                }
            }
        }
    }
    
    # Map for multipart spec
    map_data = {
        "0": ["variables.input.file"]
    }
    
    # Create .eml file from email data
    eml_content = create_eml_file(email_data)
    
    # Create multipart form data
    files = {
        'operations': (None, json.dumps(operations), 'application/json'),
        'map': (None, json.dumps(map_data), 'application/json'),
        '0': (
            email_data.get('eml_filename', 'email.eml'),
            eml_content.encode('utf-8'),
            'message/rfc822'  # Matcher n8n's contentType
        )
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Company": COMPANY,
        "X-Boligflow-Integration": INTEGRATION,
    }

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        response = await client.post(
            BASE_URL,
            files=files,
            timeout=30.0
        )
        
        # Log response for debugging
        response_text = response.text
        
        response.raise_for_status()
        result = response.json()
        
        # Check for GraphQL errors
        if "errors" in result:
            error_details = result['errors']
            raise Exception(f"GraphQL errors: {json.dumps(error_details, indent=2)}")
        
        # Check if createFile mutation actually succeeded
        if "data" in result and "createFile" in result["data"]:
            if result["data"]["createFile"] is None:
                raise Exception(f"createFile returned null - upload failed. Full response: {response_text}")
        
        return result


async def upload_attachment_to_boligflow(
    record_id: str,
    attachment: Dict[str, Any],
    fileable_type: str = "Case",
    user_credentials: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    if not BASE_URL:
        return {"error": "Missing BASE_URL environment variable"}

    api_key = (user_credentials or {}).get("BOLIGFLOW_API_KEY")
    if not api_key:
        return {"error": "Missing BOLIGFLOW_API_KEY in credentials service"}

    operations = {
        "query": CREATE_FILE_MUTATION,
        "variables": {
            "input": {
                "file": None,
                "type": "CUSTOM",
                "fileable": {
                    "connect": {
                        "type": fileable_type,
                        "id": record_id
                    }
                }
            }
        }
    }

    map_data = {"0": ["variables.input.file"]}

    files = {
        "operations": (None, json.dumps(operations), "application/json"),
        "map": (None, json.dumps(map_data), "application/json"),
        "0": (
            attachment["filename"],
            base64.b64decode(attachment["data"]),
            attachment["content_type"]
        )
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Company": COMPANY,
        "X-Boligflow-Integration": INTEGRATION,
    }

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        response = await client.post(BASE_URL, files=files)
        response.raise_for_status()
        return response.json()


def get_emails_by_sender(
    email_user: str,
    email_password: str,
    sender_filter: Optional[str] = None,
    subject_filter: Optional[str] = None,
    max_emails: int = 20,
    search_limit: int = 200
) -> tuple[List[Dict[str, Any]], int]:
    """
    Fetch all emails from IMAP inbox filtered by sender and/or subject (read + unread)

    Args:
        email_user: Email address to log in with
        email_password: Email app password
        sender_filter: Optional email address that should be from sender
        subject_filter: Text that should be in subject (None = all)
        max_emails: Maximum number of emails to return (after filtering)
        search_limit: Maximum number of emails to search through (before filtering)

    Returns:
        Tuple of (list of email objects, number of emails searched)
    """
    try:
        if not email_user or not email_password:
            raise Exception("Missing email credentials")
        # Connect to IMAP
        imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        imap.login(email_user, email_password)
        imap.select("INBOX")

        # Fetch ALL emails (read + unread)
        status, messages = imap.search(None, "ALL")

        if status != "OK":
            imap.logout()
            return [], 0

        email_ids = list(reversed(messages[0].split()))  # nyeste først

        emails = []
        emails_checked = 0

        for email_id in email_ids:
            if emails_checked >= search_limit:
                break
            if len(emails) >= max_emails:
                break

            emails_checked += 1
            status, msg_data = imap.fetch(email_id, "(BODY.PEEK[])")

            if status != "OK":
                continue

             # Parse email
            raw_data = msg_data[0]
            if not isinstance(raw_data, tuple):
                continue
            raw_email = raw_data[1]
            msg = email.message_from_bytes(raw_email)

            # Decode subject
            subject = ""
            subject_header = msg.get("Subject", "")
            if subject_header:
                decoded = decode_header(subject_header)
                for content, encoding in decoded:
                    if isinstance(content, bytes):
                        subject += content.decode(encoding or "utf-8", errors="ignore")
                    else:
                        subject += content

            # Sender filter
            from_name, from_email = parseaddr(msg.get("From", ""))
            if sender_filter and sender_filter.lower() not in from_email.lower():
                continue

            # Subject filter
            if subject_filter and subject_filter.lower() not in subject.lower():
                continue

            # Extract body, html, attachments (samme som din funktion)
            body = ""
            html_body = ""
            attachments = []

            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition", ""))

                    if content_type == "text/plain" and "attachment" not in content_disposition:
                        try:
                            p = part.get_payload(decode=True)
                            if isinstance(p, bytes):
                                body = p.decode(errors="ignore")
                        except:
                            pass

                    elif content_type == "text/html" and "attachment" not in content_disposition:
                        try:
                            p = part.get_payload(decode=True)
                            if isinstance(p, bytes):
                                html_body = p.decode(errors="ignore")
                        except:
                            pass

                    elif "attachment" in content_disposition:
                        filename = part.get_filename()
                        if filename:
                            raw_attachment = part.get_payload(decode=True)
                            if isinstance(raw_attachment, bytes):
                                attachments.append({
                                    "filename": filename,
                                    "content_type": content_type,
                                    "data": base64.b64encode(raw_attachment).decode()
                                })
            else:
                try:
                    payload = msg.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        if msg.get_content_type() == "text/html":
                            html_body = payload.decode(errors="ignore")
                        else:
                            body = payload.decode(errors="ignore")
                except:
                    body = str(msg.get_payload())

            # Generate filename
            safe_subject = re.sub(r'[^a-z0-9\s-]', '', subject, flags=re.IGNORECASE)
            safe_subject = re.sub(r'\s+', '_', safe_subject)[:50]

            date_str = msg.get('Date', '')
            try:
                date_obj = email.utils.parsedate_to_datetime(date_str)
                date_str = date_obj.strftime('%Y-%m-%d')
            except:
                date_str = datetime.now().strftime('%Y-%m-%d')

            eml_filename = f"{date_str}_{safe_subject}.eml" if safe_subject else f"{date_str}_email.eml"

            emails.append({
                "id": email_id.decode(),
                "message_id": msg.get("Message-ID", ""),
                "subject": subject,
                "from_name": from_name,
                "from_email": from_email,
                "to_name": parseaddr(msg.get("To", ""))[0],
                "to_email": parseaddr(msg.get("To", ""))[1],
                "date": msg.get("Date", ""),
                "body": body,
                "html_body": html_body,
                "attachments": attachments,
                "raw_email": base64.b64encode(raw_email).decode(),
                "eml_filename": eml_filename
            })

        imap.logout()
        return emails, emails_checked

    except Exception as e:
        raise Exception(f"Error fetching emails: {str(e)}")




# --------------------
# Financial Journal Helper Functions
# --------------------

async def get_accounts(user_credentials: Dict[str, Any], code: Optional[dict] = None) -> str:
    variables = {"code": code} if code else None
    result = await call_boligflow(GET_ACCOUNTS_QUERY, variables, user_credentials)
    return result.get("accounts", [])


async def get_dimensionables(dimension_type: str, user_credentials: Optional[Dict[str, Any]] = None):
    variables = {"type": dimension_type}
    result = await call_boligflow(GET_DIMENSIONABLES_QUERY, variables, user_credentials)
    return result.get("dimensionables", [])


async def get_properties(user_credentials: Dict[str, Any], filter: Optional[dict] = None) -> str:
    variables = {"filter": filter} if filter else None
    result = await call_boligflow(GET_PROPERTIES_QUERY, variables, user_credentials)
    return result.get("properties", [])


async def get_leases(user_credentials: Dict[str, Any], filter: Optional[dict] = None) -> str:
    variables = {"filter": filter} if filter else None
    result = await call_boligflow(GET_LEASES_QUERY, variables, user_credentials)
    return result.get("leases", [])
#{"filter": {"_any": {"floor": {"eq": "st"}}}} virker til den query


# #TODO evt 
# list_properties

# list_leases

# list_vat_codes

# list_contra_accounts

#Kan tilføjes så der valideres
# await validate_account(debet_konto_id) 
# await validate_account(kredit_konto_id)



# --------------------
# Utility Helper Functions
# --------------------



# --------------------
# Debug Helper Functions
# --------------------

#TODO - kaldes ikke lige nu - flyttet hertil fra utility_tools
async def verify_record(record_id: str, record_type: str = "Lease") -> str:
    """
    Verify that a record exists in Boligflow before uploading.
    
    Args:
        record_id: ID of record to verify
        record_type: Type of record (Lease, Case, Project, etc.)
    
    Examples:
        verify_record("0199955a-530e-71f8-a8ee-1592547cbe36", "Lease")
        verify_record("12345", "Case")
    """
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
            return json.dumps({
                "exists": False,
                "error": "GraphQL errors",
                "details": result["errors"]
            }, indent=2, ensure_ascii=False)
        
        if "data" in result:
            record_data = result["data"].get(record_type.lower())
            
            if record_data is None:
                return json.dumps({
                    "exists": False,
                    "message": f"{record_type} with ID {record_id} not found"
                }, indent=2, ensure_ascii=False)
            
            return json.dumps({
                "exists": True,
                "record_type": record_data.get("__typename"),
                "record_id": record_data.get("id"),
                "message": f"{record_type} exists and is accessible"
            }, indent=2, ensure_ascii=False)
        
        return json.dumps({
            "exists": False,
            "message": "Unexpected response format",
            "response": result
        }, indent=2, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({
            "exists": False,
            "error": str(e)
        }, indent=2, ensure_ascii=False)
    

#TODO - kaldes ikke lige nu - flyttet hertil fra email_tools. Potentielt lave nogle error tools
def debug_env_vars() -> str:
    """Debug tool to check environment variables"""
    import os
    return json.dumps({
        "IMAP_SERVER": os.getenv("IMAP_SERVER"),
        "IMAP_PORT": os.getenv("IMAP_PORT"),
        "EMAIL_USER": os.getenv("EMAIL_USER"),
        "EMAIL_PASSWORD": "***" if os.getenv("EMAIL_PASSWORD") else None,
    }, indent=2)