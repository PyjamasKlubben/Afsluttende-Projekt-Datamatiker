import os
import json
import sys
from typing import Optional
from pathlib import Path
from datetime import datetime

import mcp
from starlette.responses import JSONResponse
from fastmcp import FastMCP, Context

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from helpers_and_queries.mcp_helpers import call_boligflow, load_cache, save_cache
    from helpers_and_queries.mcp_credentials import UserSession
except ImportError:
    from helpers_and_queries.mcp_helpers import call_boligflow, load_cache, save_cache
    from helpers_and_queries.mcp_credentials import UserSession


from dotenv import load_dotenv
load_dotenv()

# --------------------
# Config
# --------------------

BASE_URL = os.environ.get("BASE_URL") 
COMPANY = os.environ.get("COMPANY")
INTEGRATION = os.environ.get("INTEGRATION")


# Query Cache
CACHE_FILE = Path(__file__).parent / "query_cache.json"


# --------------------
# MCP Server
# --------------------
mcp = FastMCP("Cache Query Tools")


# --------------------
# Query Cache MCP Tools
# --------------------

@mcp.tool()
async def search_cached_queries(search_text: str) -> str:
    """
    Search for cached GraphQL queries by keyword. Use this FIRST before running introspection
    queries or constructing new queries. This saves significant tokens and time.

    Args:
        search_text: Keywords to search for (e.g. "tenants", "leases", "invoices")
    """
    cache = load_cache()
    search_lower = search_text.lower()
    matches = []

    for q in cache["queries"]:
        score = 0
        if search_lower in q["description"].lower():
            score += 2
        for kw in q["intent_keywords"]:
            if search_lower in kw.lower() or kw.lower() in search_lower:
                score += 1
        if search_lower in q["query"].lower():
            score += 1
        if score > 0:
            matches.append({**q, "_relevance": score})

    matches.sort(key=lambda x: (-x["_relevance"], -x["use_count"]))

    for m in matches:
        del m["_relevance"]

    return json.dumps({
        "found": len(matches),
        "search_text": search_text,
        "queries": matches
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def execute_cached_query(ctx: Context, query_id: str, variables: Optional[str] = None) -> str:
    """
    Execute a cached query by its ID. Updates usage statistics.
    If a query fails with a GraphQL error, it is automatically removed from cache.
    Server/network errors do NOT count as query failures.

    Args:
        query_id: The ID of the cached query (e.g. "get-tenants-basic")
        variables: Optional JSON string of variables (e.g. '{"typeName": "Lease"}')
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    cache = load_cache()
    query_entry = None

    for q in cache["queries"]:
        if q["id"] == query_id:
            query_entry = q
            break

    if not query_entry:
        return json.dumps({"error": f"Query '{query_id}' not found in cache"}, indent=2)

    parsed_vars = None
    if variables:
        parsed_vars = json.loads(variables)
    elif query_entry.get("variables"):
        parsed_vars = query_entry["variables"]

    try:
        result = await call_boligflow(query_entry["query"], parsed_vars, session.env)
    except Exception as e:
        # HTTP/network error (server-side) - don't count as query failure
        return json.dumps({"error": f"Server error (not a query problem): {str(e)}"}, indent=2)

    query_entry["last_used"] = datetime.now().strftime("%Y-%m-%d")
    query_entry["use_count"] = query_entry.get("use_count", 0) + 1

    if "errors" in result:
        # GraphQL error - the query itself is broken, remove immediately
        cache["queries"] = [q for q in cache["queries"] if q["id"] != query_id]
        save_cache(cache)
        result["_cache_warning"] = f"Query '{query_id}' removed from cache due to GraphQL error. Use introspection to build a new query."
    else:
        # Success
        save_cache(cache)

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def list_cached_queries() -> str:
    """
    List all cached queries with their IDs, descriptions and usage stats.
    """
    cache = load_cache()
    summary = []
    for q in cache["queries"]:
        summary.append({
            "id": q["id"],
            "description": q["description"],
            "keywords": q["intent_keywords"],
            "use_count": q.get("use_count", 0),
            "last_used": q.get("last_used", "never")
        })
    return json.dumps({"total": len(summary), "queries": summary}, indent=2, ensure_ascii=False)


@mcp.tool()
async def delete_cached_query(query_id: str) -> str:
    """
    Delete a query from the cache.

    Args:
        query_id: The ID of the query to delete
    """
    cache = load_cache()
    original_count = len(cache["queries"])
    cache["queries"] = [q for q in cache["queries"] if q["id"] != query_id]

    if len(cache["queries"]) == original_count:
        return json.dumps({"success": False, "message": f"Query '{query_id}' not found"}, indent=2)

    save_cache(cache)
    return json.dumps({"success": True, "message": f"Deleted query '{query_id}'"}, indent=2)


@mcp.tool()
async def export_queries_for_n8n() -> str:
    """
    Export all cached queries in a format ready for n8n workflows.
    Each query includes the full GraphQL string and default variables,
    so n8n can execute them directly without needing Claude/introspection.
    """
    cache = load_cache()
    exports = []
    for q in cache["queries"]:
        exports.append({
            "id": q["id"],
            "description": q["description"],
            "graphql_query": q["query"],
            "default_variables": q.get("variables"),
            "use_count": q.get("use_count", 0)
        })
    return json.dumps({
        "total": len(exports),
        "api_endpoint": BASE_URL,
        "required_headers": {
            "Authorization": "Bearer <API_KEY>",
            "Company": COMPANY,
            "X-Boligflow-Integration": INTEGRATION,
            "Content-Type": "application/json"
        },
        "queries": exports
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def clear_all_cached_queries() -> str:
    """
    Delete ALL cached queries. Use this to start fresh with an empty cache.
    """
    cache = load_cache()
    count = len(cache.get("queries", []))
    cache["queries"] = []
    save_cache(cache)
    return json.dumps({
        "success": True,
        "deleted": count,
        "message": f"All {count} cached queries deleted. Cache is now empty."
    }, indent=2)


@mcp.tool()
async def cleanup_cached_queries(ctx: Context) -> str:
    """
    Clean up the query cache by removing duplicates, unnamed queries (auto-xxx),
    and queries that no longer work against the current GraphQL schema.
    Returns a report of what was removed and what remains.
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    cache = load_cache()
    original_count = len(cache["queries"])

    removed = []
    kept = []
    seen_queries = {}  # normalized query text -> first query entry

    for q in cache["queries"]:
        query_id = q["id"]
        query_text = q["query"].strip()

        # Remove unnamed queries (auto-xxx)
        if query_id.startswith("auto-"):
            removed.append({"id": query_id, "reason": "Unnamed query (auto-generated ID)"})
            continue

        # Remove duplicates (same query text, keep the one with highest use_count)
        normalized = " ".join(query_text.split())
        if normalized in seen_queries:
            existing = seen_queries[normalized]
            if q.get("use_count", 0) > existing.get("use_count", 0):
                # Replace existing with this one (higher use count)
                removed.append({"id": existing["id"], "reason": f"Duplicate of '{query_id}' (lower use count)"})
                kept.remove(existing)
                seen_queries[normalized] = q
                kept.append(q)
            else:
                removed.append({"id": query_id, "reason": f"Duplicate of '{existing['id']}'"})
            continue

        seen_queries[normalized] = q
        kept.append(q)

    # Validate remaining queries against current schema
    validated = []
    for q in kept:
        try:
            result = await call_boligflow(q["query"], q.get("variables"), session.env)
            if "errors" in result:
                error_msg = result["errors"][0].get("message", "Unknown error")
                removed.append({"id": q["id"], "reason": f"Schema validation failed: {error_msg}"})
            else:
                validated.append(q)
        except Exception:
            validated.append(q)

    # Save cleaned cache
    cache["queries"] = validated
    save_cache(cache)

    return json.dumps({
        "summary": f"Removed {len(removed)} queries, kept {len(validated)} (was {original_count})",
        "removed": removed,
        "remaining": [{"id": q["id"], "description": q.get("description", ""), "use_count": q.get("use_count", 0)} for q in validated]
    }, indent=2, ensure_ascii=False)



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
    port = int(os.getenv("PORT", "3004"))
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port
        )