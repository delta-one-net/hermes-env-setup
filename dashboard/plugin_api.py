"""Dashboard/backend routes for the env-setup plugin.

Mounted by the gateway at /api/plugins/env-setup/ when the plugin is in
``plugins.enabled``. Runs inside the gateway process, so it shares the
process-global request registry with the agent tools (core.py is imported by
file location here — the dashboard loader mounts this file without package
context — and the singleton key in core guarantees shared state anyway).

Minimal by design: only the chat-driven request flow. No catalog, no
provider presets — credentials are requested by the agent when the chat
needs them, nothing else.

Every route returns field metadata and set/unset flags only. Values arrive
via POST bodies and are written straight to .env; they are never echoed back.
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib

from fastapi import APIRouter

logger = logging.getLogger("hermes.plugins.env-setup.api")

router = APIRouter()


def _load_core():
    """Import ../core.py by location (no package context here)."""
    core_path = pathlib.Path(__file__).resolve().parents[1] / "core.py"
    spec = importlib.util.spec_from_file_location("hermes_env_setup_core", core_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_core = _load_core()


# ---------------------------------------------------------------------------
# the chat-driven ask flow
# ---------------------------------------------------------------------------

@router.get("/requests")
async def get_requests(profile: str | None = None):
    """Open setup requests plus recently resolved ones the agent may not have seen.

    ?profile=<name> scopes the view to requests created by that profile's
    agents — a profile's desktop view never sees another profile's
    credential requests (the request carries its owner profile stamp).
    Without the parameter all requests are returned (fallback/compat).
    """
    reg = _core.get_registry()
    return {"requests": reg.snapshot(profile=profile)}


@router.post("/requests/{rid}/resolve")
async def resolve_request(rid: str, body: dict):
    """User submitted the masked form: write values to .env, never echo them."""
    reg = _core.get_registry()
    result = reg.resolve(rid, (body or {}).get("values") or {}, (body or {}).get("skipped") or [])
    if "error" in result:
        return {"ok": False, "error": result["error"]}
    return {"ok": True, **result}


@router.post("/requests/{rid}/cancel")
async def cancel_request(rid: str):
    """User cancelled the form: ends the ask flow, the waiting tool gets 'cancelled'."""
    reg = _core.get_registry()
    result = reg.cancel(rid)
    if "error" in result:
        return {"ok": False, "error": result["error"]}
    return {"ok": True, **result}