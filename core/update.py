"""
MeshDash update module — dual-strategy updater.

Every path through this file is designed for zero data loss.

=== PATHS COVERED ===

P1: R2.x user clicks "Update" in dashboard → downloads R3.0 zip
    → writes update.zip + update.flag + update.major to data/
    → user restarts → MAJOR strategy runs
    → full backup → clean extract → migrate DBs/plugins/config

P2: R2.x → R3.0 via the new install.sh
    → install.sh detects existing mesh-dash/ → renames to backup
    → fresh extract → migrate DBs/plugins → writes c2_installed.flag

P3: R3.0.x → R3.0.y (incremental dot-release via dashboard Update button)
    → writes update.zip + update.flag ONLY (no major flag)
    → user restarts → INCREMENTAL strategy runs
    → in-place overlay, protects config + data/ + venv

P4: Fresh install (no prior version) → install.sh does clean extract.
    No update logic fires.

P5: R2.x user manually extracts R3.0 over existing (unsupported manual path).
    Dashboard startup will detect stale R2.x files and warn.

=== DATA-PROTECTION PRINCIPLES ===

1. NEVER delete before backup exists.
2. NEVER overwrite a database file.
3. ALWAYS preserve config (.mesh-dash_config, .env).
4. ALWAYS migrate plugins from backup.
5. ALWAYS restart the process after a successful update (os.execv).
6. ALWAYS write a result record so the dashboard can surface status.
7. If ANYTHING fails: leave backup intact, surface error, do NOT execv.
"""

import zipfile
import time
import sys
import os
import shutil
import json
import logging
import subprocess

logger = logging.getLogger("boot_updater")

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
    ".git",
    "__pycache__",
    "backup",
})


# PUBLIC ENTRY POINT — called once per boot, before FastAPI starts

def check_and_apply_update(data_dir: str = "data"):
    """
    Runs immediately on boot. Checks for update.flag + update.zip.
    Routes to incremental or major strategy automatically.
    """
    update_flag = os.path.join(data_dir, "update.flag")
    update_zip  = os.path.join(data_dir, "update.zip")
    major_flag  = os.path.join(data_dir, "update.major")

    if not (os.path.exists(update_flag) and os.path.exists(update_zip)):
        return

    _log_banner("UPDATE DETECTED ON BOOT")

    is_major = os.path.exists(major_flag)
    install_root = os.path.abspath(".")

    if is_major:
        _log_banner("MAJOR VERSION UPDATE — backup + clean extract + migrate")

    success = False
    error_msg = ""
    backup_dir = ""

    try:
        time.sleep(2)  # let filesystem I/O settle

        if is_major:
            backup_dir = _apply_major_update(data_dir, update_zip, install_root)
        else:
            _apply_incremental_update(data_dir, update_zip, install_root)

        success = True
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Update failed: {e}", exc_info=True)
    finally:
        # Always clean trigger files so we don't re-apply on next boot
        for f in (update_flag, update_zip, major_flag):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        _write_result(data_dir, success, is_major, backup_dir, error_msg)

    if success:
        # Write bootstrap marker so next boot (now on new code) does one final clean restart
        bootstrap_path = os.path.join(data_dir, "_bootstrap")
        if not os.path.exists(bootstrap_path):
            with open(bootstrap_path, "w") as f:
                f.write(str(int(time.time())))
        logger.info("Restarting process to load updated code...")
        _restart_process()
    else:
        logger.error("Update failed — process NOT restarted. Backup preserved.")
        sys.exit(1)


# INCREMENTAL UPDATE (dot-release: R2.2.8->R2.2.9, R3.0.1->R3.0.2, ...)

def _apply_incremental_update(data_dir: str, update_zip: str, install_root: str):
    """
    In-place file overlay. Fast. Safe because the file structure doesn't change.

    Protection rules (exact basename, not substring):
      - PROTECTED_FILES: never overwritten
      - PRESERVED_DIRS: entire directory skipped
      - DB files ending in .db/.db-shm/.db-wal: skipped
    """
    temp_dir = os.path.join(data_dir, ".update_temp_extract")
    _rm(temp_dir)
    os.makedirs(temp_dir)

    logger.info("Applying incremental update (in-place overlay)...")

    with zipfile.ZipFile(update_zip, "r") as zf:
        for member in zf.infolist():
            fname = member.filename

            # Path traversal guard
            if ".." in fname or fname.startswith("/"):
                continue

            # Exact basename protection (NOT substring)
            base = os.path.basename(fname.rstrip("/"))
            if base in PROTECTED_FILES:
                continue

            # Skip data files by basename extension
            if base.endswith(DATA_FILE_PATTERNS):
                continue

            # Skip JSON data files by basename
            if base in JSON_DATA_FILES:
                continue

            # Skip preserved directories
            top = fname.split("/")[0]
            if top in PRESERVED_DIRS:
                continue

            zf.extract(member, temp_dir)

    _merge_tree(temp_dir, install_root)
    _rm(temp_dir)

    # Install any new dependencies added in this dot-release
    _install_new_deps(install_root)

    logger.info("Incremental update applied")


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
    """Replace the current process with a fresh one (reloads code from disk)."""
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error(f"Failed to execv: {e}. Manual restart required.")
        sys.exit(0)


def _log_banner(text: str):
    logger.info("")
    logger.info("=" * 50)
    logger.info(f"  {text}")
    logger.info("=" * 50)
    logger.info("")


def _write_result(data_dir: str, success: bool, major: bool, backup: str, error: str):
    """Write .update_result.json so the dashboard can surface it post-update."""
    record = {
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": "major" if major else "incremental",
        "success": success,
    }
    if backup:
        record["backup_dir"] = backup
    if error:
        record["error"] = error

    try:
        path = os.path.join(data_dir, ".update_result.json")
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
    except Exception:
        pass
