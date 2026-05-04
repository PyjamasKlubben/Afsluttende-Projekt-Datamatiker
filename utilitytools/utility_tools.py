import os
import json
import sys
from pathlib import Path
from typing import Optional

from starlette.responses import JSONResponse
from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import from package when running in Docker, from relative when running locally
try:
    from helpers_and_queries.mcp_tools_queries import GET_TENANTS_QUERY, GET_TYPES_QUERY, GET_TYPE_DETAILS_QUERY, CREATE_FILE_MUTATION, CREATE_ORPHANED_FILE_MUTATION, GET_INPUT_TYPE_QUERY, GET_TYPE_DETAILS_QUERY, GET_TYPES_QUERY
    from helpers_and_queries.mcp_helpers import auto_cache_query, call_boligflow
except ImportError:
    from helpers_and_queries.mcp_tools_queries import GET_TENANTS_QUERY, GET_TYPES_QUERY, GET_TYPE_DETAILS_QUERY, CREATE_FILE_MUTATION, CREATE_ORPHANED_FILE_MUTATION, GET_INPUT_TYPE_QUERY, GET_TYPE_DETAILS_QUERY, GET_TYPES_QUERY
    from helpers_and_queries.mcp_helpers import auto_cache_query, call_boligflow

from dotenv import load_dotenv
load_dotenv()

# --------------------
# Config
# --------------------

BASE_URL = os.environ.get("BASE_URL") 
COMPANY = os.environ.get("COMPANY")
INTEGRATION = os.environ.get("INTEGRATION")

API_KEY = os.environ.get("API_KEY")

# --------------------
# MCP Server
# --------------------

mcp = FastMCP("Utility Tools")


# --------------------
# Original MCP Tools
# --------------------

@mcp.tool()
async def call_graphql(query: str, variables: Optional[str] = None) -> str:
    """
    Run a GraphQL query against Boligflow API.
    Successful queries are automatically saved to cache for future reuse.
    If a cached query with the same operation name already exists, it is
    automatically replaced with the new version.

    IMPORTANT: Before using this tool, FIRST use search_cached_queries to check
    if a matching query already exists in the cache. This saves tokens and time.

    IMPORTANT: Always include an operation name in your queries (e.g. 'query GetTenants { ... }'
    instead of 'query { ... }'). This ensures proper caching, deduplication and cache updates.

    IMPORTANT: Always include paginatorInfo { total currentPage lastPage } in queries that return
    lists, and use 'first' and 'page' arguments. If total > first, you MUST fetch all pages.
    For n8n workflows, include pagination logic in the Code node to loop through all pages.

    Args:
        query: A GraphQL query string. Must include an operation name.
        variables: Optional JSON string of variables (e.g. '{"typeName": "Lease"}')
    """
    parsed_vars = json.loads(variables) if variables else None
    result = await call_boligflow(query, parsed_vars)
    auto_cache_query(query, parsed_vars, result)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_tenants() -> str:
    """
    Get tenants from Boligflow.
    """
    return await call_graphql(GET_TENANTS_QUERY)


@mcp.tool()
async def get_types() -> str:
    """
    Get types from Boligflow API via introspection.
    """
    return await call_graphql(GET_TYPES_QUERY)


@mcp.tool()
async def get_details_types() -> str:
    """
    Get details of types from Boligflow API via introspection.
    """
    return await call_graphql(GET_TYPE_DETAILS_QUERY)


@mcp.tool()
async def create_file(
    filename: str,
    content_base64: str,
    mime_type: str = "text/plain"
) -> str:
    """
    Create/upload a file to Boligflow using the createFile mutation.
    
    Args:
        filename: Name of the file
        content_base64: File content encoded as base64
        mime_type: MIME type (e.g., 'application/pdf', 'image/jpeg', 'text/plain')
    """
    
    variables = {
        "input": {
            "filename": filename,
            "content": content_base64,
            "mime_type": mime_type
        }
    }
    
    result = await call_boligflow(CREATE_FILE_MUTATION, variables)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def create_orphaned_file(
    filename: str,
    content_base64: str,
    mime_type: str = "application/pdf"
) -> str:
    """
    Create an orphaned file (not attached to any resource yet) in Boligflow.
    Useful for uploading files that will be attached later.
    
    Args:
        filename: Name of the file
        content_base64: File content encoded as base64
        mime_type: MIME type (e.g., 'application/pdf', 'image/jpeg')
    """
    
    variables = {
        "input": {
            "filename": filename,
            "content": content_base64,
            "mime_type": mime_type
        }
    }
    
    result = await call_boligflow(CREATE_ORPHANED_FILE_MUTATION, variables)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_input_type_details(type_name: str) -> str:
    """
    Get details about a specific GraphQL input type to understand what fields are required.
    Useful for discovering the correct structure for mutations.
    
    Args:
        type_name: Name of the input type (e.g., "CreateInspectionInput", "CreateFileInput")
    """
    
    variables = {"typeName": type_name}
    
    result = await call_boligflow(GET_INPUT_TYPE_QUERY, variables)
    return json.dumps(result, indent=2, ensure_ascii=False)



# --------------------
# Debug Tools (kept from original)
# --------------------

@mcp.tool()
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


# Optional: allow local running via `python bf_praktik.py`
if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "3003"))
    )