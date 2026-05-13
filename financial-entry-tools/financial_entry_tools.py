import os
import json
import sys
from typing import Any, Dict, Optional
from pathlib import Path


import mcp
from starlette.responses import JSONResponse
from fastmcp import FastMCP

from utilitytools.utility_tools import call_graphql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import from package when running in Docker, from relative when running locally
try:
    from helpers_and_queries.mcp_tools_queries import CREATE_JOURNAL_ENTRY_MUTATION, GET_ACCOUNTS_QUERY, GET_LEASES_QUERY, GET_PROPERTIES_QUERY
    from helpers_and_queries.mcp_helpers import call_boligflow
except ImportError:
    from helpers_and_queries.mcp_tools_queries import CREATE_JOURNAL_ENTRY_MUTATION, GET_ACCOUNTS_QUERY, GET_LEASES_QUERY, GET_PROPERTIES_QUERY
    from helpers_and_queries.mcp_helpers import call_boligflow

from dotenv import load_dotenv
load_dotenv()

# --------------------
# Config
# --------------------

BASE_URL = os.environ.get("BASE_URL") 
COMPANY = os.environ.get("COMPANY")
INTEGRATION = os.environ.get("INTEGRATION")

API_KEY = os.getenv("API_KEY")


# --------------------
# MCP Server
# --------------------

mcp = FastMCP("Financial Entry Tools")


# --------------------
# Financial (Journal Entry) tools
# --------------------

@mcp.tool()
async def create_financial_entry(
    bilagsdato: str,
    beskrivelse: str,
    beloeb: float,
    debet_konto_id: str,
    kredit_konto_id: str,
    debet_beskrivelse: str,
    kredit_beskrivelse: str,
    debet_dimensionable_type: Optional[str] = None,
    debet_dimensionable_id: Optional[str] = None,
    kredit_dimensionable_type: Optional[str] = None,
    kredit_dimensionable_id: Optional[str] = None,
    should_post: bool = False
) -> str:
    """
    Opret et finansbilag (journal entry) i Boligflow.

    Args:
        bilagsdato: Bilagsdato i format YYYY-MM-DD
        beskrivelse: Overordnet beskrivelse af bilaget
        beloeb: Beløb (positivt tal)
        debet_konto_id: Debet konto (account) ID
        kredit_konto_id: Kredit konto (account) ID
        debet_beskrivelse: Beskrivelse for debetlinjen
        kredit_beskrivelse: Beskrivelse for kreditlinjen
        debet_dimensionable_type: Type af dimensionable for debet (f.eks. 'Lease', 'Property', 'LeaseAgreement')
        debet_dimensionable_id: ID for debet dimensionable
        kredit_dimensionable_type: Type af dimensionable for kredit (f.eks. 'Lease', 'Property', 'LeaseAgreement')
        kredit_dimensionable_id: ID for kredit dimensionable
        should_post: Om bilaget skal bogføres med det samme
    """

    # Byg debetlinje
    debet_linje: Dict[str, Any] = {
        "amount": beloeb,
        "ledgerType": "D",
        "account": {
            "connect": debet_konto_id
        },
        "contraAccount": {
            "disconnect": True
        },
        "vat": {
            "disconnect": True
        },
        "contraVat": {
            "disconnect": True
        },
        "description": debet_beskrivelse
    }

    # Tilføj dimensionable hvis angivet
    if debet_dimensionable_type and debet_dimensionable_id:
        debet_linje["dimensionable"] = {
            "connect": {
                "type": debet_dimensionable_type,
                "id": debet_dimensionable_id
            }
        }

    # Byg kreditlinje
    kredit_linje: Dict[str, Any] = {
        "amount": beloeb,
        "ledgerType": "C",
        "account": {
            "connect": kredit_konto_id
        },
        "contraAccount": {
            "disconnect": True
        },
        "vat": {
            "disconnect": True
        },
        "contraVat": {
            "disconnect": True
        },
        "description": kredit_beskrivelse
    }

    # Tilføj dimensionable hvis angivet
    if kredit_dimensionable_type and kredit_dimensionable_id:
        kredit_linje["dimensionable"] = {
            "connect": {
                "type": kredit_dimensionable_type,
                "id": kredit_dimensionable_id
            }
        }

    input_data: Dict[str, Any] = {
        "shouldPost": should_post,
        "description": beskrivelse,
        "date": bilagsdato,
        "transactionLines": {
            "create": [debet_linje, kredit_linje]
        },
        "files": {
            "connect": []
        }
    }

    variables = {"input": input_data}

    result = await call_boligflow(CREATE_JOURNAL_ENTRY_MUTATION, variables)
    return json.dumps(result, indent=2, ensure_ascii=False)


# --------------------
# Health Check Route
# --------------------


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "service": "mcp-server"})

# --------------------
# Entrypoint
# --------------------

def main():
    """
    FastMCP entrypoint.
    Prefect Horizon / FastMCP Cloud will call this function
    to obtain the MCP server instance.
    """
    return mcp


if __name__ == "__main__":
    port = int(os.getenv("PORT", "3002"))
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port
        )