"""Agent tools for the env-setup plugin.

``env_setup_prompt`` — request credentials by NAME; a masked desktop form is
shown to the user; values go straight to .env. The tool result NEVER contains
values, only which variables were configured or skipped.

``env_setup_status`` — check which catalog/plugin variables are set or missing,
plus any request the user resolved after a timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from . import core

logger = logging.getLogger("hermes.plugins.env-setup")

TOOLSET = "env_setup"

DEFAULT_WAIT_S = 150
MAX_WAIT_S = 900


def _result(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# env_setup_prompt
# ---------------------------------------------------------------------------

PROMPT_SCHEMA = {
    "name": "env_setup_prompt",
    "description": (
        "Ask the user to provide credential values (API keys, tokens, passwords) "
        "for environment variables via a masked form in the desktop app. Values "
        "are written DIRECTLY to .env and NEVER returned to you and never "
        "appear in the chat. NEVER ask the user to paste secrets into the chat; "
        "use this tool instead. The result tells you which variables were "
        "configured and which the user skipped, then continue the setup."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fields": {
                "type": ["array", "string"],
                "description": (
                    "Environment variables to request (max 12). Pass EITHER a "
                    "JSON array of objects or the same array as a JSON-encoded "
                    "string. Each entry: name (REQUIRED, uppercase ENV_VAR), "
                    "label, description, example, url, secret (true -> masked "
                    "input), required."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                        "example": {"type": "string"},
                        "url": {"type": "string"},
                        "secret": {"type": "boolean"},
                        "required": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            },
            "title": {
                "type": "string",
                "description": "Short form title the user sees, e.g. 'E-Mail einrichten'.",
            },
            "reason": {
                "type": "string",
                "description": "One sentence why you need these, shown under the title.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    f"How long to wait for the user to submit (default {DEFAULT_WAIT_S}s, "
                    f"max {MAX_WAIT_S}s). The request stays open afterwards."
                ),
            },
        },
        "required": ["fields"],
    },
}


async def _prompt_handler(args: dict) -> str:
    reg = core.get_registry()
    fields = (args or {}).get("fields")
    # Models with weak nested-array tool calling may pass the array as a
    # JSON-encoded STRING — accept both shapes (one string is what every
    # model can build).
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except (json.JSONDecodeError, ValueError):
            return _result({
                "error": "'fields' must be a JSON array (or a JSON-encoded string of it). "
                         "Example: [{\"name\": \"MY_API_KEY\", \"label\": \"API Key\", \"secret\": true}]"
            })
    created = reg.create(
        fields=fields or [],
        title=(args or {}).get("title") or "Zugänge einrichten",
        reason=(args or {}).get("reason") or "")
    if "error" in created:
        return _result({"error": created["error"]})

    req = created["request"]
    rid = req["id"]
    try:
        wait_s = int((args or {}).get("timeout_seconds") or DEFAULT_WAIT_S)
    except (TypeError, ValueError):
        wait_s = DEFAULT_WAIT_S
    wait_s = max(10, min(wait_s, MAX_WAIT_S))

    deadline = time.monotonic() + wait_s
    status = "open"
    while time.monotonic() < deadline:
        snapshot = reg.get(rid)
        status = snapshot["status"] if snapshot else "superseded"
        if status != "open":
            break
        await asyncio.sleep(1.5)

    snapshot = reg.get(rid) or {}
    reg.mark_consumed(rid)

    if status == "resolved":
        return _result({
            "status": "resolved",
            "configured": snapshot.get("configured", []),
            "skipped": snapshot.get("skipped", []),
            "note": ("Values were saved directly to .env and are live in the "
                     "process — continue the setup without ever seeing them."),
        })
    if status == "cancelled":
        return _result({
            "status": "cancelled",
            "note": "The user closed the form. Ask whether they want to set the values manually.",
        })
    if status == "superseded":
        return _result({
            "status": "superseded",
            "note": "A newer request replaced this one.",
        })
    return _result({
        "status": "timeout",
        "note": (
            "Nobody submitted the form in time. It stays available in the "
            "desktop app (page 'Setups' / statusbar key icon). Call "
            "env_setup_status later to check for a late completion."
        ),
    })


# ---------------------------------------------------------------------------
# env_setup_status
# ---------------------------------------------------------------------------

STATUS_SCHEMA = {
    "name": "env_setup_status",
    "description": (
        "Check credential/configuration status WITHOUT exposing values. Either "
        "pass 'variables' with specific ENV names to check (returns set/unset "
        "flags for exactly those), or omit it to list missing credentials "
        "declared by installed plugins (their requires_env) plus any setup "
        "form the user completed after an env_setup_prompt timeout. Use before "
        "starting a setup to see what is missing, or after a timeout to pick "
        "up late completions. Never returns values."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "variables": {
                "type": "array",
                "description": (
                    "Specific ENV variable names to check, e.g. "
                    "['OPENAI_API_KEY', 'EMAIL_PASSWORD']. Max 24."
                ),
                "items": {"type": "string"},
            },
        },
        "required": [],
    },
}


async def _status_handler(args: dict) -> str:
    reg = core.get_registry()
    snap = core.status_snapshot()
    for late in snap["recently_resolved"]:
        # The agent now sees the outcome — mark it consumed so it stops resurfacing.
        reg.mark_consumed(late["id"])

    requested = (args or {}).get("variables")
    checked = None
    if requested:
        result = core.check_variables(requested)
        if isinstance(result, str):
            return _result({"error": result})
        checked = result["variables"]

    compact = {
        "checked_variables": checked,
        "plugin_integrations": [
            {
                "id": i["id"],
                "label": i["label"],
                "missing": [f["name"] for f in i["fields"] if f["required"] and not f["set"]],
                "complete": all(f["set"] for f in i["fields"] if f["required"]),
            }
            for i in snap["integrations"]
        ],
        "open_requests": [
            {"id": r["id"], "title": r["title"]} for r in snap["open_requests"]
        ],
        "recently_resolved": [
            {"id": r["id"], "configured": r["configured"], "skipped": r["skipped"]}
            for r in snap["recently_resolved"]
        ],
    }
    return _result(compact)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def register_tools(ctx) -> None:
    """Register the env-setup tools via the public plugin API."""
    for schema, handler in ((PROMPT_SCHEMA, _prompt_handler), (STATUS_SCHEMA, _status_handler)):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            is_async=True,
            emoji="🔐",
        )
        logger.debug("env-setup: registered tool %s", schema["name"])