"""Offline unit tests for env-setup core.py.

Run with the hermes interpreter + the PM venv + the repo on sys.path, e.g.:

    PYTHONPATH="$HOME/.hermes/installs/<hash>/environments/<hash>/venv/lib/python3.14/site-packages:$HOME/.hermes/hermes-agent" \
      ~/.hermes/tools/python-3.14*/bin/python3 tests/test_core.py

(The exact venv hash dirs are visible in the environ of a running gateway
process: `cat /proc/$(pgrep -f hermes_kernel | head -1)/environ | tr '\\0' '\\n' | grep PYTHONPATH`.)

Tests 5 and 10 perform REAL .env writes with ENV_SETUP_TEST_* variables and
clean them up again (pre-cleanup at start makes the suite idempotent).
"""

import importlib.util
import json
import os
import sys
import pathlib

CORE = pathlib.Path(__file__).resolve().parents[1] / "core.py"
API = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    core = load(CORE, "core_a")
    core2 = load(CORE, "core_b")

    # pre-cleanup: strip any ENV_SETUP_TEST_* leftovers from a previous failed run
    from hermes_constants import get_hermes_home
    env_path = get_hermes_home() / ".env"
    lines = env_path.read_text().splitlines(keepends=True)
    kept = [l for l in lines if not l.startswith("ENV_SETUP_TEST_")]
    if len(kept) != len(lines):
        env_path.write_text("".join(kept))

    # 1. singleton across import paths
    assert core.get_registry() is core2.get_registry(), "singleton broken"
    print("1. singleton shared across import paths: OK")
    reg = core.get_registry()

    # 2. invalid field names rejected (injection protection)
    for bad in ("bad-name", "X=Y\nEVIL=1", "lower_case", "", "A" * 70, "A B"):
        r = reg.create(fields=[{"name": bad}], title="t", reason="")
        assert "error" in r, f"accepted invalid name {bad!r}"
    assert "error" in reg.create(fields=[], title="t", reason="")
    assert "error" in reg.create(fields=[{"name": "OK_NAME"}] * 2, title="t", reason="")
    print("2. env-name validation rejects injection/dupes: OK")

    # 3. valid request
    r = reg.create(fields=[
        {"name": "ENV_SETUP_TEST_HOST", "label": "Host", "secret": False, "required": True},
        {"name": "ENV_SETUP_TEST_PASS", "label": "Pass", "secret": True, "required": True},
    ], title="Selftest", reason="unit test")
    assert "request" in r, r
    rid = r["request"]["id"]
    assert r["request"]["fields"][0]["set"] is False
    assert r["request"]["fields"][1]["secret"] is True  # masked by default
    print("3. request created:", rid)

    # 4. foreign key rejected
    assert "error" in reg.resolve(rid, {"OTHER_VAR": "x"})
    print("4. foreign field in resolve rejected: OK")

    # 5. real resolve writes .env (empty value -> skipped, not written)
    out = reg.resolve(rid, {"ENV_SETUP_TEST_HOST": "imap.example.test", "ENV_SETUP_TEST_PASS": "   "})
    assert out["configured"] == ["ENV_SETUP_TEST_HOST"], out
    assert out["skipped"] == ["ENV_SETUP_TEST_PASS"], out
    from hermes_cli.config import get_env_value
    assert get_env_value("ENV_SETUP_TEST_HOST") == "imap.example.test"
    print("5. resolve wrote real .env value, skipped empty: OK")

    # 6. catalog: dynamic plugin discovery only (no static presets), no value leakage
    cat = core.catalog()
    ids = [i["id"] for i in cat["integrations"]]
    assert "plugin:homeassistant" in ids, ids  # requires_env discovered dynamically
    assert all(i["id"].startswith("plugin:") for i in cat["integrations"]), ids
    blob = json.dumps(cat)
    assert "imap.example.test" not in blob, "value leaked into catalog!"
    print("6. catalog: dynamic plugin discovery only, no value leak: OK")

    # 6b. check_variables: targeted set/unset flags (values never returned)
    out = core.check_variables(["ENV_SETUP_TEST_HOST", "ENV_SETUP_TEST_PASS"])
    assert out["variables"] == [
        {"name": "ENV_SETUP_TEST_HOST", "set": True},
        {"name": "ENV_SETUP_TEST_PASS", "set": False},
    ], out
    assert isinstance(core.check_variables(["bad-name"]), str)  # invalid -> error
    assert isinstance(core.check_variables([]), str)  # empty -> error
    print("6b. check_variables targeted flags: OK")

    # 7. status snapshot: dynamic-only + request state present
    snap = core.status_snapshot()
    assert any(i["id"] == "plugin:homeassistant" for i in snap["integrations"])
    assert "open_requests" in snap and "recently_resolved" in snap
    print("7. status snapshot: OK")

    # 8. supersede: an OPEN request gets closed when a newer one arrives
    rx = reg.create(fields=[{"name": "ENV_SETUP_TEST_B"}], title="open", reason="")["request"]
    rid2 = reg.create(fields=[{"name": "ENV_SETUP_TEST_A"}], title="2", reason="")["request"]["id"]
    assert reg.get(rx["id"])["status"] == "superseded", reg.get(rx["id"])
    print("8. new request supersedes old: OK")

    # 9. cancel works exactly once
    assert "cancelled" in reg.cancel(rid2)
    assert "error" in reg.cancel(rid2)
    print("9. cancel works exactly once: OK")

    # 10. write_values: direct configure flow
    w = core.write_values({"ENV_SETUP_TEST_DIRECT": "dv", "ENV_SETUP_TEST_EMPTY": ""})
    assert w["configured"] == ["ENV_SETUP_TEST_DIRECT"]
    assert w["skipped"] == ["ENV_SETUP_TEST_EMPTY"]
    print("10. write_values direct flow: OK")

    # 11. dashboard backend router (minimal: requests flow only)
    pa = load(API, "pa")
    routes = sorted(getattr(r, "path", "?") for r in pa.router.routes)
    assert routes == ["/requests", "/requests/{rid}/cancel", "/requests/{rid}/resolve"], routes
    print("11. backend routes:", routes)

    # cleanup: strip ENV_SETUP_TEST_* lines from .env, reset os.environ
    lines = env_path.read_text().splitlines(keepends=True)
    env_path.write_text("".join(l for l in lines if not l.startswith("ENV_SETUP_TEST_")))
    for k in ("ENV_SETUP_TEST_HOST", "ENV_SETUP_TEST_DIRECT", "ENV_SETUP_TEST_PASS"):
        os.environ.pop(k, None)
    remaining = [l for l in env_path.read_text().splitlines() if l.startswith("ENV_SETUP_TEST_")]
    assert not remaining, remaining
    print("cleanup: .env restored (no ENV_SETUP_TEST_* left)")

    print("\nALL 11 CORE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ── profile binding (v0.3.4): requests are stamped with the owning profile ──

def test_request_carries_profile_stamp(monkeypatch, tmp_path):
    reg = _Registry()
    athene_home = tmp_path / "profiles" / "athene"
    monkeypatch.setattr(core, "_hermes_home", lambda: athene_home)
    out = reg.create([{"name": "MY_KEY", "label": "Key"}], "T", "r")
    assert out["request"]["profile"] == "athene"

def test_request_default_profile_outside_profiles_dir(monkeypatch, tmp_path):
    reg = _Registry()
    monkeypatch.setattr(core, "_hermes_home", lambda: tmp_path)
    out = reg.create([{"name": "MY_KEY"}], "T", "")
    assert out["request"]["profile"] == "default"

def test_resolve_writes_profile_env_not_caller_env(monkeypatch, tmp_path):
    # gateway/backend context: default home; request owned by 'athene'
    root = tmp_path
    monkeypatch.setattr(core, "_hermes_home", lambda: root)
    reg = _Registry()
    out = reg.create([{"name": "MY_KEY", "secret": True}], "T", "")
    assert out["request"]["profile"] == "default"  # created in default context
    # simulate a profile-owned request: create under athene home
    monkeypatch.setattr(core, "_hermes_home", lambda: root / "profiles" / "athene")
    out2 = reg.create([{"name": "ATHENE_KEY", "secret": True}], "T2", "")
    rid = out2["request"]["id"]
    assert out2["request"]["profile"] == "athene"
    # resolver runs in the GATEWAY (default home) — must still write athene's env
    monkeypatch.setattr(core, "_hermes_home", lambda: root)
    import os
    os.environ.pop("ATHENE_KEY", None)
    res = reg.resolve(rid, {"ATHENE_KEY": "sekrit"})
    assert res["configured"] == ["ATHENE_KEY"]
    assert (root / "profiles" / "athene" / ".env").read_text().find("ATHENE_KEY=sekrit") >= 0
    assert not (root / ".env").exists() or "ATHENE_KEY" not in (root / ".env").read_text()
    assert os.environ.get("ATHENE_KEY") is None  # gateway env stays clean

def test_snapshot_filters_foreign_profiles(monkeypatch, tmp_path):
    reg = _Registry()
    monkeypatch.setattr(core, "_hermes_home", lambda: tmp_path / "profiles" / "athene")
    r = reg.create([{"name": "A_KEY"}], "A", "")
    monkeypatch.setattr(core, "_hermes_home", lambda: tmp_path)
    reg.create([{"name": "D_KEY"}], "D", "")
    only_athene = [x for x in reg.snapshot(profile="athene")]
    assert [x["title"] for x in only_athene] == ["A"]
    only_default = [x for x in reg.snapshot(profile="default")]
    assert [x["title"] for x in only_default] == ["D"]
    assert len(reg.snapshot()) == 2  # unfiltered view keeps everything
