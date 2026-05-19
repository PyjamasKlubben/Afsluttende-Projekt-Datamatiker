import logging
import os
import json
import sys
from typing import Optional
from pathlib import Path
from starlette.responses import JSONResponse
from fastmcp import FastMCP, Context

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import from package when running in Docker, from relative when running locally
try:
    from helpers_and_queries.mcp_helpers import upload_attachment_to_boligflow, get_unread_emails, mark_email_as_read, upload_email_to_boligflow, get_emails_by_sender
    from helpers_and_queries.mcp_credentials import UserSession
except ImportError:
    from helpers_and_queries.mcp_helpers import upload_attachment_to_boligflow, get_unread_emails, mark_email_as_read, upload_email_to_boligflow, get_emails_by_sender
    from helpers_and_queries.mcp_credentials import UserSession

from dotenv import load_dotenv
load_dotenv()


# Konfigurer logging til stderr
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr,
    force=True
)

logger = logging.getLogger(__name__)


# --------------------
# Config
# --------------------

BASE_URL = os.environ.get("BASE_URL") 
COMPANY = os.environ.get("COMPANY")
INTEGRATION = os.environ.get("INTEGRATION")



# --------------------
# MCP Server
# --------------------
mcp = FastMCP("Email Tools")


# --------------------
# Email MCP Tools
# --------------------

@mcp.tool()
async def list_unread_emails(
    ctx: Context,
    subject_filter: Optional[str] = None,
    max_emails: int = 20,
    search_limit: int = 100
) -> str:
    """
    List unread emails in inbox, optionally filtered by subject or sender.
    Returns metadata without uploading.

    Args:
        subject_filter: Optional text that should be in subject
        sender_filter: Optional email address that should be from sender
        max_emails: Maximum number of emails to list - default: 20
        search_limit: Maximum number of unread emails to search through - default: 100

    Examples:
        list_unread_emails()
        list_unread_emails(subject_filter="Faktura")
        list_unread_emails(sender_filter="blank@gmail.com")
        list_unread_emails(sender_filter="blank@gmail.com")
        list_unread_emails(subject_filter="Support", sender_filter="blank@hotmail.com", max_emails=50, search_limit=500)
    """
    try:
        session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]

        import asyncio
        loop = asyncio.get_event_loop()
        emails, emails_searched = await loop.run_in_executor(
            None,
            get_unread_emails,
            session.env.get("EMAIL", ""),
            session.env.get("EMAIL_PASSWORD", ""),
            subject_filter,
            max_emails,
            search_limit
        )

        if not emails:
            msg = "No unread emails found"
            if subject_filter:
                msg += f" with subject containing: '{subject_filter}'"
            return json.dumps({
                "success": True,
                "count": 0,
                "emails_searched": emails_searched,
                "message": msg
            }, indent=2, ensure_ascii=False)

        # Format output
        email_list = []
        for email_data in emails:
            email_list.append({
                "subject": email_data['subject'],
                "from": email_data['from_email'],
                "date": email_data['date'],
                "attachments_count": len(email_data['attachments']),
                "id": email_data['id']
            })

        return json.dumps({
            "success": True,
            "count": len(email_list),
            "emails_searched": emails_searched,
            "emails": email_list
        }, indent=2, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2, ensure_ascii=False)



@mcp.tool()
async def list_emails_by_sender(
    ctx: Context,
    sender_filter: Optional[str] = None,
    subject_filter: Optional[str] = None,
    max_emails: int = 20,
    search_limit: int = 200
) -> str:
    """
    List ALL emails (read + unread), filtered by sender and/or subject.
    Args:
        subject_filter: Optional text that should be in subject
        sender_filter: Optional email address that should be from sender
        max_emails: Maximum number of emails to list - default: 20
        search_limit: Maximum number of unread emails to search through - default: 100

    Examples:
        list_emails_by_sender()
        list_emails_by_sender(subject_filter="Faktura")
        list_emails_by_sender(sender_filter="blank@gmail.com")
        list_emails_by_sender(subject_filter="Support", sender_filter="blank@hotmail.com", max_emails=50, search_limit=500)

    Returns metadata only.
    """
    try:
        session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]

        import asyncio
        loop = asyncio.get_event_loop()

        emails, emails_searched = await loop.run_in_executor(
            None,
            get_emails_by_sender,
            session.env.get("EMAIL", ""),
            session.env.get("EMAIL_PASSWORD", ""),
            sender_filter,
            subject_filter,
            max_emails,
            search_limit
        )

        if not emails:
            msg = "No emails found"
            if sender_filter:
                msg += f" from sender containing: '{sender_filter}'"
            if subject_filter:
                msg += f" with subject containing: '{subject_filter}'"

            return json.dumps({
                "success": True,
                "count": 0,
                "emails_searched": emails_searched,
                "message": msg
            }, indent=2, ensure_ascii=False)

        email_list = []
        for email_data in emails:
            email_list.append({
                "subject": email_data['subject'],
                "from": email_data['from_email'],
                "date": email_data['date'],
                "attachments_count": len(email_data['attachments']),
                "id": email_data['id']
            })

        return json.dumps({
            "success": True,
            "count": len(email_list),
            "emails_searched": emails_searched,
            "emails": email_list
        }, indent=2, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2, ensure_ascii=False)



@mcp.tool()
async def upload_email_only(
    ctx: Context,
    record_id: str,
    subject_filter: str,
    fileable_type: str = "Case",
    mark_as_read: bool = True,
    max_emails: int = 10,
    search_limit: int = 100
) -> str:
    """
    Scan inbox and upload ONLY the .eml file (no attachments).

    Args:
        record_id: ID of record to attach email to
        subject_filter: Text that should be in subject
        fileable_type: Type of record (Case, Project, etc.) - default: Case
        mark_as_read: Mark emails as read after upload - default: True
        max_emails: Maximum number of emails to upload - default: 10
        search_limit: Maximum number of unread emails to search through - default: 100

    Examples:
        upload_email_only("12345", "Support Ticket")
        upload_email_only("67890", "Faktura", fileable_type="Project")
    """
    try:
        session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]

        import asyncio
        loop = asyncio.get_event_loop()
        emails, emails_searched = await loop.run_in_executor(
            None,
            get_unread_emails,
            session.env.get("EMAIL", ""),
            session.env.get("EMAIL_PASSWORD", ""),
            subject_filter,
            max_emails,
            search_limit
        )

        if not emails:
            return json.dumps({
                "success": False,
                "message": f"No unread emails found with subject containing: '{subject_filter}'"
            }, indent=2, ensure_ascii=False)

        results = []

        for email_data in emails:
            email_result = {
                "email_subject": email_data["subject"],
                "email_from": email_data["from_email"]
            }

            try:
                result = await upload_email_to_boligflow(record_id, email_data, fileable_type, session.env)
                email_result["eml_upload_result"] = result
                email_result["success"] = True

                if mark_as_read:
                    await loop.run_in_executor(None, mark_email_as_read, email_data["id"], session.env.get("EMAIL", ""), session.env.get("EMAIL_PASSWORD", ""))
                    email_result["marked_as_read"] = True

            except Exception as e:
                email_result["success"] = False
                email_result["error"] = str(e)

            results.append(email_result)

        return json.dumps({
            "success": True,
            "uploaded_count": len(results),
            "emails_searched": emails_searched,
            "results": results
        }, indent=2, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2, ensure_ascii=False)


@mcp.tool()
async def upload_attachments_only(
    ctx: Context,
    record_id: str,
    subject_filter: str,
    fileable_type: str = "Case",
    mark_as_read: bool = True,
    max_emails: int = 5,
    search_limit: int = 50
) -> str:
    """
    Scan inbox and upload ONLY attachments (no .eml file).

    Args:
        record_id: ID of record to attach files to
        subject_filter: Text that should be in subject
        fileable_type: Type of record (Case, Project, etc.) - default: Case
        mark_as_read: Mark emails as read after upload - default: True
        max_emails: Maximum number of emails to process - default: 5
        search_limit: Maximum number of unread emails to search through - default: 50

    Examples:
        upload_attachments_only("12345", "Invoice")
        upload_attachments_only("67890", "Bilag", fileable_type="Project")
    """
    import asyncio
    loop = asyncio.get_event_loop()

    try:
        session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]

        emails, searched = await loop.run_in_executor(
            None,
            get_unread_emails,
            session.env.get("EMAIL", ""),
            session.env.get("EMAIL_PASSWORD", ""),
            subject_filter,
            max_emails,
            search_limit
        )

        if not emails:
            return json.dumps({
                "success": False,
                "message": f"No unread emails found with subject containing '{subject_filter}'"
            }, indent=2, ensure_ascii=False)

        results = []

        for email_data in emails:
            email_result = {
                "subject": email_data["subject"],
                "from": email_data["from_email"],
                "attachments_found": len(email_data["attachments"]),
                "uploaded": []
            }

            for att in email_data["attachments"]:
                try:
                    upload_result = await upload_attachment_to_boligflow(
                        record_id, att, fileable_type, session.env
                    )
                    email_result["uploaded"].append({
                        "filename": att["filename"],
                        "status": "uploaded",
                        "result": upload_result
                    })
                except Exception as e:
                    email_result["uploaded"].append({
                        "filename": att["filename"],
                        "status": "error",
                        "error": str(e)
                    })

            if mark_as_read:
                try:
                    await loop.run_in_executor(None, mark_email_as_read, email_data["id"], session.env.get("EMAIL", ""), session.env.get("EMAIL_PASSWORD", ""))
                    email_result["marked_as_read"] = True
                except Exception as e:
                    email_result["marked_as_read"] = False
                    email_result["mark_error"] = str(e)

            results.append(email_result)

        return json.dumps({
            "success": True,
            "record_id": record_id,
            "fileable_type": fileable_type,
            "emails_processed": len(results),
            "emails_searched": searched,
            "results": results
        }, indent=2, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2, ensure_ascii=False)


@mcp.tool()
async def upload_email_with_attachments(
    ctx: Context,
    record_id: str,
    subject_filter: str,
    fileable_type: str = "Case",
    mark_as_read: bool = True,
    max_emails: int = 10,
    search_limit: int = 100
) -> str:
    """
    Scan email inbox and upload BOTH .eml file AND all attachments.

    Args:
        record_id: ID of record to attach email to
        subject_filter: Text that should be in subject (e.g., 'Faktura', 'Ordre')
        fileable_type: Type of record (Case, Project, etc.) - default: Case
        mark_as_read: Mark emails as read after upload - default: True
        max_emails: Maximum number of emails to upload - default: 10
        search_limit: Maximum number of unread emails to search through - default: 100

    Examples:
        upload_email_with_attachments("12345", "Support Ticket")
        upload_email_with_attachments("67890", "Faktura", fileable_type="Project")
        upload_email_with_attachments("99999", "Ordre", search_limit=500)
    """
    try:
        session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]

        import asyncio
        loop = asyncio.get_event_loop()
        emails, emails_searched = await loop.run_in_executor(
            None,
            get_unread_emails,
            session.env.get("EMAIL", ""),
            session.env.get("EMAIL_PASSWORD", ""),
            subject_filter,
            max_emails,
            search_limit
        )

        if not emails:
            return json.dumps({
                "success": False,
                "message": f"No unread emails found with subject containing: '{subject_filter}'"
            }, indent=2, ensure_ascii=False)

        results = []

        for email_data in emails:
            email_result = {
                "email_subject": email_data["subject"],
                "email_from": email_data["from_email"],
                "attachments_count": len(email_data["attachments"]),
                "uploaded_attachments": []
            }

            try:
                result = await upload_email_to_boligflow(record_id, email_data, fileable_type, session.env)
                email_result["eml_upload_result"] = result
                email_result["eml_success"] = True

                for att in email_data["attachments"]:
                    try:
                        att_result = await upload_attachment_to_boligflow(record_id, att, fileable_type, session.env)
                        email_result["uploaded_attachments"].append({
                            "filename": att["filename"],
                            "success": True,
                            "result": att_result
                        })
                    except Exception as e:
                        email_result["uploaded_attachments"].append({
                            "filename": att["filename"],
                            "success": False,
                            "error": str(e)
                        })

                if mark_as_read:
                    await loop.run_in_executor(None, mark_email_as_read, email_data["id"], session.env.get("EMAIL", ""), session.env.get("EMAIL_PASSWORD", ""))
                    email_result["marked_as_read"] = True

                email_result["success"] = True
                results.append(email_result)

            except Exception as e:
                email_result["eml_success"] = False
                email_result["error"] = str(e)
                email_result["success"] = False
                results.append(email_result)

        return json.dumps({
            "success": True,
            "uploaded_count": len(results),
            "emails_searched": emails_searched,
            "results": results
        }, indent=2, ensure_ascii=False)
    
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2, ensure_ascii=False)
    


# --------------------
# Health Check Route
# --------------------

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "service": "mcp-server"})


# --------------------
# FastMCP Entrypoint (required for hosting)
# --------------------

def main():
    """
    FastMCP entrypoint.
    Prefect Horizon / FastMCP Cloud will call this function
    to obtain the MCP server instance.
    """
    return mcp


# Optional: allow local running via `python email_tools.py`
if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "3005"))
    )
