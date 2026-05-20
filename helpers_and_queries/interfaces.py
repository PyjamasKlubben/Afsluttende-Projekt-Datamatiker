"""
Interfaces (Protocols) for external integrations.

These protocols define contracts for external dependencies, enabling:
- Testability via mocks without hitting real APIs
- Swappable implementations (e.g. IMAP → Microsoft Graph)
- Clear separation between tool logic and I/O
"""

from typing import Protocol, Optional, Any


# --------------------
# Email
# --------------------

class EmailProvider(Protocol):
    """
    Contract for fetching and managing emails from any mail backend.
    Current implementation: IMAP (ImapEmailProvider in mcp_helpers.py)
    """

    def get_emails(
        self,
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
        unread_only: bool = True,
        max_emails: int = 20,
        search_limit: int = 100,
    ) -> tuple[list[dict], int]:
        """
        Fetch emails with optional filtering.

        Args:
            sender_filter: Only return emails from this address (substring match)
            subject_filter: Only return emails with this text in subject (substring match)
            unread_only: If True, only return unread/UNSEEN emails
            max_emails: Max emails to return after filtering
            search_limit: Max emails to scan before filtering

        Returns:
            Tuple of (list of email dicts, number of emails scanned)
        """
        ...

    def mark_as_read(self, email_id: str) -> None:
        """Mark a single email as read by its IMAP ID."""
        ...


# --------------------
# Boligflow API
# --------------------

class BoligflowClient(Protocol):
    """
    Contract for communicating with the Boligflow GraphQL API.
    Current implementation: HTTP via httpx (HttpBoligflowClient in mcp_helpers.py)
    """

    async def query(
        self,
        query: str,
        variables: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Execute a GraphQL query or mutation.

        Args:
            query: GraphQL query/mutation string
            variables: Optional variable dict

        Returns:
            Parsed JSON response dict
        """
        ...

    async def upload_file(
        self,
        record_id: str,
        file_data: dict[str, Any],
        fileable_type: str = "Case",
    ) -> dict[str, Any]:
        """
        Upload a file (eml or attachment) to a Boligflow record.

        Args:
            record_id: ID of the record to attach the file to
            file_data: Dict with filename, content_type, and raw bytes or base64 data
            fileable_type: Boligflow record type (Case, Project, etc.)

        Returns:
            Parsed JSON response dict
        """
        ...


# --------------------
# Cache
# --------------------

class QueryCache(Protocol):
    """
    Contract for persisting GraphQL query cache entries.
    Current implementation: JSON file (JsonQueryCache in mcp_helpers.py)
    """

    def load(self) -> dict[str, Any]:
        """Load and return the full cache dict."""
        ...

    def save(self, cache: dict[str, Any]) -> None:
        """Persist the full cache dict."""
        ...