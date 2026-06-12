import os
import json
import sys
from pathlib import Path

import mcp
from fastmcp import FastMCP, Context
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import from packagee
try:
    from helpers_and_queries.mcp_helpers import call_boligflow, call_n8n, _is_template
    from helpers_and_queries.mcp_credentials import UserSession
except ImportError:
    from helpers_and_queries.mcp_helpers import call_boligflow, call_n8n, _is_template
    from helpers_and_queries.mcp_credentials import UserSession

from dotenv import load_dotenv
load_dotenv()

# --------------------
# Config
# --------------------

BASE_URL = os.environ.get("BASE_URL") 
COMPANY = os.environ.get("COMPANY")
INTEGRATION = os.environ.get("INTEGRATION")

TEMPLATE_PREFIX = "[TEMPLATE]"

# Query Cache
CACHE_FILE = Path(__file__).parent / "query_cache.json"


# --------------------
# MCP Server
# --------------------
mcp = FastMCP("n8n Workflow Tools")


# --------------------
# n8n Workflow MCP Tools
# --------------------

@mcp.tool()
async def list_n8n_workflows(ctx: Context) -> str:
    """
    List available n8n template workflows. Only returns [TEMPLATE] workflows.
    These are master templates that should be cloned (via clone_n8n_workflow) before modification.
    NEVER use or modify non-template workflows.
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    try:
        result = await call_n8n("GET", "/workflows", user_credentials=session.env)
        if "error" in result:
            return json.dumps(result, indent=2)

        workflows = result.get("data", result) if isinstance(result, dict) else result
        if isinstance(workflows, dict) and "data" in workflows:
            workflows = workflows["data"]

        templates = []
        for wf in workflows:
            if wf.get("name", "").startswith(TEMPLATE_PREFIX):
                entry = {
                    "id": wf.get("id"),
                    "name": wf.get("name"),
                    "active": wf.get("active"),
                }
                # Fetch full workflow to extract sticky note description
                try:
                    full_wf = await call_n8n("GET", f"/workflows/{wf.get('id')}", user_credentials=session.env)
                    if "error" not in full_wf:
                        for node in full_wf.get("nodes", []):
                            if node.get("type") == "n8n-nodes-base.stickyNote":
                                entry["description"] = node.get("parameters", {}).get("content", "")
                                break
                except Exception:
                    pass
                templates.append(entry)

        return json.dumps({
            "total": len(templates),
            "templates": templates,
            "instruction": "Read each template's description to choose the best match for the user's request."
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def get_n8n_workflow(ctx: Context, workflow_id: str) -> str:
    """
    Get full details of an n8n workflow including all nodes, parameters and connections.

    Args:
        workflow_id: The ID of the workflow to retrieve
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    try:
        result = await call_n8n("GET", f"/workflows/{workflow_id}", user_credentials=session.env)
        if "error" in result:
            return json.dumps(result, indent=2)

        # Return a useful summary with node details
        nodes_summary = []
        for node in result.get("nodes", []):
            nodes_summary.append({
                "name": node.get("name"),
                "type": node.get("type"),
                "parameters": node.get("parameters", {})
            })

        return json.dumps({
            "id": result.get("id"),
            "name": result.get("name"),
            "active": result.get("active"),
            "is_template": _is_template(result),
            "nodes": nodes_summary,
            "connections": result.get("connections", {})
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def clone_n8n_workflow(ctx: Context, template_workflow_id: str, new_name: str) -> str:
    """
    Clone a [TEMPLATE] workflow to create a new editable workflow.
    The source workflow MUST be a template (name starts with [TEMPLATE]).
    Use list_n8n_workflows to find available templates.
    After cloning, always read the template's Sticky Note for configuration instructions.

    Args:
        template_workflow_id: ID of the template workflow to clone
        new_name: Name for the new workflow (without [TEMPLATE] prefix)
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    try:
        # Get template
        template = await call_n8n("GET", f"/workflows/{template_workflow_id}", user_credentials=session.env)
        if "error" in template:
            return json.dumps(template, indent=2)

        # Strip [TEMPLATE] prefix if Claude accidentally includes it
        if new_name.startswith(TEMPLATE_PREFIX):
            new_name = new_name[len(TEMPLATE_PREFIX):].strip()

        if not _is_template(template):
            return json.dumps({
                "error": f"Workflow '{template.get('name')}' is not a template. Only [TEMPLATE] workflows can be cloned."
            }, indent=2)

        # Add template source tag to workflow name
        template_short = template.get("name", "").replace(TEMPLATE_PREFIX, "").strip()
        tagged_name = f"{new_name} [{template_short}]"

        # Prepare clone - remove IDs so n8n creates new ones
        # Only keep settings fields that n8n API accepts on create
        allowed_settings = {"executionOrder", "timezone"}
        raw_settings = template.get("settings", {})
        clean_settings = {k: v for k, v in raw_settings.items() if k in allowed_settings}

        clone = {
            "name": tagged_name,
            "nodes": template.get("nodes", []),
            "connections": template.get("connections", {}),
            "settings": clean_settings,
        }

        # Create new workflow
        result = await call_n8n("POST", "/workflows", clone, user_credentials=session.env)
        if "error" in result:
            return json.dumps(result, indent=2)

        # Fetch enum values for Code node filtering (NOT for GraphQL query arguments)
        enum_info = {}
        try:
            enum_types = ["LeaseStatus", "LeaseAgreementStatus", "LeaseCategory", "LeaseTypeEnum",
                          "InspectionStatus", "InspectionType", "PropertyType"]
            for enum_name in enum_types:
                enum_result = await call_boligflow(f"""
                    query {{ __type(name: "{enum_name}") {{ name enumValues {{ name description }} }} }}
                """, user_credentials=session.env)
                if "errors" not in enum_result:
                    enum_data = enum_result.get("data", {}).get("__type")
                    if enum_data and enum_data.get("enumValues"):
                        enum_info[enum_name] = [ev["name"] for ev in enum_data["enumValues"]]
        except Exception:
            pass

        return json.dumps({
            "success": True,
            "new_workflow_id": result.get("id"),
            "name": result.get("name"),
            "query_workflow": (
                "MANDATORY STEPS for configuring the workflow:\n"
                "1. Search cached queries with search_cached_queries() for a matching query.\n"
                "2. If found: use it directly — it is already tested and working.\n"
                "3. If NOT found: run introspection via get_types/get_details_types/graphql to discover "
                "the correct field names.\n"
                "4. NEVER use filters in the GraphQL query. Fetch ALL data with simple queries like "
                "leases(first:100) or leaseAgreements(first:100). ALL filtering MUST happen in the Code node using JavaScript.\n"
                "5. ALWAYS test the query with graphql() BEFORE setting it in the HTTP Request node.\n"
                "6. Use the enum_values below to write correct filters in the Code node."
            ),
            "enum_values_for_code_node": enum_info,
            "enum_note": "Use these enum values for JavaScript filtering in the Code node. Example: items.filter(i => i.status === 'CANCELLED'). NEVER use them in GraphQL query arguments.",
            "next_steps": "Use update_n8n_node_param to configure nodes, then activate_n8n_workflow to turn it on."
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def update_n8n_node_param(
    ctx: Context,
    workflow_id: str,
    node_name: str,
    param_path: str,
    new_value: str
) -> str:
    """
    Update a parameter on a specific node in an n8n workflow.
    Cannot modify [TEMPLATE] workflows - clone them first.

    IMPORTANT – n8n expression syntax:
    n8n nodes do NOT support raw JavaScript like new Date(). Use n8n/Luxon expressions instead:
      - Current date (Danish): {{ $now.setLocale('da').toFormat('LLLL yyyy') }}  → "marts 2026"
      - ISO date: {{ $now.toISO() }}
      - Day/month/year: {{ $now.toFormat('dd-MM-yyyy') }}
      - Relative: {{ $now.plus({days: 7}).toFormat('dd-MM-yyyy') }}
    These expressions work in string fields like subject, toEmail, fromEmail, etc.

    Args:
        workflow_id: ID of the workflow to update
        node_name: Name of the node (e.g. "Send email", "Schedule Trigger")
        param_path: Dot-notation path to the parameter. Common paths:
            - HTTP Request JSON body: "jsonBody" (value must be a JSON string, e.g. '{"query": "..."}')
            - Send Email: "toEmail", "fromEmail", "subject", "html"
            - Schedule Trigger: "rule.interval"
            - Code node: "jsCode"
        new_value: New value as JSON string (e.g. '"peter@firma.dk"' for strings,
                   '[{"field": "cronExpression", "expression": "0 12 * * 5"}]' for objects)
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    try:
        # Get current workflow
        workflow = await call_n8n("GET", f"/workflows/{workflow_id}", user_credentials=session.env)
        if "error" in workflow:
            return json.dumps(workflow, indent=2)

        # Template protection
        if _is_template(workflow):
            return json.dumps({
                "error": f"Cannot modify template workflow '{workflow['name']}'. Use clone_n8n_workflow first."
            }, indent=2)

        # Find the node
        target_node = None
        for node in workflow.get("nodes", []):
            if node.get("name") == node_name:
                target_node = node
                break

        if not target_node:
            node_names = [n.get("name") for n in workflow.get("nodes", [])]
            return json.dumps({
                "error": f"Node '{node_name}' not found. Available nodes: {node_names}"
            }, indent=2)

        # Parse the new value from JSON string
        try:
            parsed_value = json.loads(new_value)
        except json.JSONDecodeError:
            # If it's not valid JSON, treat as plain string
            parsed_value = new_value

        # If updating Code node's jsCode, ensure GraphQL error check is present
        if target_node.get("type") == "n8n-nodes-base.code" and param_path == "jsCode" and isinstance(parsed_value, str):
            error_check = """const response = $input.all()[0].json;
if (response.errors) {
  throw new Error('GraphQL error: ' + response.errors.map(e => e.message).join(', '));
}"""
            if "$input.all()[0].json" not in parsed_value and "$input.first().json" not in parsed_value:
                parsed_value = error_check + "\n\n" + parsed_value

        # Navigate dot-notation path and set value
        params = target_node.setdefault("parameters", {})
        path_parts = param_path.split(".")

        for part in path_parts[:-1]:
            if part not in params:
                params[part] = {}
            params = params[part]

        # If value contains n8n expressions {{ }}, ensure the = prefix so n8n evaluates it
        if isinstance(parsed_value, str) and "{{" in parsed_value and "}}" in parsed_value:
            if not parsed_value.startswith("="):
                parsed_value = "=" + parsed_value
        params[path_parts[-1]] = parsed_value

        # PUT the entire workflow back
        update_body = {
            "name": workflow["name"],
            "nodes": workflow["nodes"],
            "connections": workflow["connections"],
            "settings": workflow.get("settings", {}),
        }

        result = await call_n8n("PUT", f"/workflows/{workflow_id}", update_body, user_credentials=session.env)
        if "error" in result:
            return json.dumps(result, indent=2)

        return json.dumps({
            "success": True,
            "workflow_id": workflow_id,
            "node": node_name,
            "param_path": param_path,
            "new_value": parsed_value,
            "message": f"Updated '{node_name}.{param_path}' successfully."
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def activate_n8n_workflow(ctx: Context, workflow_id: str, activate: bool = True) -> str:
    """
    Activate or deactivate an n8n workflow.
    Cannot activate/deactivate [TEMPLATE] workflows.

    Args:
        workflow_id: ID of the workflow
        activate: True to activate, False to deactivate (default: True)
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    try:
        # Check template protection
        workflow = await call_n8n("GET", f"/workflows/{workflow_id}", user_credentials=session.env)
        if "error" in workflow:
            return json.dumps(workflow, indent=2)

        if _is_template(workflow):
            return json.dumps({
                "error": f"Cannot activate/deactivate template workflow '{workflow['name']}'. Clone it first."
            }, indent=2)

        # Placeholder validation – refuse to activate if email nodes still have placeholder addresses
        if activate:
            placeholder_addresses = {"placeholder@change.me", "foruser@change.me"}
            placeholder_nodes = []
            for node in workflow.get("nodes", []):
                params = node.get("parameters", {})
                for field in ("toEmail", "fromEmail", "to", "from"):
                    value = params.get(field, "")
                    if isinstance(value, str) and value.strip().lower() in placeholder_addresses:
                        placeholder_nodes.append(f"Node '{node['name']}' field '{field}' = '{value}'")
            if placeholder_nodes:
                return json.dumps({
                    "error": "Cannot activate workflow – placeholder email addresses detected. Update them first.",
                    "placeholder_nodes": placeholder_nodes
                }, indent=2, ensure_ascii=False)

        action = "activate" if activate else "deactivate"
        result = await call_n8n("POST", f"/workflows/{workflow_id}/{action}", user_credentials=session.env)
        if "error" in result:
            return json.dumps(result, indent=2)

        return json.dumps({
            "success": True,
            "workflow_id": workflow_id,
            "name": workflow.get("name"),
            "active": activate,
            "message": f"Workflow '{workflow.get('name')}' {'activated' if activate else 'deactivated'}."
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def run_n8n_workflow(ctx: Context, workflow_id: str) -> str:
    """
    Run an n8n workflow manually (one-time execution for testing).
    Cannot run [TEMPLATE] workflows.

    Args:
        workflow_id: ID of the workflow to run
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    try:
        # Check template protection
        workflow = await call_n8n("GET", f"/workflows/{workflow_id}", user_credentials=session.env)
        if "error" in workflow:
            return json.dumps(workflow, indent=2)

        if _is_template(workflow):
            return json.dumps({
                "error": f"Cannot run template workflow '{workflow['name']}'. Clone it first."
            }, indent=2)

        result = await call_n8n("POST", f"/workflows/{workflow_id}/execute", user_credentials=session.env)
        if "error" in result:
            return json.dumps(result, indent=2)

        return json.dumps({
            "success": True,
            "workflow_id": workflow_id,
            "name": workflow.get("name"),
            "execution": result,
            "message": f"Workflow '{workflow.get('name')}' executed manually."
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def check_n8n_workflow_health(ctx: Context) -> str:
    """
    Check health of all active (non-template) n8n workflows.
    Returns execution status for each workflow - shows which ones have failed recently.

    If a workflow has failed and 'likely_query_issue' is true, the response includes
    current schema fields from introspection. You MUST use this schema info to:
    1. Use get_n8n_workflow to see the current broken query
    2. Build a corrected query using ONLY the field names from 'current_schema_fields' - NEVER guess
    3. Use get_details_types to inspect specific types if you need deeper field info
    4. Test the new query with graphql() to verify it works
    5. Use update_n8n_node_param to fix the HTTP Request node
    6. Also update the Code node if the data structure changed
    7. Use run_n8n_workflow to verify the full workflow works
    """
    session = await UserSession.from_headers(dict(ctx.request_context.request.headers))  # type: ignore[union-attr]
    try:
        # Get all workflows
        result = await call_n8n("GET", "/workflows", user_credentials=session.env)
        if "error" in result:
            return json.dumps(result, indent=2)

        workflows = result.get("data", result) if isinstance(result, dict) else result
        if isinstance(workflows, dict) and "data" in workflows:
            workflows = workflows["data"]

        # Filter to active non-template workflows
        active_workflows = [
            wf for wf in workflows
            if wf.get("active") and not wf.get("name", "").startswith(TEMPLATE_PREFIX)
        ]

        if not active_workflows:
            return json.dumps({"message": "No active workflows found.", "workflows": []}, indent=2)

        # Check executions for each workflow
        health_report = []
        for wf in active_workflows:
            wf_id = wf.get("id")
            wf_name = wf.get("name")

            # Get recent executions for this workflow
            executions = await call_n8n("GET", f"/executions?workflowId={wf_id}&limit=5", user_credentials=session.env)
            if "error" in executions:
                health_report.append({
                    "workflow_id": wf_id,
                    "name": wf_name,
                    "status": "unknown",
                    "error": "Could not fetch executions"
                })
                continue

            exec_data = executions.get("data", [])
            if not exec_data:
                health_report.append({
                    "workflow_id": wf_id,
                    "name": wf_name,
                    "status": "no_executions",
                    "message": "No executions found yet"
                })
                continue

            # Check all recent executions for failures
            latest = exec_data[0]
            latest_status = latest.get("status", "unknown")
            latest_finished = latest.get("stoppedAt", latest.get("finishedAt", ""))

            failed_executions = [e for e in exec_data if e.get("status") != "success"]
            successful_executions = [e for e in exec_data if e.get("status") == "success"]

            # Workflow is unhealthy if ANY recent execution failed
            is_healthy = len(failed_executions) == 0

            entry = {
                "workflow_id": wf_id,
                "name": wf_name,
                "status": "healthy" if is_healthy else "failed",
                "last_execution": latest_finished,
                "last_status": latest_status,
                "recent_failures": len(failed_executions),
                "recent_successes": len(successful_executions),
            }

            # If any failures, include error details from the most recent failure
            if failed_executions:
                failed_exec = failed_executions[0]
                exec_id = failed_exec.get("id")
                entry["last_failure_time"] = failed_exec.get("stoppedAt", failed_exec.get("finishedAt", ""))
                if exec_id:
                    exec_detail = await call_n8n("GET", f"/executions/{exec_id}", user_credentials=session.env)
                    if "error" not in exec_detail:
                        # Find the node that failed
                        result_data = exec_detail.get("data", {}).get("resultData", {})
                        run_data = result_data.get("runData", {})
                        for node_name, node_runs in run_data.items():
                            if node_runs and isinstance(node_runs, list):
                                last_run = node_runs[-1]
                                if last_run.get("error"):
                                    entry["failed_node"] = node_name
                                    entry["error_message"] = last_run["error"].get("message", str(last_run["error"]))
                                    break

            # If there are failures, run introspection to provide schema context for fixing
            failed_node = entry.get("failed_node", "")
            if failed_executions:
                entry["likely_query_issue"] = True
                entry["recommendation"] = "GraphQL query is likely broken. Schema info included below - use it to build a corrected query."

                # Run introspection to get current schema
                try:
                    introspection_query = """
                    query GetQueryTypes {
                        __type(name: "Query") {
                            name
                            fields {
                                name
                                type {
                                    name
                                    kind
                                    ofType { name kind }
                                }
                            }
                        }
                    }
                    """
                    schema_result = await call_boligflow(introspection_query, user_credentials=session.env)
                    if "errors" not in schema_result:
                        fields = schema_result.get("data", {}).get("__type", {}).get("fields", [])
                        entry["current_schema_fields"] = [
                            {"name": f["name"], "type": f["type"].get("name") or f["type"].get("ofType", {}).get("name", "unknown")}
                            for f in fields
                        ]
                except Exception:
                    entry["schema_note"] = "Could not fetch schema - use get_types and get_details_types manually"

            health_report.append(entry)

        # Summary
        failed = [w for w in health_report if w.get("status") == "failed"]
        healthy = [w for w in health_report if w.get("status") == "healthy"]

        return json.dumps({
            "summary": f"{len(healthy)} healthy, {len(failed)} failed out of {len(health_report)} active workflows",
            "workflows": health_report
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


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
    port = int(os.getenv("PORT", "3001"))
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port
        )