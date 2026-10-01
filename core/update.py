"""
MeshDash update module — transactional updater with automatic rollback.

An update must never leave a panel stuck. Every path below either finishes on
the new version, or puts the previous version back and keeps running it.

=== FLOW ===

1. DOWNLOAD (core/routes/system_routes.py start-update, panel still running)
   → preflight: disk space, install dir writable, no update already in trial
   → stream to data/update.zip.part, verify (zip integrity, sha256 when the
     server publishes one, required files, version inside == version offered)
   → atomic rename to data/update.zip, write data/update.json + update.flag
   → restart IN PLACE (os.execv). Never exit and hope a supervisor restarts us.

2. APPLY (check_and_apply_update, first thing on the next boot)
   → stage: extract to data/.update_staging (unsafe paths/symlinks skipped)
   → check: every .py compiles; deps resolve (pip --dry-run) then install,
     only when requirements.txt changed
   → snapshot: every file about to be replaced or removed is copied to
     data/update_backups/<from>_<ts>/ BEFORE anything is touched
   → swap: per-file atomic replace; files a release removed are deleted
     (tracked via data/.release_manifest.json)
   → state "trial", restart in place
   Any failure before the swap: nothing changed, keep booting the old version.
   Any failure during the swap: restore the snapshot, keep booting the old one.
   Power cut during the swap: state is "applying" → next boot restores.

3. TRIAL (every boot until the new version proves itself)
   → boot counter: more than MAX_TRIAL_BOOTS starts without being marked
     healthy → roll back (catches crash loops under any supervisor)
   → crash hook: unhandled exception during startup → roll back + restart
   → deadline: not healthy within TRIAL_DEADLINE_S → roll back + restart
   → healthy (app startup complete + stable) → state "good", old backups pruned

A rolled-back version is remembered (blocked_version) so it is not offered
again until a newer one is published or the user forces it.

Legacy R2.x → R3.0 major path (update.major flag) is kept unchanged below.
"""

import zipfile
import time
import sys
import os
import re
import shutil
import json
import hashlib
import logging
import stat
import subprocess
import threading

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

logger = logging.getLogger("boot_updater")
if not logger.handlers:
    # The updater runs before the app configures logging; keep its lines.
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - boot_updater - %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_TRIAL_BOOTS = 3
TRIAL_DEADLINE_S = int(os.environ.get("MESHDASH_UPDATE_TRIAL_SECONDS", "300"))
HEALTHY_AFTER_S = 20
BACKUPS_TO_KEEP = 2
PIP_TIMEOUT_S = 900

STATE_FILE = ".update_state.json"
RESULT_FILE = ".update_result.json"
RELEASE_MANIFEST = ".release_manifest.json"
BACKUPS_DIR = "update_backups"
STAGING_DIR = ".update_staging"

# Files we MUST NOT touch during incremental updates.
# Exact basename matches — no substring ambiguity.
PROTECTED_FILES = frozenset({
    ".mesh-dash_config",
    ".env",
    "setup.flag",
    ".setup",
    ".new",
    "c2_installed.flag",
    "migration.log",
    ".update_result.json",
    "version.tag",
    "docker-compose.local.yml",
})

# Files that contain user data and must survive ANY update type.
DATA_FILE_PATTERNS = (
    ".db", ".db-shm", ".db-wal",   # SQLite database + WAL/Shm
    ".db-journal",                  # SQLite journal (rolling)
)

JSON_DATA_FILES = frozenset({
    "slots.json",
    "geocode_cache.json",
})

# Directories preserved as-is during incremental updates.
PRESERVED_DIRS = frozenset({
    "data",
    "mesh-dash_venv",
    "venv",
    ".git",
    "__pycache__",
    "backup",
})

# A release zip must contain these or it is not a MeshDash release.
REQUIRED_RELEASE_FILES = (
    "meshtastic_dashboard.py",
    "core/__init__.py",
    "core/update.py",
    "requirements.txt",
    "static/index.html",
)

_VERSION_RE = re.compile(r'version="(R[0-9][0-9.]*)"')


class UpdateAborted(Exception):
    """The update was refused before anything on disk changed."""


# PATHS + SMALL FILE HELPERS

def _abs_data_dir(data_dir: str = "data", install_root: str = None) -> str:
    root = install_root or INSTALL_ROOT
    return data_dir if os.path.isabs(data_dir) else os.path.join(root, data_dir)


def _read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _fsync_dir(path: str):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _write_json_atomic(path: str, data) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def _copy_atomic(src: str, dst: str) -> None:
    """Copy src over dst so dst is always either the old or the new file.

    The temp file sits next to dst, so the final os.replace never crosses a
    filesystem boundary (data/ is often a separate Docker volume).
    """
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = f"{dst}.mdtmp"
    shutil.copy2(src, tmp)
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, dst)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_version_from_tree(root: str) -> str:
    """The version string baked into meshtastic_dashboard.py under root."""
    try:
        with open(os.path.join(root, "meshtastic_dashboard.py"), "r", encoding="utf-8") as f:
            m = _VERSION_RE.search(f.read())
        return m.group(1) if m else ""
    except Exception:
        return ""


def read_state(data_dir: str = "data") -> dict:
    return _read_json(os.path.join(_abs_data_dir(data_dir), STATE_FILE), {}) or {}


def _write_state(data_dir: str, state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_json_atomic(os.path.join(data_dir, STATE_FILE), state)


class _UpdateLock:
    """Exclusive lock so two processes never apply/rollback at once."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, ".update.lock")
        self.fd = None
        self.acquired = False

    def __enter__(self):
        if fcntl is None:
            self.acquired = True
            return self
        try:
            self.fd = open(self.path, "w")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except OSError:
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except Exception:
                pass
            self.fd.close()
        return False


# ZIP VERIFICATION + STAGING

def _zip_prefix(names) -> str:
    """Releases normally sit at the zip root; tolerate one wrapping folder."""
    if "meshtastic_dashboard.py" in names:
        return ""
    for n in names:
        if n.endswith("/meshtastic_dashboard.py") and n.count("/") == 1:
            return n.split("/")[0] + "/"
    return ""


def _is_unsafe_member(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    if name.startswith("/") or name.startswith("\\") or re.match(r"^[A-Za-z]:", name):
        return True
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        return True
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        return True
    return False


def verify_update_zip(zip_path: str, expected_sha256: str = None,
                      expected_version: str = None) -> str:
    """Raise UpdateAborted unless zip_path is a complete, matching release.

    Returns the version found inside the zip.
    """
    if not os.path.isfile(zip_path) or os.path.getsize(zip_path) == 0:
        raise UpdateAborted("update file is missing or empty")
    if expected_sha256:
        actual = _sha256_file(zip_path)
        if actual.lower() != expected_sha256.strip().lower():
            raise UpdateAborted(f"checksum mismatch (got {actual[:12]}…, expected {expected_sha256[:12]}…)")
    if not zipfile.is_zipfile(zip_path):
        raise UpdateAborted("update file is not a zip (download truncated or replaced by an error page?)")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad:
                raise UpdateAborted(f"corrupt entry in update zip: {bad}")
            names = zf.namelist()
            prefix = _zip_prefix(names)
            missing = [f for f in REQUIRED_RELEASE_FILES if prefix + f not in names]
            if missing:
                raise UpdateAborted(f"update zip is incomplete, missing: {missing}")
            src = zf.read(prefix + "meshtastic_dashboard.py").decode("utf-8", "replace")
    except UpdateAborted:
        raise
    except Exception as e:
        raise UpdateAborted(f"cannot read update zip: {e}")
    m = _VERSION_RE.search(src)
    found = m.group(1) if m else ""
    if expected_version and found and found != expected_version:
        raise UpdateAborted(f"zip contains {found}, but {expected_version} was offered")
    return found


def _should_stage(rel: str) -> bool:
    base = os.path.basename(rel)
    if base in PROTECTED_FILES or base in JSON_DATA_FILES:
        return False
    if base.endswith(DATA_FILE_PATTERNS):
        return False
    top = rel.split("/")[0]
    if top in PRESERVED_DIRS or top.startswith("mesh-dash_backup_"):
        return False
    if "/__pycache__/" in f"/{rel}" or rel.endswith(".pyc"):
        return False
    return True


def _stage(zip_path: str, staging: str) -> list:
    """Extract the installable part of the release into staging.

    Returns the sorted list of relative file paths staged.
    """
    _rm(staging)
    os.makedirs(staging)
    staged = []
    with zipfile.ZipFile(zip_path) as zf:
        prefix = _zip_prefix(zf.namelist())
        for info in zf.infolist():
            if info.is_dir():
                continue
            if _is_unsafe_member(info):
                logger.warning(f"Skipping unsafe zip entry: {info.filename}")
                continue
            if prefix and not info.filename.startswith(prefix):
                continue
            rel = info.filename[len(prefix):].replace("\\", "/")
            if not rel or not _should_stage(rel):
                continue
            dest = os.path.join(staging, rel)
            if not os.path.abspath(dest).startswith(os.path.abspath(staging) + os.sep):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as s, open(dest, "wb") as d:
                shutil.copyfileobj(s, d)
            staged.append(rel)
    return sorted(staged)


def _compile_check(staging: str, files: list) -> None:
    """Every Python file in the release must at least compile."""
    for rel in files:
        if not rel.endswith(".py"):
            continue
        path = os.path.join(staging, rel)
        try:
            with open(path, "rb") as f:
                compile(f.read(), rel, "exec", dont_inherit=True)
        except SyntaxError as e:
            raise UpdateAborted(f"release file {rel} does not compile: {e.msg} (line {e.lineno})")


def _run_pip(args: list, timeout: int = PIP_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip"] + args,
                          capture_output=True, text=True, timeout=timeout)


def _requirements_hash(path: str) -> str:
    return _sha256_file(path) if os.path.exists(path) else ""


def _sync_deps(new_req: str, current_req: str) -> bool:
    """Install the release's dependencies if they changed.

    Resolves first (pip --dry-run) so an impossible requirement aborts the
    update before anything is installed. Returns True if pip ran.
    """
    if not os.path.exists(new_req):
        return False
    if _requirements_hash(new_req) == _requirements_hash(current_req):
        logger.info("requirements.txt unchanged — skipping dependency install")
        return False
    logger.info("requirements.txt changed — resolving dependencies...")
    try:
        dry = _run_pip(["install", "--dry-run", "--no-cache-dir", "-r", new_req])
        # pip < 22.2 has no --dry-run; fall through to the real install then
        if dry.returncode != 0 and "no such option: --dry-run" not in (dry.stderr or ""):
            raise UpdateAborted(f"dependencies cannot be resolved: {(dry.stderr or '')[-400:]}")
        logger.info("Installing dependencies...")
        res = _run_pip(["install", "--no-cache-dir", "-r", new_req])
    except subprocess.TimeoutExpired:
        raise UpdateAborted(f"dependency install timed out after {PIP_TIMEOUT_S}s")
    except UpdateAborted:
        raise
    except Exception as e:
        raise UpdateAborted(f"dependency install could not run: {e}")
    if res.returncode != 0:
        raise UpdateAborted(f"dependency install failed: {(res.stderr or '')[-400:]}")
    logger.info("Dependencies installed")
    return True


def _restore_deps(req_path: str) -> None:
    """Best effort: put the old release's pinned deps back after a rollback."""
    if not os.path.exists(req_path):
        return
    try:
        res = _run_pip(["install", "--no-cache-dir", "-r", req_path])
        if res.returncode != 0:
            logger.warning(f"Restoring previous dependencies had issues: {(res.stderr or '')[-300:]}")
    except Exception as e:
        logger.warning(f"Restoring previous dependencies failed: {e}")


# SNAPSHOT / SWAP / ROLLBACK

def _snapshot_and_swap(staging: str, files: list, install_root: str, data_dir: str,
                       from_ver: str, to_ver: str, deps_changed: bool) -> str:
    """Back up everything the swap will touch, then swap. Returns backup dir.

    On any error during the swap the backup is restored before re-raising.
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(data_dir, BACKUPS_DIR, f"{from_ver or 'unknown'}_{ts}")
    os.makedirs(backup_dir, exist_ok=True)

    old_manifest = _read_json(os.path.join(data_dir, RELEASE_MANIFEST), None)
    new_set = set(files)
    stale = []
    if isinstance(old_manifest, dict):
        stale = sorted(rel for rel in old_manifest.get("files", [])
                       if rel not in new_set and _should_stage(rel)
                       and os.path.isfile(os.path.join(install_root, rel)))

    saved, added = [], []
    for rel in files + stale:
        src = os.path.join(install_root, rel)
        if os.path.isfile(src):
            dst = os.path.join(backup_dir, "files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            saved.append(rel)
        elif rel in new_set:
            added.append(rel)
    if os.path.exists(os.path.join(data_dir, RELEASE_MANIFEST)):
        shutil.copy2(os.path.join(data_dir, RELEASE_MANIFEST),
                     os.path.join(backup_dir, RELEASE_MANIFEST))

    meta = {
        "from_version": from_ver, "to_version": to_ver,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "saved": saved, "added": added, "removed": stale,
        "deps_changed": deps_changed,
    }
    _write_json_atomic(os.path.join(backup_dir, "backup.json"), meta)
    logger.info(f"Snapshot: {len(saved)} file(s) saved, {len(added)} new, {len(stale)} to remove → {backup_dir}")

    # From here on, a crash must be undone on the next boot.
    _write_state(data_dir, {"phase": "applying", "from_version": from_ver,
                            "to_version": to_ver, "backup": backup_dir})
    try:
        for rel in files:
            _copy_atomic(os.path.join(staging, rel), os.path.join(install_root, rel))
        for rel in stale:
            try:
                os.remove(os.path.join(install_root, rel))
            except FileNotFoundError:
                pass
        _write_json_atomic(os.path.join(data_dir, RELEASE_MANIFEST),
                           {"version": to_ver, "files": files})
    except Exception:
        logger.error("Swap failed — restoring snapshot", exc_info=True)
        _restore_snapshot(backup_dir, install_root, data_dir)
        raise
    return backup_dir


def _restore_snapshot(backup_dir: str, install_root: str, data_dir: str) -> dict:
    meta = _read_json(os.path.join(backup_dir, "backup.json"), None)
    if not isinstance(meta, dict):
        raise RuntimeError(f"backup metadata missing in {backup_dir}")
    for rel in meta.get("saved", []):
        _copy_atomic(os.path.join(backup_dir, "files", rel), os.path.join(install_root, rel))
    for rel in meta.get("added", []):
        try:
            os.remove(os.path.join(install_root, rel))
        except FileNotFoundError:
            pass
    old_manifest = os.path.join(backup_dir, RELEASE_MANIFEST)
    if os.path.exists(old_manifest):
        _copy_atomic(old_manifest, os.path.join(data_dir, RELEASE_MANIFEST))
    else:
        try:
            os.remove(os.path.join(data_dir, RELEASE_MANIFEST))
        except FileNotFoundError:
            pass
    return meta


def _rollback(data_dir: str, install_root: str, state: dict, reason: str) -> bool:
    """Put the previous version back. Returns True if files were restored."""
    backup_dir = state.get("backup") or ""
    to_ver = state.get("to_version", "")
    from_ver = state.get("from_version", "")
    _log_banner(f"ROLLING BACK {to_ver} → {from_ver}: {reason}")
    if not backup_dir or not os.path.isdir(backup_dir):
        logger.error("No backup available to roll back to — staying on current files")
        _write_state(data_dir, {"phase": "failed", "from_version": from_ver, "to_version": to_ver,
                                "reason": f"{reason}; no backup to restore"})
        _write_result(data_dir, False, False, "", f"{reason}; rollback impossible (no backup)",
                      from_ver=from_ver, to_ver=to_ver, outcome="failed")
        return False
    try:
        meta = _restore_snapshot(backup_dir, install_root, data_dir)
    except Exception as e:
        logger.error(f"Rollback failed: {e}", exc_info=True)
        _write_state(data_dir, {"phase": "failed", "from_version": from_ver, "to_version": to_ver,
                                "backup": backup_dir, "reason": f"{reason}; rollback error: {e}"})
        _write_result(data_dir, False, False, backup_dir, f"{reason}; rollback error: {e}",
                      from_ver=from_ver, to_ver=to_ver, outcome="failed")
        return False
    if meta.get("deps_changed"):
        _restore_deps(os.path.join(install_root, "requirements.txt"))
    _write_state(data_dir, {"phase": "rolled_back", "from_version": from_ver, "to_version": to_ver,
                            "backup": backup_dir, "reason": reason, "blocked_version": to_ver})
    _write_result(data_dir, False, False, backup_dir, reason,
                  from_ver=from_ver, to_ver=to_ver, outcome="rolled_back")
    logger.info(f"Rolled back to {from_ver}")
    return True


def _prune_backups(data_dir: str, keep: int = BACKUPS_TO_KEEP) -> None:
    root = os.path.join(data_dir, BACKUPS_DIR)
    if not os.path.isdir(root):
        return
    entries = sorted((os.path.join(root, d) for d in os.listdir(root)),
                     key=lambda p: os.path.getmtime(p), reverse=True)
    for old in entries[keep:]:
        _rm(old)


def _clear_triggers(data_dir: str) -> None:
    for name in ("update.flag", "update.zip", "update.major", "update.json"):
        try:
            os.remove(os.path.join(data_dir, name))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not remove {name}: {e}")


# TRIAL GUARDS

_trial_ok = threading.Event()
_trial_ctx = {}


def _trial_fail(reason: str) -> None:
    if _trial_ok.is_set():
        return
    _trial_ok.set()  # one rollback only
    ctx = dict(_trial_ctx)
    try:
        with _UpdateLock(ctx["data_dir"]):
            state = read_state(ctx["data_dir"])
            if state.get("phase") == "trial":
                _rollback(ctx["data_dir"], ctx["install_root"], state, reason)
    finally:
        ctx["restart"]()


def _arm_trial_guards(data_dir: str, install_root: str, restart) -> None:
    _trial_ctx.update(data_dir=data_dir, install_root=install_root, restart=restart)
    prev_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        prev_hook(exc_type, exc, tb)
        if not issubclass(exc_type, KeyboardInterrupt):
            _trial_fail(f"new version crashed during startup: {exc_type.__name__}: {exc}")

    sys.excepthook = _hook

    def _watchdog():
        if not _trial_ok.wait(TRIAL_DEADLINE_S):
            _trial_fail(f"new version did not become healthy within {TRIAL_DEADLINE_S}s")

    threading.Thread(target=_watchdog, name="update-trial-watchdog", daemon=True).start()


def trial_active() -> bool:
    """True while this process is the trial boot of a new version."""
    return bool(_trial_ctx) and not _trial_ok.is_set()


def fail_trial(reason: str) -> None:
    """The running trial version is unhealthy: roll back and restart now."""
    if trial_active():
        _trial_fail(reason)


def mark_update_healthy(data_dir: str = "data") -> bool:
    """Called once the app has started and stayed up. Ends the trial."""
    data_dir = _abs_data_dir(data_dir)
    with _UpdateLock(data_dir):
        state = read_state(data_dir)
        if state.get("phase") != "trial":
            _trial_ok.set()
            return False
        _trial_ok.set()
        to_ver = state.get("to_version", "")
        _write_state(data_dir, {"phase": "good", "from_version": state.get("from_version", ""),
                                "to_version": to_ver, "backup": state.get("backup", "")})
        _write_result(data_dir, True, False, state.get("backup", ""), "",
                      from_ver=state.get("from_version", ""), to_ver=to_ver, outcome="success")
        _prune_backups(data_dir)
        # Docker runner: record the version so it does not re-download it
        tag = os.path.join(INSTALL_ROOT, "version.tag")
        if os.path.exists(tag) and to_ver:
            try:
                with open(tag, "w") as f:
                    f.write(to_ver)
            except Exception:
                pass
    logger.info(f"Update to {to_ver} confirmed healthy")
    return True


def request_rollback(data_dir: str = "data") -> dict:
    """Ask the next boot to restore the backup taken by the last update."""
    data_dir = _abs_data_dir(data_dir)
    with _UpdateLock(data_dir):
        state = read_state(data_dir)
        backup = state.get("backup", "")
        if state.get("phase") not in ("good", "trial") or not backup or not os.path.isdir(backup):
            raise UpdateAborted("there is no previous version to roll back to")
        state["phase"] = "rollback_requested"
        _write_state(data_dir, state)
    return state


# PUBLIC ENTRY POINT — called once per boot, before anything else imports

def check_and_apply_update(data_dir: str = "data", install_root: str = None,
                           restart=None) -> str:
    """
    Runs first thing on boot. Returns what happened (for logs and tests):
    "none", "applied", "aborted", "rolled_back", "trial", "locked".
    restart() is called when the process must restart (applied/rolled back).
    """
    install_root = install_root or INSTALL_ROOT
    data_dir = _abs_data_dir(data_dir, install_root)
    restart = restart or _restart_process
    if not os.path.isdir(data_dir):
        return "none"

    with _UpdateLock(data_dir) as lock:
        if not lock.acquired:
            logger.warning("Another process holds the update lock — skipping update checks")
            return "locked"
        state = read_state(data_dir)
        phase = state.get("phase")

        if phase == "applying":
            _rollback(data_dir, install_root, state, "previous update was interrupted mid-install")
            outcome = "rolled_back"
        elif phase == "rollback_requested":
            _rollback(data_dir, install_root, state, "rollback requested by user")
            outcome = "rolled_back"
        elif phase == "trial":
            state["boots"] = int(state.get("boots", 0)) + 1
            if state["boots"] > MAX_TRIAL_BOOTS:
                _rollback(data_dir, install_root, state,
                          f"new version failed to start {MAX_TRIAL_BOOTS} times")
                outcome = "rolled_back"
            else:
                _write_state(data_dir, state)
                logger.info(f"Update trial boot {state['boots']}/{MAX_TRIAL_BOOTS} for {state.get('to_version')}")
                _arm_trial_guards(data_dir, install_root, restart)
                return "trial"
        else:
            has_flag = os.path.exists(os.path.join(data_dir, "update.flag"))
            has_zip = os.path.exists(os.path.join(data_dir, "update.zip"))
            if not (has_flag and has_zip):
                if has_flag or has_zip:
                    logger.warning("Incomplete update trigger found — discarding")
                    _clear_triggers(data_dir)
                return "none"
            if os.path.exists(os.path.join(data_dir, "update.major")):
                outcome = _apply_legacy_major(data_dir, install_root)
                if outcome != "applied":
                    return outcome
            else:
                outcome = _apply_incremental(data_dir, install_root)
                if outcome != "applied":
                    return outcome

    restart()
    return outcome


def _apply_incremental(data_dir: str, install_root: str) -> str:
    _log_banner("UPDATE DETECTED ON BOOT")
    zip_path = os.path.join(data_dir, "update.zip")
    meta = _read_json(os.path.join(data_dir, "update.json"), {}) or {}
    from_ver = read_version_from_tree(install_root) or meta.get("from_version", "")
    staging = os.path.join(data_dir, STAGING_DIR)

    try:
        to_ver = verify_update_zip(zip_path, meta.get("sha256"), meta.get("to_version"))
        files = _stage(zip_path, staging)
        _compile_check(staging, files)
        deps_changed = _sync_deps(os.path.join(staging, "requirements.txt"),
                                  os.path.join(install_root, "requirements.txt"))
    except Exception as e:
        reason = str(e) if isinstance(e, UpdateAborted) else f"unexpected error preparing update: {e}"
        logger.error(f"Update aborted, still running {from_ver}: {reason}",
                     exc_info=not isinstance(e, UpdateAborted))
        _rm(staging)
        _clear_triggers(data_dir)
        _write_state(data_dir, {"phase": "aborted", "from_version": from_ver,
                                "to_version": meta.get("to_version", ""), "reason": reason})
        _write_result(data_dir, False, False, "", reason, from_ver=from_ver,
                      to_ver=meta.get("to_version", ""), outcome="aborted")
        return "aborted"

    logger.info(f"Applying {from_ver} → {to_ver} ({len(files)} files)...")
    try:
        backup_dir = _snapshot_and_swap(staging, files, install_root, data_dir,
                                        from_ver, to_ver, deps_changed)
    except Exception as e:
        reason = f"install failed and was undone: {e}"
        if deps_changed:
            _restore_deps(os.path.join(install_root, "requirements.txt"))
        _rm(staging)
        _clear_triggers(data_dir)
        _write_state(data_dir, {"phase": "rolled_back", "from_version": from_ver, "to_version": to_ver,
                                "reason": reason, "blocked_version": to_ver})
        _write_result(data_dir, False, False, "", reason, from_ver=from_ver, to_ver=to_ver,
                      outcome="rolled_back")
        return "rolled_back"

    _rm(staging)
    _clear_triggers(data_dir)
    _write_state(data_dir, {"phase": "trial", "from_version": from_ver, "to_version": to_ver,
                            "backup": backup_dir, "boots": 0})
    _write_result(data_dir, True, False, backup_dir, "", from_ver=from_ver, to_ver=to_ver,
                  outcome="trial")
    logger.info(f"Update to {to_ver} installed — restarting into trial boot")
    return "applied"


def _apply_legacy_major(data_dir: str, install_root: str) -> str:
    """R2.x → R3.0 path, unchanged in behaviour apart from exit handling."""
    _log_banner("MAJOR VERSION UPDATE — backup + clean extract + migrate")
    zip_path = os.path.join(data_dir, "update.zip")
    backup_dir = ""
    try:
        time.sleep(2)
        backup_dir = _apply_major_update(data_dir, zip_path, install_root)
    except Exception as e:
        logger.error(f"Major update failed: {e}", exc_info=True)
        _clear_triggers(data_dir)
        _write_result(data_dir, False, True, backup_dir, str(e), outcome="failed")
        logger.error("Major update failed — backup preserved. Exiting non-zero so a supervisor retries.")
        sys.exit(1)
    _clear_triggers(data_dir)
    _write_result(data_dir, True, True, backup_dir, "", outcome="success")
    bootstrap_path = os.path.join(data_dir, "_bootstrap")
    if not os.path.exists(bootstrap_path):
        with open(bootstrap_path, "w") as f:
            f.write(str(int(time.time())))
    return "applied"


# MAJOR UPDATE (R2.x -> R3.0+)

def _apply_major_update(data_dir: str, update_zip: str, install_root: str) -> str:
    """
    Full backup -> clean extract -> data migration.
    Returns the backup directory path on success.

    STEPS:
      1. Scan for existing data files (DBs, JSON) in current install.
      2. Create timestamped full backup of everything except venv + .git.
      3. Extract new R3.0 zip to temp directory.
      4. Validate zip integrity.
      5. Clear old install files (keep .mesh-dash_config, .env, data/, venv/,
         backup dirs).
      6. Merge new files into install root.
      7. Migrate databases: copy DBs from backup -> data/.
      8. Migrate plugins: copy non-bundled plugins from backup -> plugins/.
      9. Validate post-update structure.
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = _unique_backup_name(install_root, f"mesh-dash_backup_{ts}")

    logger.info(f"Backup destination: {backup_dir}")

    # Step 0: Scan existing install for data files
    existing_data = _scan_data_files(install_root)

    # Step 1: Full backup
    _full_backup(install_root, backup_dir)

    # Step 2: Extract new version
    temp_dir = os.path.join(data_dir, ".update_temp_extract")
    _rm(temp_dir)
    os.makedirs(temp_dir)

    logger.info("Extracting R3.0 update...")
    with zipfile.ZipFile(update_zip, "r") as zf:
        for member in zf.infolist():
            if ".." in member.filename or member.filename.startswith("/"):
                continue
            zf.extract(member, temp_dir)

    # Step 3: Validate zip integrity
    _validate_zip_extraction(temp_dir)

    # Step 4: Clear old install (preserving data + config + venv)
    _clear_old_files(install_root, data_dir, backup_dir)

    # Step 5: Merge new files
    logger.info("Installing R3.0 files...")
    _merge_tree(temp_dir, install_root)
    _rm(temp_dir)

    # Step 6: Migrate databases
    _migrate_databases(backup_dir, os.path.join(install_root, data_dir),
                       existing_data)

    # Step 7: Migrate plugins
    _migrate_plugins(backup_dir, install_root)

    # Step 8: Install new dependencies (e.g. PyJWT added in R3.0)
    _install_new_deps(install_root)

    # Step 9: Post-update validation
    _validate_post_update(install_root)

    logger.info("Major update complete!")
    logger.info(f"   Old files preserved at: {backup_dir}")
    logger.info(f"   To roll back: rm -rf meshdash && mv {backup_dir} meshdash")
    return backup_dir


# MAJOR UPDATE STEP HELPERS

def _install_new_deps(install_root: str):
    """
    Run pip install against the updated requirements.txt to pick up new deps.
    Detects the correct pip whether native (mesh-dash_venv) or Docker (/opt/venv).
    """
    req_file = os.path.join(install_root, "requirements.txt")
    if not os.path.exists(req_file):
        logger.warning("requirements.txt not found — skipping dependency install")
        return

    logger.info("Installing/updating Python dependencies...")

    # Use the currently running Python's pip module — this works in ALL scenarios:
    #   Native: sys.executable points to mesh-dash_venv/bin/python
    #   Docker:  sys.executable points to /opt/venv/bin/python
    #   Self-heal post-venv-rebuild: sys.executable is already the new python
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", req_file],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            logger.warning(f"pip install had issues: {result.stderr[-500:]}")
        else:
            logger.info("Dependencies installed successfully")
    except subprocess.TimeoutExpired:
        logger.warning("pip install timed out after 600s — deps may be incomplete")
    except Exception as e:
        logger.warning(f"Dependency install failed: {e}")


def _scan_data_files(install_root: str) -> set:
    """Walk the entire install and identify every data file that must survive."""
    found = set()
    for dirpath, _, filenames in os.walk(install_root):
        rel = os.path.relpath(dirpath, install_root)
        for fn in filenames:
            if fn.endswith(DATA_FILE_PATTERNS) or fn in JSON_DATA_FILES:
                rp = os.path.join(rel, fn) if rel != "." else fn
                found.add(rp)
    logger.info(f"Found {len(found)} existing data file(s) in current install")
    for f in sorted(found):
        logger.info(f"   {f}")
    return found


def _full_backup(install_root: str, backup_dir: str):
    """Copy everything except the virtualenv and .git into the backup."""
    os.makedirs(backup_dir, exist_ok=True)

    exclude = {
        backup_dir,
        os.path.join(install_root, "mesh-dash_venv"),
        os.path.join(install_root, ".git"),
        os.path.join(install_root, "__pycache__"),
    }

    count = 0
    for item in sorted(os.listdir(install_root)):
        item_path = os.path.join(install_root, item)
        if item_path in exclude:
            continue
        # Skip any existing backup dirs
        if item.startswith("mesh-dash_backup_"):
            continue

        dest = os.path.join(backup_dir, item)
        try:
            if os.path.isdir(item_path):
                shutil.copytree(item_path, dest,
                    ignore=lambda d, f: [x for x in f if x.endswith(".update_temp_extract")])
            else:
                shutil.copy2(item_path, dest)
            count += 1
        except Exception as e:
            logger.warning(f"Could not backup {item}: {e}")
            # Don't abort — partial backup is better than none

    logger.info(f"Backed up {count} item(s) to {backup_dir}")
    return backup_dir


def _validate_zip_extraction(temp_dir: str):
    """Ensure the extracted update contains the expected R3.0 structure."""
    required = [
        "meshtastic_dashboard.py",
        "core/auth.py",
        "core/config.py",
        "core/update.py",
        "static/index.html",
        "requirements.txt",
    ]
    missing = [f for f in required if not os.path.exists(os.path.join(temp_dir, f))]
    if missing:
        raise RuntimeError(
            f"Update ZIP validation failed — missing critical files: {missing}"
        )
    logger.info("Update ZIP structure validated")


def _clear_old_files(install_root: str, data_dir: str, backup_dir: str):
    """
    Remove all old install files.
    KEEPS: .mesh-dash_config, .env, data/, mesh-dash_venv/, venv/ (Docker), and backup dirs.
    """
    keep = {".mesh-dash_config", ".env", data_dir, "mesh-dash_venv", "venv"}

    logger.info("Clearing old install files...")
    removed = 0
    for item in sorted(os.listdir(install_root)):
        if item in keep or item.startswith("mesh-dash_backup_"):
            continue
        item_path = os.path.join(install_root, item)
        try:
            if os.path.isdir(item_path):
                shutil.rmtree(item_path)
            else:
                os.remove(item_path)
            removed += 1
        except Exception as e:
            logger.warning(f"Could not remove {item}: {e}")

    logger.info(f"  Removed {removed} old file(s)/dir(s)")


def _migrate_databases(backup_dir: str, target_data_dir: str,
                       existing_data: set):
    """
    Copy all database files from backup into the target data directory.
    Does NOT overwrite existing files in data/.
    """
    logger.info("Migrating databases from backup...")
    os.makedirs(target_data_dir, exist_ok=True)

    migrated = 0

    # Search both possible locations (legacy root + data/)
    search_roots = [backup_dir, os.path.join(backup_dir, "data")]

    for search_root in search_roots:
        if not os.path.isdir(search_root):
            continue

        for item in sorted(os.listdir(search_root)):
            item_path = os.path.join(search_root, item)
            if not os.path.isfile(item_path):
                continue

            is_db = item.endswith(DATA_FILE_PATTERNS)
            is_json = item in JSON_DATA_FILES
            if not (is_db or is_json):
                continue

            dest = os.path.join(target_data_dir, item)

            # Never overwrite — data/ may already have this file
            if os.path.exists(dest):
                logger.info(f"  {item} (already in data/)")
                continue

            try:
                shutil.copy2(item_path, dest)
                logger.info(f"  {item}")
                migrated += 1
            except Exception as e:
                logger.warning(f"  {item}: {e}")

    if migrated:
        logger.info(f"  {migrated} data file(s) migrated")
    else:
        logger.info("  No additional databases to migrate (data/ already current)")


def _migrate_plugins(backup_dir: str, install_root: str):
    """Copy user-installed plugins from backup -> new install plugins/."""
    src = os.path.join(backup_dir, "plugins")
    dst = os.path.join(install_root, "plugins")

    if not os.path.isdir(src):
        logger.info("  No plugins directory in backup")
        return

    items = sorted(i for i in os.listdir(src) if not i.startswith("."))
    if not items:
        return

    os.makedirs(dst, exist_ok=True)
    migrated, skipped = 0, 0

    for item in items:
        src_item = os.path.join(src, item)
        dst_item = os.path.join(dst, item)

        if os.path.exists(dst_item):
            logger.info(f"  {item} (bundled — using new version)")
            skipped += 1
            continue

        try:
            if os.path.isdir(src_item):
                shutil.copytree(src_item, dst_item)
            else:
                shutil.copy2(src_item, dst_item)
            logger.info(f"  {item}")
            migrated += 1
        except Exception as e:
            logger.warning(f"  {item}: {e}")

    if migrated or skipped:
        logger.info(f"  {migrated} plugin(s) migrated, {skipped} skipped (bundled)")


def _validate_post_update(install_root: str):
    """Quick sanity check: are critical files present?"""
    checks = [
        ("meshtastic_dashboard.py", "main application script"),
        ("core/__init__.py", "core package"),
        ("core/update.py", "update module"),
        ("static/index.html", "UI entry point"),
        ("requirements.txt", "Python dependencies"),
    ]
    for filename, desc in checks:
        path = os.path.join(install_root, filename)
        if not os.path.exists(path):
            raise RuntimeError(f"Post-update check failed: {desc} ({filename}) missing")

    config_file = os.path.join(install_root, "data", ".mesh-dash_config")
    config_file_legacy = os.path.join(install_root, ".mesh-dash_config")
    if not os.path.exists(config_file) and not os.path.exists(config_file_legacy):
        logger.warning(".mesh-dash_config missing — setup wizard will run on next boot")
    else:
        logger.info("Config file preserved")

    logger.info("Post-update validation passed — all critical files present")


# UTILITY FUNCTIONS

def _merge_tree(src: str, dst: str):
    """Move all files from src tree into dst tree (merges, doesn't replace)."""
    for dirpath, _, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for fn in filenames:
            s = os.path.join(dirpath, fn)
            d = os.path.join(target, fn)
            if os.path.exists(d):
                os.remove(d)
            shutil.move(s, d)


def _unique_backup_name(install_root: str, base: str) -> str:
    """Return a unique backup directory name, adding _2, _3, etc. if needed."""
    candidate = os.path.join(install_root, base) if not os.path.isabs(base) else base
    if not os.path.exists(candidate):
        return candidate
    counter = 1
    while True:
        candidate = (os.path.join(install_root, f"{base}_{counter}")
                     if not os.path.isabs(base) else f"{base}_{counter}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _rm(path: str):
    """Remove a file or directory tree, silently if it doesn't exist."""
    if not os.path.exists(path):
        return
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except Exception:
            pass



def _restart_process():
    """Replace the current process with a fresh one (reloads code from disk).

    execv keeps the PID, so this works under systemd (any Restart= policy),
    Docker, or a bare terminal. If execv itself fails we exit NON-zero, so a
    supervisor with Restart=on-failure still brings us back.
    """
    for h in logging.getLogger().handlers + logger.handlers:
        try:
            h.flush()
        except Exception:
            pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error(f"Failed to execv: {e}. Exiting with code 75 so the service manager restarts us.")
        os._exit(75)


def restart_process():
    """Public alias used by the web routes."""
    _restart_process()


def _log_banner(text: str):
    logger.info("")
    logger.info("=" * 50)
    logger.info(f"  {text}")
    logger.info("=" * 50)
    logger.info("")


def _write_result(data_dir: str, success: bool, major: bool, backup: str, error: str,
                  from_ver: str = "", to_ver: str = "", outcome: str = ""):
    """Write .update_result.json so the dashboard can surface it post-update."""
    record = {
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": "major" if major else "incremental",
        "success": success,
    }
    if outcome:
        record["outcome"] = outcome
    if from_ver:
        record["from_version"] = from_ver
    if to_ver:
        record["to_version"] = to_ver
    if backup:
        record["backup_dir"] = backup
    if error:
        record["error"] = error

    try:
        _write_json_atomic(os.path.join(data_dir, RESULT_FILE), record)
    except Exception:
        pass
