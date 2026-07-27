#!/usr/bin/env python3
"""
MeshDash Release Helper — single source of truth for version bumps.

Usage:
    python3 scripts/release.py bump R3.1.5      # Bump version everywhere
    python3 scripts/release.py check             # Check version consistency
    python3 scripts/release.py zip               # Build release zip
    python3 scripts/release.py upload             # Upload zip + install.sh to server
    python3 scripts/release.py api R3.1.5         # Bump API version on server
    python3 scripts/release.py push               # Push to GitHub via API
    python3 scripts/release.py all R3.1.5         # Do everything (bump + zip + upload + api + push)

The canonical version lives in meshtastic_dashboard.py (FastAPI app version).
All other files are updated to match.
"""
import os
import re
import sys
import shutil
import zipfile
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Files that contain the version string and must be updated ──
# Each entry: (relative_path, old_pattern, new_template)
# Uses {ver} as placeholder for the new version
VERSION_FILES = [
    # Main app
    ("meshtastic_dashboard.py",
     r'version="R3\.1\.\d+"',
     'version="{ver}"'),
    # App Dockerfile
    ("Dockerfile",
     r'org\.opencontainers\.image\.version="R3\.1\.\d+"',
     'org.opencontainers.image.version="{ver}"'),
    # Runner Dockerfile — 4 places
    ("docker/runner/Dockerfile",
     r'MeshDash R3\.1\.\d+ Runner',
     'MeshDash {ver} Runner'),
    ("docker/runner/Dockerfile",
     r'official Docker runner for MeshDash R3\.1\.\d+\+',
     'official Docker runner for MeshDash {ver}+'),
    ("docker/runner/Dockerfile",
     r'Official MeshDash Runner R3\.1\.\d+ —',
     'Official MeshDash Runner {ver} —'),
    ("docker/runner/Dockerfile",
     r'LABEL version="R3\.1\.\d+"',
     'LABEL version="{ver}"'),
    ("docker/runner/Dockerfile",
     r'Docker container for MeshDash R3\.1\.\d+\+',
     'Docker container for MeshDash {ver}+'),
    ("docker/runner/Dockerfile",
     r'org\.opencontainers\.image\.version="R3\.1\.\d+"',
     'org.opencontainers.image.version="{ver}"'),
    # Runner entrypoint
    ("docker/runner/entrypoint.sh",
     r'MeshDash R3\.1\.\d+ Docker Runner',
     'MeshDash {ver} Docker Runner'),
    # Docker Hub description
    ("docker/runner/dockerhub-shortdesc.txt",
     r'MeshDash R3\.1\.\d+ —',
     'MeshDash {ver} —'),
    ("docker/runner/dockerhub-description.md",
     r'MeshDash R3\.1\.\d+ — Official',
     'MeshDash {ver} — Official'),
    # README badge
    ("README.md",
     r'badge/version-R3\.1\.\d+-orange',
     'badge/version-{ver}-orange'),
    # README install URL
    ("README.md",
     r'meshdash\.co\.uk/versions/R3\.1\.\d+/install\.sh',
     'meshdash.co.uk/versions/{ver}/install.sh'),
]

# Files to exclude from the release zip
ZIP_EXCLUDES = [
    '.git', '__pycache__', '*.pyc', '*.pyo', 'data/', '*.db', '*.db-shm',
    '*.db-wal', '*.db-journal', '.mesh-dash_config', 'venv/', 'mesh-dash_venv*',
    'mesh-dash_backup_*', '.env', '.DS_Store', 'docker-data/',
    'docker-compose.local.yml', '*.bak', 'scripts/',
]


def get_current_version():
    """Read the current version from meshtastic_dashboard.py."""
    path = os.path.join(ROOT, "meshtastic_dashboard.py")
    with open(path) as f:
        content = f.read()
    m = re.search(r'version="(R3\.\d+\.\d+)"', content)
    if not m:
        print("ERROR: Could not find version in meshtastic_dashboard.py")
        sys.exit(1)
    return m.group(1)


def bump_version(new_ver):
    """Update version string in all configured files."""
    if not re.match(r'^R3\.\d+\.\d+$', new_ver):
        print(f"ERROR: Version must match R3.X.Y format, got: {new_ver}")
        sys.exit(1)

    updated = 0
    for rel_path, pattern, template in VERSION_FILES:
        path = os.path.join(ROOT, rel_path)
        if not os.path.exists(path):
            print(f"  SKIP (not found): {rel_path}")
            continue
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        replacement = template.format(ver=new_ver)
        new_content = re.sub(pattern, replacement, content)
        if new_content != content:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"  UPDATED: {rel_path}")
            updated += 1
        else:
            print(f"  OK (already {new_ver}): {rel_path}")

    print(f"\nBumped {updated} files to {new_ver}")


def check_version():
    """Check that all files have consistent version strings."""
    current = get_current_version()
    print(f"Canonical version (meshtastic_dashboard.py): {current}\n")

    all_ok = True
    for rel_path, pattern, template in VERSION_FILES:
        path = os.path.join(ROOT, rel_path)
        if not os.path.exists(path):
            print(f"  MISSING: {rel_path}")
            all_ok = False
            continue
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        # Find any R3.X.Y version in the file
        versions = set(re.findall(r'R3\.\d+\.\d+', content))
        if not versions:
            print(f"  NO VERSION FOUND: {rel_path}")
            all_ok = False
        elif versions == {current}:
            print(f"  OK: {rel_path} → {current}")
        else:
            print(f"  MISMATCH: {rel_path} → {versions} (expected {current})")
            all_ok = False

    if all_ok:
        print(f"\n✅ All files consistent at {current}")
    else:
        print(f"\n❌ Version mismatch detected — run: python3 scripts/release.py bump {current}")
    return all_ok


def build_zip():
    """Build a flat release zip."""
    ver = get_current_version()
    stage = f"/tmp/meshdash_{ver}"
    zip_path = f"/tmp/meshdash_{ver}.zip"

    print(f"Building {ver} release zip...")

    # Clean stage
    if os.path.exists(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)

    # Copy files
    import fnmatch
    for item in os.listdir(ROOT):
        src = os.path.join(ROOT, item)
        skip = False
        for pattern in ZIP_EXCLUDES:
            if fnmatch.fnmatch(item, pattern) or item == 'scripts':
                skip = True
                break
        if skip:
            continue
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(stage, item),
                          ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo',
                                                         '*.db', '*.db-shm', '*.db-wal', '.DS_Store'))
        else:
            shutil.copy2(src, os.path.join(stage, item))

    # Ensure static/maps exists
    os.makedirs(os.path.join(stage, 'static', 'maps'), exist_ok=True)

    # Clean __pycache__ and plugin DBs
    for root_dir, dirs, files in os.walk(stage):
        if '__pycache__' in dirs:
            dirs.remove('__pycache__')
            shutil.rmtree(os.path.join(root_dir, '__pycache__'), ignore_errors=True)
        for f in files:
            if f.endswith(('.db', '.db-shm', '.db-wal', '.pyc', '.DS_Store')):
                os.remove(os.path.join(root_dir, f))

    # Build zip
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root_dir, dirs, files in os.walk(stage):
            for f in files:
                full = os.path.join(root_dir, f)
                arc = os.path.relpath(full, stage)
                zf.write(full, arc)

    file_count = sum(1 for _ in zipfile.ZipFile(zip_path).namelist())
    size = os.path.getsize(zip_path)
    print(f"✅ Built: {zip_path} ({file_count} files, {size/1024/1024:.1f}MB)")
    return zip_path


def upload_zip():
    """Upload zip + install.sh to the FastPanel server."""
    ver = get_current_version()
    zip_path = f"/tmp/meshdash_{ver}.zip"

    if not os.path.exists(zip_path):
        print("No zip found. Run 'python3 scripts/release.py zip' first.")
        sys.exit(1)

    # Load credentials
    env_path = "/Users/russ/Overlord/.env"
    host = user = password = None
    with open(env_path) as f:
        for line in f:
            if line.startswith("MESHDASH_INTERNAL_IP="):
                host = line.split("=", 1)[1].strip()
            elif line.startswith("MESHDASH_SSH_USER="):
                user = line.split("=", 1)[1].strip()
            elif line.startswith("MESHDASH_SSH_PASSWORD="):
                password = line.split("=", 1)[1].strip()

    if not all([host, user, password]):
        print("ERROR: Missing MESHDASH_* credentials in .env")
        sys.exit(1)

    base_dir = "/var/www/meshdash_co__usr/data/www/meshdash.co.uk/versions"
    ver_dir = f"{base_dir}/{ver}"
    prev_ver = None

    # Find previous version for install.sh
    result = subprocess.run(
        ["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no",
         f"{user}@{host}", f"ls {base_dir}/ | sort -V | tail -5"],
        capture_output=True, text=True
    )
    versions = [v.strip() for v in result.stdout.strip().split('\n') if v.strip()]
    for v in reversed(versions):
        if v != ver:
            prev_ver = v
            break

    print(f"Uploading {ver} to server (previous: {prev_ver})...")

    # Create version directory
    subprocess.run(["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no",
                    f"{user}@{host}", f"mkdir -p {ver_dir}"], check=True)

    # Upload zip
    subprocess.run(["sshpass", "-p", password, "scp", "-o", "StrictHostKeyChecking=no",
                    zip_path, f"{user}@{host}:{ver_dir}/mesh-dash.zip"], check=True)
    print(f"  ✅ Uploaded mesh-dash.zip")

    # Copy install.sh from previous version
    if prev_ver:
        subprocess.run(["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no",
                        f"{user}@{host}",
                        f"cp {base_dir}/{prev_ver}/install.sh {ver_dir}/install.sh 2>/dev/null && echo OK || echo SKIP"],
                       capture_output=True, text=True)
        if "OK" in subprocess.run(["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no",
                                    f"{user}@{host}", f"ls {ver_dir}/install.sh 2>/dev/null"],
                                   capture_output=True, text=True).stdout:
            print(f"  ✅ Copied install.sh from {prev_ver}")
        else:
            print(f"  ⚠️  No install.sh in {prev_ver} — copy manually!")

    print(f"✅ Upload complete: {ver_dir}/")


def bump_api(new_ver=None):
    """Bump the API version on the server."""
    if new_ver is None:
        new_ver = get_current_version()

    env_path = "/Users/russ/Overlord/.env"
    host = user = password = None
    with open(env_path) as f:
        for line in f:
            if line.startswith("MESHDASH_INTERNAL_IP="):
                host = line.split("=", 1)[1].strip()
            elif line.startswith("MESHDASH_SSH_USER="):
                user = line.split("=", 1)[1].strip()
            elif line.startswith("MESHDASH_SSH_PASSWORD="):
                password = line.split("=", 1)[1].strip()

    if not all([host, user, password]):
        print("ERROR: Missing MESHDASH_* credentials in .env")
        sys.exit(1)

    php_code = f'''require_once "/var/www/meshdash_co__usr/data/www/meshdash.co.uk/db_connect.php";
$stmt = $pdo->prepare("UPDATE c2_api_config SET setting_value = ? WHERE setting_key = ?");
$stmt->execute(["{new_ver}", "api_version"]);
$stmt2 = $pdo->prepare("SELECT setting_value FROM c2_api_config WHERE setting_key = ?");
$stmt2->execute(["api_version"]);
echo "API version: " . $stmt2->fetchColumn() . "\\n";'''

    result = subprocess.run(
        ["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no",
         f"{user}@{host}", f"php -r '{php_code}'"],
        capture_output=True, text=True
    )
    print(result.stdout.strip() if result.stdout else result.stderr.strip())


def push_github():
    """Push all changes to GitHub via the REST API."""
    import base64, json, urllib.request

    token = None
    with open("/Users/russ/Overlord/.env") as f:
        for line in f:
            if line.startswith("GITHUB_PERSONAL_ACCESS_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    if not token:
        print("ERROR: Missing GITHUB_PERSONAL_ACCESS_TOKEN in .env")
        sys.exit(1)

    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
    base = "https://api.github.com/repos/ruspea/MeshDash/git"

    # Get remote HEAD
    resp = urllib.request.urlopen(f"{base}/refs/heads/main")
    remote_sha = json.loads(resp.read())["object"]["sha"]

    # Get base tree
    resp = urllib.request.urlopen(f"{base}/commits/{remote_sha}")
    base_tree = json.loads(resp.read())["tree"]["sha"]

    # Find changed files (git diff)
    result = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", "HEAD"],
                           capture_output=True, text=True)
    changed = [f for f in result.stdout.strip().split('\n') if f]
    # Also check staged
    result2 = subprocess.run(["git", "-C", ROOT, "diff", "--cached", "--name-only"],
                            capture_output=True, text=True)
    changed += [f for f in result2.stdout.strip().split('\n') if f]
    changed = list(set(changed))

    if not changed:
        print("No changes to push.")
        return

    print(f"Pushing {len(changed)} files to GitHub...")

    # Create blobs
    blobs = []
    for path in changed:
        full = os.path.join(ROOT, path)
        if not os.path.isfile(full):
            continue
        with open(full, "rb") as f:
            content = base64.b64encode(f.read()).decode()
        data = json.dumps({"content": content, "encoding": "base64"}).encode()
        resp = urllib.request.urlopen(urllib.request.Request(f"{base}/blobs", data=data, headers=headers))
        blob_sha = json.loads(resp.read())["sha"]
        blobs.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
        print(f"  {path}")

    # Create tree
    tree_data = json.dumps({"base_tree": base_tree, "tree": blobs}).encode()
    resp = urllib.request.urlopen(urllib.request.Request(f"{base}/trees", data=tree_data, headers=headers))
    tree_sha = json.loads(resp.read())["sha"]

    # Create commit
    ver = get_current_version()
    commit_data = json.dumps({"message": f"release: {ver}", "tree": tree_sha, "parents": [remote_sha]}).encode()
    resp = urllib.request.urlopen(urllib.request.Request(f"{base}/commits", data=commit_data, headers=headers))
    commit_sha = json.loads(resp.read())["sha"]

    # Update ref
    ref_data = json.dumps({"sha": commit_sha}).encode()
    req = urllib.request.Request(f"{base}/refs/heads/main", data=ref_data, headers=headers, method="PATCH")
    resp = urllib.request.urlopen(req)
    print(f"✅ Pushed: {json.loads(resp.read())['object']['sha'][:12]}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "bump":
        if len(sys.argv) < 3:
            print("Usage: python3 scripts/release.py bump R3.1.5")
            sys.exit(1)
        bump_version(sys.argv[2])
    elif cmd == "check":
        check_version()
    elif cmd == "zip":
        build_zip()
    elif cmd == "upload":
        upload_zip()
    elif cmd == "api":
        bump_api(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "push":
        push_github()
    elif cmd == "all":
        if len(sys.argv) < 3:
            print("Usage: python3 scripts/release.py all R3.1.5")
            sys.exit(1)
        ver = sys.argv[2]
        print("=== Step 1: Bump version ===")
        bump_version(ver)
        print("\n=== Step 2: Check consistency ===")
        if not check_version():
            sys.exit(1)
        print("\n=== Step 3: Build zip ===")
        build_zip()
        print("\n=== Step 4: Upload to server ===")
        upload_zip()
        print("\n=== Step 5: Push to GitHub ===")
        push_github()
        print("\n=== Step 6: Bump API (when ready) ===")
        print(f"Run when ready: python3 scripts/release.py api {ver}")
        print(f"\n✅ Release {ver} prepared. Bump API when ready for users.")
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()