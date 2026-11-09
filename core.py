"""env-setup core: shared request registry, catalog, and .env writer.

Used by BOTH halves of the plugin, which are imported through different
mechanisms (the agent plugin loader imports the package, the dashboard
backend mounts ``dashboard/plugin_api.py`` by file location). To guarantee a
single shared state no matter which import path ran first, the registry
singleton is stashed in ``sys.modules`` under a fixed key.

Security invariant (the whole point of this plugin): values NEVER leave this
module towards the model. Every function that returns request or catalog data
returns field NAMES and set/unset flags only.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger("hermes.plugins.env-setup")

PLUGIN_ID = "env-setup"

# ENV var names must look like identifiers: no injection into .env format
# (no "=", no newline, no lowercase, bounded length).
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

MAX_FIELDS = 12
MAX_REQUESTS = 50
MAX_AGE_SECONDS = 24 * 3600
_SINGLETON_KEY = "hermes_env_setup_shared_registry"
LATE_RESOLUTION_WINDOW_S = 15 * 60  # a resolve the agent never saw stays queryable 15 min


# ---------------------------------------------------------------------------
# Environment helpers (reuse Hermes' canonical .env path handling)
# ---------------------------------------------------------------------------

def _save_env_value(name: str, value: str) -> None:
    """Write KEY=VALUE into the active profile's .env (atomic, canonical path)."""
    from hermes_cli.config import save_env_value
    save_env_value(name, value)


def _get_env_value(name: str) -> str | None:
    from hermes_cli.config import get_env_value
    return get_env_value(name)


# YAML for the optional plugin-credential discovery. This must FAIL SOFT:
# tool contexts exist that ship neither hermes_yaml's ruamel nor PyYAML
# (fresh-session one-shots after a PM env switch crashed here with
# ModuleNotFoundError). The core flow (prompt/status/variables) needs no YAML
# at all — only _dynamic_catalog() does, and it degrades to an empty list.
def _yaml_loader():
    for module in ("hermes_yaml", "yaml"):
        try:
            imported = __import__(module)
            return imported.safe_load
        except Exception:
            continue
    return None


_YAML_SAFE_LOAD = _yaml_loader()


def _plugins_root() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "plugins"
    except Exception:  # pragma: no cover - defensive
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home) / "plugins"


def _broadcast(event: str, payload: dict | None) -> None:
    """Push ``plugin.env-setup.<event>`` to every connected desktop client."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_ID, event, payload or {})
    except Exception:
        # Fire-and-forget; headless sessions (CLI, cron) have no transports.
        logger.debug("env-setup: event broadcast '%s' skipped", event, exc_info=True)


# ---------------------------------------------------------------------------
# Field normalization / validation
# ---------------------------------------------------------------------------

def _norm_text(value, cap: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:cap]


def _valid_env_name(name) -> bool:
    return isinstance(name, str) and bool(_ENV_NAME_RE.match(name))


def _normalize_field(raw: dict, existing_names: set) -> tuple[dict | None, str | None]:
    """Normalize one incoming field spec. Returns (field, error)."""
    if not isinstance(raw, dict):
        return None, "each field must be an object"
    name = raw.get("name")
    if not _valid_env_name(name):
        return None, (
            f"invalid field name {name!r}: must match ^[A-Z][A-Z0-9_]{{1,63}}$ "
            "(uppercase ENV_VAR style)")
    if name in existing_names:
        return None, f"duplicate field name {name!r}"
    existing_names.add(name)
    field = {
        "name": name,
        "label": _norm_text(raw.get("label") or name, 60) or name,
        "description": _norm_text(raw.get("description"), 240),
        "example": _norm_text(raw.get("example"), 80),
        "url": _norm_text(raw.get("url"), 200),
        "secret": bool(raw.get("secret", True)),  # default masked: safer
        "required": bool(raw.get("required", False)),
        "set": bool(_get_env_value(name)),
    }
    return field, None


def _catalog_field(spec: dict) -> dict:
    """Field dict from plugin.yaml requires_env declarations, plus live set flag."""
    name = spec.get("name")
    return {
        "name": name,
        "label": _norm_text(spec.get("label") or spec.get("prompt") or name, 60) or name,
        "description": _norm_text(spec.get("description"), 240),
        "example": _norm_text(spec.get("example"), 80),
        "url": _norm_text(spec.get("url"), 200),
        "secret": bool(spec.get("secret", spec.get("password", True))),
        "required": bool(spec.get("required", True)),
        "set": bool(_get_env_value(name)),
    }


# ---------------------------------------------------------------------------
# Value writing (shared by request-resolve and the direct /configure flow)
# ---------------------------------------------------------------------------

def write_values(values: dict) -> dict:
    """Write KEY=VALUE pairs to .env. Returns {configured, skipped, errors}.

    Caller validates names belong to an allowed set; this function only
    refuses empty values (→ skipped) and collects write errors.
    """
    configured: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for name, value in values.items():
        value = value if isinstance(value, str) else str(value)
        if not value.strip():
            skipped.append(name)
            continue
        try:
            _save_env_value(name, value)
            # Mirror the installer behaviour: also expose it to the running
            # process so adapters pick it up without a restart.
            os.environ[name] = value
            configured.append(name)
        except Exception as exc:  # .env write failed — report, continue
            logger.exception("env-setup: failed writing %s", name)
            errors.append(f"{name}: {exc}")
    return {"configured": configured, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Request registry (process-global singleton)
# ---------------------------------------------------------------------------

class _Request:
    __slots__ = ("id", "title", "reason", "fields", "status", "created",
                 "resolved_at", "configured", "skipped", "consumed", "waiter")

    def __init__(self, title: str, reason: str, fields: list[dict]):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.reason = reason
        self.fields = fields  # list of normalized field dicts (no values!)
        self.status = "open"  # open | resolved | cancelled | superseded
        self.created = time.time()
        self.resolved_at: float | None = None
        self.configured: list[str] = []
        self.skipped: list[str] = []
        self.consumed = False
        self.waiter = threading.Event()

    def public(self) -> dict:
        """Serializable projection — field metadata only, never values."""
        return {
            "id": self.id,
            "title": self.title,
            "reason": self.reason,
            "fields": self.fields,
            "status": self.status,
            "created": self.created,
            "resolved_at": self.resolved_at,
            "configured": list(self.configured),
            "skipped": list(self.skipped),
            "consumed": self.consumed,
        }


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, _Request] = {}

    # -- internal maintenance ------------------------------------------------

    def _prune_locked(self) -> None:
        now = time.time()
        stale = [rid for rid, r in self._requests.items()
                 if now - (r.resolved_at or r.created) > MAX_AGE_SECONDS]
        for rid in stale:
            del self._requests[rid]
        if len(self._requests) > MAX_REQUESTS:
            ordered = sorted(self._requests.values(), key=lambda r: r.created)
            for r in ordered[: len(self._requests) - MAX_REQUESTS]:
                del self._requests[r.id]

    # -- create ---------------------------------------------------------------

    def create(self, fields: list[dict], title: str, reason: str) -> dict:
        """Validate and register a new request. Returns the public dict or {'error': ...}."""
        if not isinstance(fields, list) or not fields:
            return {"error": "fields must be a non-empty list"}
        if len(fields) > MAX_FIELDS:
            return {"error": f"too many fields: {len(fields)} (max {MAX_FIELDS})"}
        seen: set = set()
        normalized: list[dict] = []
        for raw in fields:
            field, err = _normalize_field(raw, seen)
            if err:
                return {"error": err}
            normalized.append(field)
        req = _Request(
            _norm_text(title, 80) or "Zugänge einrichten",
            _norm_text(reason, 300),
            normalized)
        with self._lock:
            self._prune_locked()
            # Supersede any still-open request: one dialog at a time.
            for other in self._requests.values():
                if other.status == "open":
                    other.status = "superseded"
                    other.waiter.set()
            self._requests[req.id] = req
        _broadcast("request.created", {"request": req.public()})
        return {"request": req.public()}

    # -- resolve / cancel -----------------------------------------------------

    def resolve(self, rid: str, values: dict, skipped: list | None = None) -> dict:
        with self._lock:
            req = self._requests.get(rid)
            if req is None or req.status != "open":
                return {"error": f"no open request {rid!r}"}
            if not isinstance(values, dict):
                return {"error": "values must be an object"}
            allowed = {f["name"] for f in req.fields}
            for key in values:
                if key not in allowed:
                    return {"error": f"field {key!r} was not part of request {rid!r}"}
            written = write_values(values)
            req.configured = written["configured"]
            req.skipped = written["skipped"]
            for name in (skipped or []):
                if name in allowed and name not in req.configured and name not in req.skipped:
                    req.skipped.append(name)
            req.status = "resolved"
            req.resolved_at = time.time()
            req.waiter.set()
        _broadcast("request.resolved", {"id": rid, "configured": req.configured,
                                        "skipped": req.skipped})
        out = {"configured": req.configured, "skipped": req.skipped}
        if written["errors"]:
            out["errors"] = written["errors"]
        return out

    def cancel(self, rid: str) -> dict:
        with self._lock:
            req = self._requests.get(rid)
            if req is None or req.status != "open":
                return {"error": f"no open request {rid!r}"}
            req.status = "cancelled"
            req.resolved_at = time.time()
            req.waiter.set()
        _broadcast("request.cancelled", {"id": rid})
        return {"cancelled": rid}

    # -- queries ----------------------------------------------------------------

    def get(self, rid: str) -> dict | None:
        with self._lock:
            req = self._requests.get(rid)
            return req.public() if req else None

    def snapshot(self, include_resolved: bool = True) -> list[dict]:
        with self._lock:
            out = []
            for req in sorted(self._requests.values(), key=lambda r: r.created):
                if req.status == "open" or (include_resolved and not req.consumed):
                    out.append(req.public())
            return out

    def mark_consumed(self, rid: str) -> None:
        with self._lock:
            req = self._requests.get(rid)
            if req is not None:
                req.consumed = True


def get_registry() -> _Registry:
    """Process-global singleton shared across both import paths."""
    reg = sys.modules.get(_SINGLETON_KEY)
    if reg is None:
        reg = _Registry()
        sys.modules[_SINGLETON_KEY] = reg
    return reg


# ---------------------------------------------------------------------------
# Catalog: credentials declared by installed plugins (dynamic only)
# ---------------------------------------------------------------------------
# No hardcoded provider list by design — the plugin is a minimal on-demand
# channel, not a catalog. Anything that needs credentials declares them in its
# plugin.yaml (requires_env / optional_env) and shows up here automatically.

def catalog() -> dict:
    """Plugin-declared credentials with live set/unset flags. NEVER values."""
    return {"integrations": _dynamic_catalog()}


def check_variables(names) -> dict | str:
    """Check specific ENV variable names for set/unset — values never returned."""
    if not isinstance(names, list) or not names:
        return "variables must be a non-empty list of ENV names"
    if len(names) > 24:
        return "too many variables (max 24)"
    out = []
    for name in names:
        if not _valid_env_name(name):
            return f"invalid variable name {name!r}"
        out.append({"name": name, "set": bool(_get_env_value(name))})
    return {"variables": out}


def _dynamic_catalog() -> list[dict]:
    """requires_env / optional_env declared by installed plugins -> integrations."""
    out: list[dict] = []
    if _YAML_SAFE_LOAD is None:
        logger.warning("env-setup: no YAML parser available (neither hermes_yaml "
                       "nor PyYAML) — plugin-credential discovery disabled, "
                       "core flow unaffected")
        return out
    root = _plugins_root()
    if not root.is_dir():
        return out

    def _specs(raw: list) -> list[dict]:
        specs = []
        for entry in raw or []:
            if isinstance(entry, str):
                specs.append({"name": entry})
            elif isinstance(entry, dict) and entry.get("name"):
                specs.append(entry)
        return specs

    for plugin_dir in sorted(root.iterdir()):
        manifest = plugin_dir / "plugin.yaml"
        if not manifest.is_file() or plugin_dir.name == PLUGIN_ID:
            continue
        try:
            data = _YAML_SAFE_LOAD(manifest.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        required = _specs(data.get("requires_env"))
        optional = _specs(data.get("optional_env"))
        if not required and not optional:
            continue
        fields = [_catalog_field(s) for s in required + optional]
        if not fields:
            continue
        out.append({
            "id": f"plugin:{plugin_dir.name}",
            "label": data.get("label") or plugin_dir.name,
            "description": f"Zugänge für das installierte Plugin „{plugin_dir.name}“.",
            "source": "plugin",
            "plugin": plugin_dir.name,
            "fields": fields,
        })
    return out


def status_snapshot() -> dict:
    reg = get_registry()
    now = time.time()
    late = [r for r in reg.snapshot()
            if r["status"] == "resolved" and not r["consumed"]
            and r["resolved_at"] and now - r["resolved_at"] <= LATE_RESOLUTION_WINDOW_S]
    return {
        "integrations": catalog()["integrations"],
        "open_requests": [r for r in reg.snapshot() if r["status"] == "open"],
        "recently_resolved": late,
    }