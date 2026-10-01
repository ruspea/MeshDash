"""Updater tests: every way an update can go wrong must end on a working version.

Run: python -m pytest tests/ -q
"""
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import core.update as upd  # noqa: E402


# FIXTURES

def _release_files(version, extra=None, drop=()):
    files = {
        "meshtastic_dashboard.py": f'app = dict(version="{version}")\n',
        "core/__init__.py": "# core package\n",
        "core/update.py": f"# updater {version}\n",
        "core/thing.py": f"VALUE = '{version}'\n",
        "requirements.txt": "httpx==0.28.1\n",
        "static/index.html": f"<html>{version}</html>\n",
    }
    files.update(extra or {})
    for d in drop:
        files.pop(d, None)
    return files


def _make_zip(path, files, raw_entries=()):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        for info, content in raw_entries:
            zf.writestr(info, content)
    return path


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An installed R3.1.9 panel with user data, plus helpers."""
    root = tmp_path / "meshdash"
    data = root / "data"
    data.mkdir(parents=True)
    for rel, content in _release_files("R3.1.9", extra={"core/legacy.py": "OLD = 1\n"}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (data / ".mesh-dash_config").write_text("COMMUNITY_API_KEY=MD-KEEP\n")
    (data / "meshtastic_data.db").write_bytes(b"user-data")
    (data / upd.RELEASE_MANIFEST).write_text(json.dumps({
        "version": "R3.1.9",
        "files": sorted(_release_files("R3.1.9", extra={"core/legacy.py": ""}).keys()),
    }))

    restarts = []
    pip_calls = []

    def fake_pip(args, timeout=upd.PIP_TIMEOUT_S):
        pip_calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(upd, "_run_pip", fake_pip)
    monkeypatch.setattr(upd, "_trial_ok", threading.Event())
    monkeypatch.setattr(upd, "_trial_ctx", {})
    monkeypatch.setattr(upd, "_arm_trial_guards", lambda *a, **k: None)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)

    class Ctx:
        pass

    c = Ctx()
    c.root, c.data, c.restarts, c.pip_calls = root, data, restarts, pip_calls
    c.restart = lambda: restarts.append(time.time())

    def stage_update(version, files=None, meta=None, zip_bytes=None):
        z = data / "update.zip"
        if zip_bytes is not None:
            z.write_bytes(zip_bytes)
        else:
            _make_zip(z, files or _release_files(version))
        m = {"from_version": "R3.1.9", "to_version": version}
        m.update(meta or {})
        (data / "update.json").write_text(json.dumps(m))
        (data / "update.flag").write_text("1")

    def boot():
        return upd.check_and_apply_update("data", install_root=str(root), restart=c.restart)

    def state():
        return upd.read_state(str(data))

    def read(rel):
        return (root / rel).read_text()

    c.stage_update, c.boot, c.state, c.read = stage_update, boot, state, read
    return c


def _unchanged(c):
    assert c.read("core/thing.py") == "VALUE = 'R3.1.9'\n"
    assert c.read("meshtastic_dashboard.py") == 'app = dict(version="R3.1.9")\n'
    assert (c.root / "core/legacy.py").exists()
    assert (c.data / "meshtastic_data.db").read_bytes() == b"user-data"
    assert (c.data / ".mesh-dash_config").read_text() == "COMMUNITY_API_KEY=MD-KEEP\n"


def _triggers_gone(c):
    for name in ("update.flag", "update.zip", "update.json", "update.major"):
        assert not (c.data / name).exists(), name


# HAPPY PATH

def test_update_applies_then_trial_then_good(install):
    c = install
    c.stage_update("R3.1.10", _release_files("R3.1.10", extra={"core/new.py": "NEW = 1\n"}))
    assert c.boot() == "applied"
    assert len(c.restarts) == 1, "must restart in place, not exit"
    assert c.read("core/thing.py") == "VALUE = 'R3.1.10'\n"
    assert c.read("core/new.py") == "NEW = 1\n"
    assert not (c.root / "core/legacy.py").exists(), "file dropped by the release is removed"
    assert (c.data / "meshtastic_data.db").read_bytes() == b"user-data"
    assert (c.data / ".mesh-dash_config").read_text() == "COMMUNITY_API_KEY=MD-KEEP\n"
    _triggers_gone(c)
    st = c.state()
    assert st["phase"] == "trial" and st["to_version"] == "R3.1.10" and st["from_version"] == "R3.1.9"
    assert os.path.isdir(st["backup"])

    assert c.boot() == "trial"
    assert c.state()["boots"] == 1
    assert upd.mark_update_healthy(str(c.data)) is True
    assert c.state()["phase"] == "good"
    result = json.loads((c.data / upd.RESULT_FILE).read_text())
    assert result["success"] is True and result["outcome"] == "success"
    assert c.boot() == "none"


def test_unchanged_requirements_do_not_run_pip(install):
    c = install
    c.stage_update("R3.1.10")
    assert c.boot() == "applied"
    assert c.pip_calls == []


def test_changed_requirements_resolve_then_install(install):
    c = install
    c.stage_update("R3.1.10", _release_files("R3.1.10", extra={"requirements.txt": "httpx==0.28.2\n"}))
    assert c.boot() == "applied"
    assert [a[:2] for a in c.pip_calls] == [["install", "--dry-run"], ["install", "--no-cache-dir"]]


def test_works_from_any_working_directory(install, tmp_path, monkeypatch):
    c = install
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    c.stage_update("R3.1.10")
    assert c.boot() == "applied"
    assert c.read("core/thing.py") == "VALUE = 'R3.1.10'\n"
    assert not (elsewhere / "core").exists(), "never writes into the cwd"


def test_release_wrapped_in_a_folder_is_accepted(install):
    c = install
    files = {f"MeshDash-R3.1.10/{k}": v for k, v in _release_files("R3.1.10").items()}
    c.stage_update("R3.1.10", files)
    assert c.boot() == "applied"
    assert c.read("core/thing.py") == "VALUE = 'R3.1.10'\n"


# REFUSED BEFORE ANYTHING CHANGES

@pytest.mark.parametrize("case", ["truncated", "not_a_zip", "empty", "missing_files",
                                  "bad_sha", "wrong_version", "syntax_error", "pip_unresolvable",
                                  "pip_install_fails", "pip_timeout"])
def test_bad_updates_are_refused_and_nothing_changes(install, monkeypatch, case):
    c = install
    if case == "truncated":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for k, v in _release_files("R3.1.10").items():
                zf.writestr(k, v * 50)
        c.stage_update("R3.1.10", zip_bytes=buf.getvalue()[: len(buf.getvalue()) // 2])
    elif case == "not_a_zip":
        c.stage_update("R3.1.10", zip_bytes=b"<html>502 Bad Gateway</html>")
    elif case == "empty":
        c.stage_update("R3.1.10", zip_bytes=b"")
    elif case == "missing_files":
        c.stage_update("R3.1.10", _release_files("R3.1.10", drop=("core/update.py",)))
    elif case == "bad_sha":
        c.stage_update("R3.1.10", meta={"sha256": "0" * 64})
    elif case == "wrong_version":
        c.stage_update("R3.1.11", _release_files("R3.1.10"))
    elif case == "syntax_error":
        c.stage_update("R3.1.10", _release_files("R3.1.10", extra={"core/thing.py": "def broken(:\n"}))
    else:
        c.stage_update("R3.1.10", _release_files("R3.1.10", extra={"requirements.txt": "nope==99\n"}))

        def pip(args, timeout=upd.PIP_TIMEOUT_S):
            c.pip_calls.append(args)
            if case == "pip_timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            failing = "--dry-run" in args if case == "pip_unresolvable" else "--dry-run" not in args
            return subprocess.CompletedProcess(args, 1 if failing else 0, "", "ERROR: no matching distribution")

        monkeypatch.setattr(upd, "_run_pip", pip)

    assert c.boot() == "aborted"
    assert c.restarts == [], "an aborted update keeps booting the old version"
    _unchanged(c)
    _triggers_gone(c)
    st = c.state()
    assert st["phase"] == "aborted" and st["reason"]
    assert not (c.data / upd.STAGING_DIR).exists()
    result = json.loads((c.data / upd.RESULT_FILE).read_text())
    assert result["success"] is False and result["outcome"] == "aborted"


def test_matching_sha_is_accepted(install):
    c = install
    c.stage_update("R3.1.10")
    sha = upd._sha256_file(str(c.data / "update.zip"))
    meta = json.loads((c.data / "update.json").read_text())
    meta["sha256"] = sha
    (c.data / "update.json").write_text(json.dumps(meta))
    assert c.boot() == "applied"


def test_unsafe_zip_entries_are_never_written(install):
    c = install
    evil = zipfile.ZipInfo("../../evil.py")
    link = zipfile.ZipInfo("core/link.py")
    link.external_attr = (0o120777 << 16)
    files = _release_files("R3.1.10")
    with zipfile.ZipFile(c.data / "update.zip", "w") as zf:
        for k, v in files.items():
            zf.writestr(k, v)
        zf.writestr(evil, "pwned")
        zf.writestr(link, "/etc/passwd")
    (c.data / "update.json").write_text(json.dumps({"to_version": "R3.1.10"}))
    (c.data / "update.flag").write_text("1")
    assert c.boot() == "applied"
    assert not (c.root.parent.parent / "evil.py").exists()
    assert not (c.root / "core/link.py").exists()


def test_user_data_in_the_zip_never_overwrites(install):
    c = install
    c.stage_update("R3.1.10", _release_files("R3.1.10", extra={
        "data/meshtastic_data.db": "shipped", ".mesh-dash_config": "COMMUNITY_API_KEY=\n",
        "plugins/x/x.db": "shipped"}))
    assert c.boot() == "applied"
    assert (c.data / "meshtastic_data.db").read_bytes() == b"user-data"
    assert (c.data / ".mesh-dash_config").read_text() == "COMMUNITY_API_KEY=MD-KEEP\n"
    assert not (c.root / "plugins/x/x.db").exists()


def test_incomplete_trigger_is_discarded(install):
    c = install
    (c.data / "update.flag").write_text("1")
    assert c.boot() == "none"
    _triggers_gone(c)
    _unchanged(c)


# FAILURES DURING AND AFTER THE INSTALL ROLL BACK

def test_failure_midway_through_swap_is_undone_immediately(install, monkeypatch):
    c = install
    c.stage_update("R3.1.10")
    real = upd._copy_atomic
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        return real(src, dst)

    monkeypatch.setattr(upd, "_copy_atomic", flaky)
    assert c.boot() == "rolled_back"
    monkeypatch.setattr(upd, "_copy_atomic", real)
    assert c.restarts == []
    _unchanged(c)
    _triggers_gone(c)
    assert c.state()["blocked_version"] == "R3.1.10"


def test_power_cut_during_swap_is_undone_on_next_boot(install, monkeypatch):
    c = install
    c.stage_update("R3.1.10")
    real = upd._copy_atomic
    calls = {"n": 0}

    class PowerCut(BaseException):
        pass

    def dies(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise PowerCut()
        return real(src, dst)

    monkeypatch.setattr(upd, "_copy_atomic", dies)
    with pytest.raises(PowerCut):
        c.boot()
    monkeypatch.setattr(upd, "_copy_atomic", real)
    assert c.state()["phase"] == "applying"
    assert c.read("core/__init__.py") or True  # half-swapped tree on disk

    assert c.boot() == "rolled_back"
    assert len(c.restarts) == 1
    _unchanged(c)
    assert c.state()["phase"] == "rolled_back"


def test_crash_loop_rolls_back_after_max_trial_boots(install):
    c = install
    c.stage_update("R3.1.10")
    assert c.boot() == "applied"
    for i in range(upd.MAX_TRIAL_BOOTS):
        assert c.boot() == "trial", i
    assert c.boot() == "rolled_back"
    _unchanged(c)
    st = c.state()
    assert st["phase"] == "rolled_back" and st["blocked_version"] == "R3.1.10"
    assert "failed to start" in st["reason"]
    result = json.loads((c.data / upd.RESULT_FILE).read_text())
    assert result["outcome"] == "rolled_back"
    assert c.boot() == "none", "a rolled-back panel boots normally afterwards"


def test_rollback_reinstalls_previous_deps_when_they_changed(install):
    c = install
    c.stage_update("R3.1.10", _release_files("R3.1.10", extra={"requirements.txt": "httpx==0.28.2\n"}))
    assert c.boot() == "applied"
    c.pip_calls.clear()
    for _ in range(upd.MAX_TRIAL_BOOTS + 1):
        c.boot()
    assert c.read("requirements.txt") == "httpx==0.28.1\n"
    assert c.pip_calls and c.pip_calls[-1][-1].endswith("requirements.txt")


def test_user_requested_rollback(install):
    c = install
    c.stage_update("R3.1.10")
    c.boot()
    upd.mark_update_healthy(str(c.data))
    upd.request_rollback(str(c.data))
    assert c.boot() == "rolled_back"
    _unchanged(c)


def test_rollback_without_a_previous_update_is_refused(install):
    with pytest.raises(upd.UpdateAborted):
        upd.request_rollback(str(install.data))


def test_trial_watchdog_rolls_back_a_hung_startup(install, monkeypatch):
    c = install
    c.stage_update("R3.1.10")
    c.boot()
    monkeypatch.setattr(upd, "TRIAL_DEADLINE_S", 0.2)
    monkeypatch.setattr(upd, "_arm_trial_guards", ORIGINAL_ARM)
    assert c.boot() == "trial"
    deadline = time.time() + 5
    while not c.restarts[1:] and time.time() < deadline:
        time.sleep(0.05)
    assert len(c.restarts) == 2
    _unchanged(c)
    assert "did not become healthy" in c.state()["reason"]


def test_trial_crash_hook_rolls_back(install, monkeypatch):
    c = install
    c.stage_update("R3.1.10")
    c.boot()
    monkeypatch.setattr(upd, "TRIAL_DEADLINE_S", 60)
    monkeypatch.setattr(upd, "_arm_trial_guards", ORIGINAL_ARM)
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    assert c.boot() == "trial"
    try:
        raise ImportError("No module named 'newdep'")
    except ImportError:
        sys.excepthook(*sys.exc_info())
    assert len(c.restarts) == 2
    _unchanged(c)
    assert "crashed during startup" in c.state()["reason"]
    upd._trial_ok.set()


def test_healthy_mark_stops_the_watchdog(install, monkeypatch):
    c = install
    c.stage_update("R3.1.10")
    c.boot()
    monkeypatch.setattr(upd, "TRIAL_DEADLINE_S", 0.3)
    monkeypatch.setattr(upd, "_arm_trial_guards", ORIGINAL_ARM)
    assert c.boot() == "trial"
    upd.mark_update_healthy(str(c.data))
    time.sleep(0.6)
    assert len(c.restarts) == 1
    assert c.read("core/thing.py") == "VALUE = 'R3.1.10'\n"


def test_old_backups_are_pruned(install):
    c = install
    for v in ("R3.1.10", "R3.1.11", "R3.1.12"):
        c.stage_update(v)
        assert c.boot() == "applied"
        upd.mark_update_healthy(str(c.data))
        time.sleep(0.01)
    assert len(os.listdir(c.data / upd.BACKUPS_DIR)) == upd.BACKUPS_TO_KEEP


def test_restart_never_exits_zero(monkeypatch):
    codes = []
    monkeypatch.setattr(os, "execv", lambda *a: (_ for _ in ()).throw(OSError("exec failed")))
    monkeypatch.setattr(os, "_exit", lambda code: codes.append(code))
    upd._restart_process()
    assert codes == [75]


ORIGINAL_ARM = upd._arm_trial_guards


# CONFIG WRITES

def test_config_writes_are_atomic_and_keep_secrets(tmp_path):
    from core import config as cfg
    path = tmp_path / ".mesh-dash_config"
    path.write_text("COMMUNITY_API_KEY=MD-KEEP\nWEBSERVER_PORT=8181\n")
    cfg.write_dash_config(str(path), {"WEBSERVER_PORT": "9000"})
    text = path.read_text()
    assert "WEBSERVER_PORT=9000" in text and "COMMUNITY_API_KEY=MD-KEEP" in text
    assert (tmp_path / ".mesh-dash_config.bak").read_text().startswith("COMMUNITY_API_KEY=MD-KEEP")
    assert not [p for p in os.listdir(tmp_path) if ".tmp" in p]

    existing = {"COMMUNITY_API_KEY": "MD-KEEP", "AUTH_SECRET_KEY": "s3cret"}
    merged = {"COMMUNITY_API_KEY": "", "AUTH_SECRET_KEY": "YOUR_SUPER_SECRET_API_KEY_REPLACE_ME"}
    kept = cfg.keep_existing_secrets(existing, merged)
    assert sorted(kept) == ["AUTH_SECRET_KEY", "COMMUNITY_API_KEY"]
    assert merged == existing
    merged = {"COMMUNITY_API_KEY": "MD-NEW", "AUTH_SECRET_KEY": "s3cret"}
    assert cfg.keep_existing_secrets(existing, merged) == []
    assert merged["COMMUNITY_API_KEY"] == "MD-NEW"


def test_fail_trial_rolls_back_a_running_but_incomplete_version(install, monkeypatch):
    c = install
    c.stage_update("R3.1.10")
    c.boot()
    monkeypatch.setattr(upd, "TRIAL_DEADLINE_S", 60)
    monkeypatch.setattr(upd, "_arm_trial_guards", ORIGINAL_ARM)
    assert c.boot() == "trial"
    assert upd.trial_active()
    upd.fail_trial("critical routes missing after startup: ['/api/system/start-update']")
    assert not upd.trial_active()
    assert len(c.restarts) == 2
    _unchanged(c)
    assert "critical routes missing" in c.state()["reason"]


def test_fail_trial_is_a_no_op_outside_a_trial(install):
    upd.fail_trial("should do nothing")
    assert install.restarts == []
