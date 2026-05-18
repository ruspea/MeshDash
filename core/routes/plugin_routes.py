import os
import re
import core.globals as g
# Auto-extracted from meshtastic_dashboard.py
import asyncio
import logging
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, Request, Response, Depends, HTTPException, File, UploadFile, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from core.routes.schemas import User, RemoteInstallRequest
from core.auth import verify_csrf, get_current_active_user, _generate_csrf_token

logger = logging.getLogger(__name__)
router = APIRouter()
@router.get("/api/c2/status")
async def c2_status(user: User = Depends(get_current_active_user)):
    return c2_activity.get_snapshot()


@router.get("/c2_status", response_class=HTMLResponse)
async def c2_status_page(user: User = Depends(get_current_active_user)):
    path = os.path.join(g.STATIC_DIR, "c2_status.html")
    if os.path.exists(path):
        return FileResponse(path)
    return HTMLResponse("C2 Status viewer not found.", 404)


@router.get("/api/system/plugins")
async def list_plugins(user: User = Depends(get_current_active_user)):
    """Returns the status and manifest of all installed plugins safely."""
    safe_registry = {}
    for pid, data in g.PLUGIN_REGISTRY.items():
        safe_registry[pid] = {
            "manifest": data.get("manifest", {}),
            "status": data.get("status"),
            "error": data.get("error"),
            "path": data.get("path"),
            # watchdog_monitored is the ground truth: True only if this pid
            # is actively tracked in g._plugin_watchdog (requires "watchdog":true
            # in manifest AND successful load). Never derived from manifest alone.
            "watchdog_monitored": pid in g._plugin_watchdog,
            "last_watchdog_ping": g._plugin_watchdog.get(pid),
        }
    return {"status": "success", "plugins": safe_registry}


@router.get("/api/system/plugins/{plugin_id}/logs")
async def get_plugin_logs(plugin_id: str, user: User = Depends(get_current_active_user)):
    """Returns the last _PLUGIN_LOG_MAX_LINES log lines captured from plugin.<plugin_id> logger."""
    if plugin_id not in g.PLUGIN_REGISTRY:
        raise HTTPException(404, f"Plugin '{plugin_id}' not found.")
    handler = _plugin_log_handlers.get(plugin_id)
    lines = handler.get_lines() if handler else []
    return {
        "plugin_id": plugin_id,
        "count": len(lines),
        "max": _PLUGIN_LOG_MAX_LINES,
        "logs": lines,
    }


@router.delete("/api/system/plugins/{plugin_id}/logs")
async def clear_plugin_logs(plugin_id: str, user: User = Depends(verify_csrf)):
    """Clears the in-memory log buffer for the given plugin."""
    if plugin_id not in g.PLUGIN_REGISTRY:
        raise HTTPException(404, f"Plugin '{plugin_id}' not found.")
    handler = _plugin_log_handlers.get(plugin_id)
    if handler:
        handler.clear()
    return {"status": "success", "message": f"Log buffer cleared for plugin '{plugin_id}'."}


@router.get("/api/system/plugins/menu")
async def get_plugin_menu(user: User = Depends(get_current_active_user)):
    """Aggregates active plugin nav menus."""
    nav_items = []
    for pid, data in g.PLUGIN_REGISTRY.items():
        if data["status"] == "running" and "nav_menu" in data["manifest"]:
            for item in data["manifest"]["nav_menu"]:
                item_copy = item.copy()
                if "/static/plugins/" in item_copy.get("href", ""):
                    item_copy["href"] = item_copy["href"].replace("/static/plugins/", "/plugin/")
                nav_items.append(item_copy)
    return {"nav_items": nav_items}


@router.get("/api/plugins/bridges")
async def get_plugin_bridges(user: User = Depends(get_current_active_user)):
    """
    Returns bridge iframe descriptors for every running plugin that declares
    a "bridge" key in its manifest. Called by the frontend PluginBridge system
    ~800ms after DOMContentLoaded to mount hidden bridge iframes.

    Response shape expected by _loadPluginBridges() in app.js:
        { "bridges": [{ "plugin_id", "bridge_src", "name" }] }
    """
    import re as _re
    _SAFE_FILENAME = _re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*\.html$')

    bridges = []
    for pid, data in g.PLUGIN_REGISTRY.items():
        if data.get("status") != "running":
            continue
        manifest = data.get("manifest", {})
        bridge_file = manifest.get("bridge")
        if not bridge_file:
            continue
        # Validate filename: no path separators, no colons, must end .html
        if not _SAFE_FILENAME.match(bridge_file):
            logger.warning(
                "Plugin '%s' bridge filename '%s' failed validation  skipped",
                pid, bridge_file,
            )
            continue
        static_prefix = manifest.get("static_prefix", f"/static/plugins/{pid}")
        bridges.append({
            "plugin_id":  pid,
            "name":       manifest.get("name", pid),
            "bridge_src": f"{static_prefix}/{bridge_file}",
        })

    return {"bridges": bridges}


@router.get("/plugin/{plugin_id}", response_class=HTMLResponse)
async def serve_plugin_redirect(plugin_id: str, request: Request):
    return RedirectResponse(url=f"/plugin/{plugin_id}/", status_code=307)


@router.get("/plugin/{plugin_id}/", response_class=HTMLResponse)
async def serve_plugin_index(
    plugin_id: str,
    request: Request,
    user: User = Depends(get_current_active_user),
):
    return await serve_plugin_frame(plugin_id, "index.html", request, user)


@router.get("/plugin/{plugin_id}/{file_path:path}", response_class=HTMLResponse)
async def serve_plugin_frame(plugin_id: str, file_path: str, request: Request, user: User = Depends(get_current_active_user)):
    """Dynamically frames a plugin's static HTML inside the main MeshDash UI."""
    if plugin_id not in g.PLUGIN_REGISTRY:
        # Unknown plugin  genuine 404
        raise HTTPException(404, "Plugin not found.")

    plugin_data = g.PLUGIN_REGISTRY[plugin_id]
    plugin_status = plugin_data.get("status", "unknown")

    if plugin_status != "running":
        # Friendly offline page instead of raw 404
        manifest = plugin_data.get("manifest", {})
        plugin_name = manifest.get("name", plugin_id)
        status_label = plugin_status.upper()
        status_colour = {
            "stopped": "var(--warn)",
            "crashed":  "var(--err)",
            "hung":     "var(--err)",
            "pending_restart": "var(--pur,#b060ff)",
            "loading":  "var(--acc)",
        }.get(plugin_status, "var(--txt3)")

        error_msg = plugin_data.get("error") or (
            "This plugin is currently stopped. Use the Plugins page to start it."
            if plugin_status == "stopped" else
            f"Plugin status: {plugin_status}. Please check the Plugins page."
        )

        html_content = f'''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{plugin_name}  Offline | MeshDash</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      background: #0d1117;
      color: #8b949e;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100vh;
      text-align: center;
    }}
    .card {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 40px 48px;
      max-width: 440px;
      width: 90%;
    }}
    .icon {{ font-size: 36px; margin-bottom: 16px; }}
    h1 {{ color: #e6edf3; font-size: 1.3rem; font-weight: 600; margin-bottom: 8px; }}
    .status-badge {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 20px;
      font-size: 0.7rem;
      font-weight: 700;
      font-family: "SF Mono", "Fira Code", monospace;
      letter-spacing: 0.05em;
      margin: 8px 0 16px;
      border: 1px solid {status_colour};
      color: {status_colour};
    }}
    .error-msg {{
      color: #8b949e;
      font-size: 0.85rem;
      line-height: 1.5;
      margin-bottom: 24px;
    }}
    a {{
      display: inline-block;
      background: #21262d;
      color: #58a6ff;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 8px 20px;
      text-decoration: none;
      font-size: 0.85rem;
      transition: background 0.15s;
    }}
    a:hover {{ background: #30363d; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon"></div>
    <h1>{plugin_name}</h1>
    <div class="status-badge">{status_label}</div>
    <div class="error-msg">{error_msg}</div>
    <a href="/#view=plugins"> Back to Dashboard</a>
  </div>
</body>
</html>
        '''
        return HTMLResponse(content=html_content)

    manifest = g.PLUGIN_REGISTRY[plugin_id]["manifest"]
    plugin_name = manifest.get("name", plugin_id)

    if not file_path.endswith(".html"):
        return RedirectResponse(f"/static/plugins/{plugin_id}/{file_path}")

    index_path = os.path.join(g.STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(500, "Core index.html missing, cannot frame plugin.")

    base_html = await asyncio.to_thread(lambda: open(index_path, "r", encoding="utf-8").read())
    iframe_src = f"/static/plugins/{plugin_id}/{file_path}"

    iframe_html = f'''
    <div class="plugin-wrapper" style="flex: 1; padding: 15px; display: flex; flex-direction: column; min-height: calc(100vh - 80px);">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; border-bottom: 1px solid var(--bd2); padding-bottom: 8px;">
            <h2 style="margin: 0; color: var(--txt); font-family: var(--sans); font-size: 1.2rem;">
                <i class="fas fa-puzzle-piece" style="color: var(--acc);"></i> {plugin_name}
            </h2>
            <a href="{iframe_src}" target="_blank" class="btn" style="background: var(--bg2); border: 1px solid var(--bd2); color: var(--txt); padding: 4px 10px; border-radius: 4px; text-decoration: none; font-size: 0.75rem; font-family: var(--mono);">
                <i class="fas fa-external-link-alt"></i> Pop Out
            </a>
        </div>
        <iframe src="{iframe_src}" style="flex: 1; width: 100%; height: 100%; border: 1px solid var(--bd2); border-radius: 6px; background: var(--bg1);"></iframe>
    </div>
    '''

    framed_html = re.sub(r'<header id="topbar">.*?</header>', '', base_html, flags=re.DOTALL)
    framed_html = re.sub(r'<nav id="sidebar">.*?</nav>', '', framed_html, flags=re.DOTALL)
    framed_html = framed_html.replace(
        '<main id="content"></main>',
        f'{iframe_html}\n<main id="content" style="display: none !important;"></main>'
    )
    framed_html = re.sub(r'<title>.*?</title>', f'<title>{plugin_name} | MeshDash</title>', framed_html)
    return HTMLResponse(content=framed_html)


@router.post("/api/system/plugins/{plugin_id}/toggle")
async def toggle_plugin(plugin_id: str, action: str = Query(...), user: User = Depends(verify_csrf)):
    """Soft-starts or stops a plugin without restarting the main app."""
    if plugin_id not in g.PLUGIN_REGISTRY:
        raise HTTPException(404, "Plugin not found")

    state_file = os.path.join(g.PLUGIN_REGISTRY[plugin_id]["path"], ".disabled")

    if action == "stop":
        await asyncio.to_thread(lambda: open(state_file, "w").write("disabled"))
        g.PLUGIN_REGISTRY[plugin_id]["status"] = "stopped"
        g._plugin_watchdog.pop(plugin_id, None)
        
        # ? ADDED: Broadcast the state change to all SSE clients
        if g.main_event_loop:
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "plugin_update", "data": {"id": plugin_id, "status": "stopped"}}),
                g.main_event_loop
            )
            
        return {"status": "success", "message": f"Plugin {plugin_id} stopped."}
        
    elif action == "start":
        if os.path.exists(state_file):
            await asyncio.to_thread(os.remove, state_file)
        
        if g.PLUGIN_REGISTRY[plugin_id]["status"] in ["crashed", "stopped", "hung"]:
            g.PLUGIN_REGISTRY[plugin_id]["status"] = "pending_restart"
            
            # ? ADDED: Broadcast the pending restart state
            if g.main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "plugin_update", "data": {"id": plugin_id, "status": "pending_restart"}}),
                    g.main_event_loop
                )
                
            return {
                "status": "success",
                "message": f"Plugin {plugin_id} enabled. A system restart is required to load it.",
                "requires_restart": True,
            }
            
        g.PLUGIN_REGISTRY[plugin_id]["status"] = "running"
        if g.PLUGIN_REGISTRY[plugin_id].get("manifest", {}).get("watchdog", False):
            g._plugin_watchdog[plugin_id] = time.time()
            
        # ? ADDED: Broadcast the running state
        if g.main_event_loop:
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "plugin_update", "data": {"id": plugin_id, "status": "running"}}),
                g.main_event_loop
            )
            
        return {"status": "success", "message": f"Plugin {plugin_id} running."}

    raise HTTPException(400, "Invalid action. Use 'start' or 'stop'.")


@router.delete("/api/system/plugins/{plugin_id}")
async def remove_plugin(plugin_id: str, user: User = Depends(verify_csrf)):
    if plugin_id not in g.PLUGIN_REGISTRY:
        raise HTTPException(404, "Plugin not found")
    plugin_path = g.PLUGIN_REGISTRY[plugin_id]["path"]
    try:
        await asyncio.to_thread(shutil.rmtree, plugin_path)
        del g.PLUGIN_REGISTRY[plugin_id]
        g._plugin_watchdog.pop(plugin_id, None)
        return {"status": "success", "message": f"Plugin {plugin_id} removed. Please restart the system.", "requires_restart": True}
    except Exception as e:
        logger.error(f"Failed to remove plugin {plugin_id}: {e}")
        raise HTTPException(500, f"Failed to delete plugin files: {e}")


@router.post("/api/system/plugins/install")
async def install_plugin(file: UploadFile = File(...), user: User = Depends(verify_csrf)):
    """Accepts a .zip file, validates it, and installs it."""
    if not file.filename.endswith('.zip'):
        raise HTTPException(400, "Only .zip files are allowed.")

    os.makedirs(PLUGIN_DIR, exist_ok=True)
    temp_zip = os.path.join(PLUGIN_DIR, f"temp_{secrets.token_hex(4)}.zip")
    extract_dir = os.path.join(PLUGIN_DIR, f"temp_extract_{secrets.token_hex(4)}")

    try:
        # R2.X: stream upload to disk non-blocking
        def _save_upload():
            with open(temp_zip, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        await asyncio.to_thread(_save_upload)

        def _safe_extractall(zip_ref, dest_dir):
            """Extract zip preserving directory structure, skipping any path-traversing entries."""
            for entry in zip_ref.infolist():
                # Block any entry that would escape the destination tree
                if entry.is_dir() or entry.filename.endswith('/'):
                    continue
                # Normalise and validate the entry name
                normalized = entry.filename.replace('\\', '/')
                if any(part in ('..', '~') for part in normalized.split('/')):
                    logger.warning(f"Blocked suspicious zip entry: {entry.filename}")
                    continue
                # Block absolute paths or ones pointing outside dest
                safe_name = normalized.lstrip('/')
                if '/' in safe_name and not safe_name.startswith(('plugins/', 'static/', 'icons/', 'help/')):
                    # Allow only expected top-level plugin dirs
                    pass
                dest_path = os.path.join(dest_dir, safe_name)
                # Final realpath check to catch traversal via symlinks etc.
                real_dest = os.path.realpath(dest_path)
                real_dest_dir = os.path.realpath(dest_dir)
                if not real_dest.startswith(real_dest_dir + os.sep):
                    logger.warning(f"Blocked zip path traversal: {entry.filename}")
                    continue
                zip_ref.extract(entry, path=dest_dir)

        def _extract_and_validate():
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
                _safe_extractall(zip_ref, extract_dir)
            manifest_path = None
            target_move_dir = None
            for root, dirs, files in os.walk(extract_dir):
                if "__MACOSX" in root or "/." in root:
                    continue
                if "manifest.json" in files:
                    actual_file = os.path.join(root, "manifest.json")
                    if os.path.getsize(actual_file) > 0:
                        manifest_path = actual_file
                        target_move_dir = root
                        break
            return manifest_path, target_move_dir

        manifest_path, target_move_dir = await asyncio.to_thread(_extract_and_validate)

        if not manifest_path or not target_move_dir:
            raise HTTPException(400, "Invalid plugin: No valid manifest.json found in the zip.")

        def _read_manifest():
            with open(manifest_path, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
            if not content:
                raise ValueError("manifest.json is completely empty.")
            return json.loads(content)

        try:
            manifest = await asyncio.to_thread(_read_manifest)
        except json.JSONDecodeError as je:
            raise HTTPException(400, f"JSON Syntax Error in manifest: {je}")
        except Exception as e:
            raise HTTPException(400, f"Could not read manifest.json: {e}")

        pid = manifest.get("id")
        if not pid or not re.match(r"^[a-zA-Z0-9_-]+$", pid):
            raise HTTPException(400, "Invalid manifest: 'id' must be alphanumeric/underscores.")

        if "watchdog" not in manifest:
            raise HTTPException(400,
                "Invalid manifest: missing required field 'watchdog'. "
                "Add \"watchdog\": true (monitored) or \"watchdog\": false (unmonitored).")

        final_dest = os.path.join(PLUGIN_DIR, pid)
        backup_dest = os.path.join(PLUGIN_DIR, f"{pid}_backup_{secrets.token_hex(4)}")

        def _atomic_replace():
            if os.path.exists(final_dest):
                shutil.move(final_dest, backup_dest)
            try:
                shutil.move(target_move_dir, final_dest)
                if os.path.exists(backup_dest):
                    shutil.rmtree(backup_dest, ignore_errors=True)
            except Exception:
                if os.path.exists(backup_dest):
                    shutil.move(backup_dest, final_dest)
                raise

        await asyncio.to_thread(_atomic_replace)

        if pid in g.PLUGIN_REGISTRY:
            g.PLUGIN_REGISTRY[pid]["status"] = "pending_restart"
            g.PLUGIN_REGISTRY[pid]["manifest"]["version"] = manifest.get("version", "1.0.0")
            g.PLUGIN_REGISTRY[pid]["manifest"] = {**g.PLUGIN_REGISTRY[pid]["manifest"], **manifest}

        return {
            "status": "success",
            "message": f"Plugin '{manifest.get('name', pid)}' installed successfully. Restart required.",
            "requires_restart": True,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Plugin install failed: {e}")
        raise HTTPException(500, f"Installation failed: {e}")
    finally:
        if os.path.exists(temp_zip):
            try:
                os.remove(temp_zip)
            except Exception:
                pass
        if os.path.exists(extract_dir):
            await asyncio.to_thread(shutil.rmtree, extract_dir, True)


@router.post("/api/system/plugins/install-remote")
async def install_remote_plugin(req: RemoteInstallRequest, user: User = Depends(verify_csrf)):
    download_url = req.url
    is_valid, reason = await asyncio.to_thread(validate_url, download_url)
    if not is_valid:
        raise HTTPException(400, f"Invalid Target URL: {reason}")
    
    if not download_url.split("?")[0].endswith('.zip'):
        raise HTTPException(400, "Target URL must point to a .zip file.")

    if "?" in download_url:
        download_url += f"&_cb={int(time.time())}"
    else:
        download_url += f"?_cb={int(time.time())}"

    os.makedirs(PLUGIN_DIR, exist_ok=True)
    temp_zip = os.path.join(PLUGIN_DIR, f"temp_{secrets.token_hex(4)}.zip")
    extract_dir = os.path.join(PLUGIN_DIR, f"temp_extract_{secrets.token_hex(4)}")

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(
                download_url,
                headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
            )
            if resp.status_code != 200:
                raise HTTPException(502, f"Failed to download plugin. Remote server returned HTTP {resp.status_code}")

            def _write_zip():
                with open(temp_zip, "wb") as fh:
                    fh.write(resp.content)

            await asyncio.to_thread(_write_zip)

        def _safe_extractall(zip_ref, dest_dir):
            """Extract zip preserving directory structure, skipping any path-traversing entries."""
            for entry in zip_ref.infolist():
                # Block any entry that would escape the destination tree
                if entry.is_dir() or entry.filename.endswith('/'):
                    continue
                # Normalise and validate the entry name
                normalized = entry.filename.replace('\\', '/')
                if any(part in ('..', '~') for part in normalized.split('/')):
                    logger.warning(f"Blocked suspicious zip entry: {entry.filename}")
                    continue
                # Block absolute paths or ones pointing outside dest
                safe_name = normalized.lstrip('/')
                if '/' in safe_name and not safe_name.startswith(('plugins/', 'static/', 'icons/', 'help/')):
                    # Allow only expected top-level plugin dirs
                    pass
                dest_path = os.path.join(dest_dir, safe_name)
                # Final realpath check to catch traversal via symlinks etc.
                real_dest = os.path.realpath(dest_path)
                real_dest_dir = os.path.realpath(dest_dir)
                if not real_dest.startswith(real_dest_dir + os.sep):
                    logger.warning(f"Blocked zip path traversal: {entry.filename}")
                    continue
                zip_ref.extract(entry, path=dest_dir)

        def _extract_and_validate():
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
                _safe_extractall(zip_ref, extract_dir)
            manifest_path = None
            target_move_dir = None
            for root, dirs, files in os.walk(extract_dir):
                if "__MACOSX" in root or "/." in root:
                    continue
                if "manifest.json" in files:
                    actual_file = os.path.join(root, "manifest.json")
                    if os.path.getsize(actual_file) > 0:
                        manifest_path = actual_file
                        target_move_dir = root
                        break
            return manifest_path, target_move_dir

        manifest_path, target_move_dir = await asyncio.to_thread(_extract_and_validate)

        if not manifest_path or not target_move_dir:
            raise HTTPException(400, "Invalid plugin structure: No valid manifest.json found inside the zip.")

        def _read_manifest():
            with open(manifest_path, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
            if not content:
                raise ValueError("manifest.json is empty.")
            return json.loads(content)

        try:
            manifest = await asyncio.to_thread(_read_manifest)
        except json.JSONDecodeError as je:
            raise HTTPException(400, f"JSON Syntax Error in manifest: {je}")
        except Exception as e:
            raise HTTPException(400, f"Could not read manifest.json: {e}")

        pid = manifest.get("id")
        if not pid or not re.match(r"^[a-zA-Z0-9_-]+$", pid):
            raise HTTPException(400, "Invalid manifest: 'id' must be alphanumeric or underscores.")

        if "watchdog" not in manifest:
            raise HTTPException(400,
                "Invalid manifest: missing required field 'watchdog'. "
                "Add \"watchdog\": true (monitored) or \"watchdog\": false (unmonitored).")

        final_dest = os.path.join(PLUGIN_DIR, pid)
        backup_dest = os.path.join(PLUGIN_DIR, f"{pid}_backup_{secrets.token_hex(4)}")

        def _atomic_replace():
            # Rename existing to backup so we can restore on failure
            if os.path.exists(final_dest):
                shutil.move(final_dest, backup_dest)
            try:
                shutil.move(target_move_dir, final_dest)
                # Remove backup only once move succeeded
                if os.path.exists(backup_dest):
                    shutil.rmtree(backup_dest, ignore_errors=True)
            except Exception:
                # Restore backup on failure
                if os.path.exists(backup_dest):
                    shutil.move(backup_dest, final_dest)
                raise

        await asyncio.to_thread(_atomic_replace)

        if pid in g.PLUGIN_REGISTRY:
            g.PLUGIN_REGISTRY[pid]["status"] = "pending_restart"
            g.PLUGIN_REGISTRY[pid]["manifest"]["version"] = manifest.get("version", "1.0.0")
            g.PLUGIN_REGISTRY[pid]["manifest"] = {**g.PLUGIN_REGISTRY[pid]["manifest"], **manifest}

        return {
            "status": "success",
            "message": f"Plugin '{manifest.get('name', pid)}' fetched and installed successfully. Restart required.",
            "requires_restart": True,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Remote install failed: {e}")
        raise HTTPException(500, f"Remote Installation failed: {e}")
    finally:
        if os.path.exists(temp_zip):
            try:
                os.remove(temp_zip)
            except Exception:
                pass
        if os.path.exists(extract_dir):
            await asyncio.to_thread(shutil.rmtree, extract_dir, True)


