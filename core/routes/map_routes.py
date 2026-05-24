import core.globals as g
# Auto-extracted from meshtastic_dashboard.py
import logging
from typing import List, Set
from fastapi import APIRouter, Request, Response, Depends, HTTPException, File, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from core.routes.schemas import User
from core.auth import verify_csrf, get_current_active_user

logger = logging.getLogger(__name__)
router = APIRouter()
@router.get("/api/map/tiles/{z}/{x}/{y}")
@router.get("/api/map/tiles/{z}/{x}/{y}.pbf")
async def serve_map_tile(z: int, x: int, y: int):
    """Serve a tile from the active MBTiles archive. No auth required for embeds."""
    cfg = _load_maps_config()
    active = cfg.get("active_file")
    if not active:
        logging.debug(f"[Tiles] No active mbtiles file configured")
        return Response(content=_EMPTY_TILE, media_type="image/gif")
    filepath = os.path.join(MAPS_DIR, active)
    if not os.path.isfile(filepath):
        logging.debug(f"[Tiles] Active file not found: {filepath}")
        return Response(content=_EMPTY_TILE, media_type="image/gif")

    # TMS Y-axis flip: tms_y = (2^zoom - 1) - slippy_y
    tms_y = (1 << z) - 1 - y

    def _read_tile():
        conn = _get_mbtiles_conn(filepath)
        if not conn:
            return None
        try:
            row = conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, tms_y)
            ).fetchone()
            return row[0] if row else None
        except Exception as e:
            logging.warning(f"[Tiles] DB error z={z} x={x} y={y}: {e}")
            return None

    tile_data = await asyncio.to_thread(_read_tile)
    if not tile_data:
        return Response(content=_EMPTY_TILE, media_type="image/gif")

    # Detect content type and encoding from magic bytes
    # Gzip magic: 0x1f 0x8b (the third byte varies)
    is_gzipped = len(tile_data) >= 2 and tile_data[0] == 0x1f and tile_data[1] == 0x8b
    
    headers = {"Cache-Control": "public, max-age=86400"}
    
    if is_gzipped:
        # Gzipped vector tile (PBF)
        ct = "application/x-protobuf"
        headers["Content-Encoding"] = "gzip"
    elif tile_data[:3] == b'\xff\xd8\xff':
        ct = "image/jpeg"
    elif tile_data[:8] == b'\x89PNG\r\n\x1a\n':
        ct = "image/png"
    else:
        # Assume uncompressed PBF
        ct = "application/x-protobuf"
    
    return Response(content=tile_data, media_type=ct, headers=headers)


@router.post("/api/map/download")
async def start_map_download(
    request: Request,
    user: User = Depends(verify_csrf),
):
    """Start a background download of an MBTiles file from a URL."""
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "No URL provided")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Only HTTP/HTTPS URLs are supported")

    async with _download_lock:
        if _download_state.get("status") == "downloading":
            raise HTTPException(409, "A download is already in progress. Cancel it first.")

    # Derive filename from URL or use timestamp
    filename = os.path.basename(parsed.path) or f"map_{int(time.time())}.mbtiles"
    if not filename.endswith(".mbtiles"):
        filename += ".mbtiles"
    # Sanitize
    filename = re.sub(r'[^\w\-.]', '_', filename)

    dest_path = os.path.join(MAPS_DIR, filename)

    # Init download state
    _download_state.clear()
    _download_state.update({
        "status": "downloading",
        "url": url,
        "filename": filename,
        "downloaded": 0,
        "total": 0,
        "percent": 0,
        "speed": 0,
        "eta": 0,
        "error": None,
        "cancel_event": asyncio.Event(),
        "start_time": time.time(),
    })

    async def _do_download():
        cancel_evt = _download_state["cancel_event"]
        tmp_path = dest_path + ".part"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=60)) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        _download_state["status"] = "error"
                        _download_state["error"] = f"HTTP {resp.status_code}: {resp.reason_phrase}"
                        return

                    total = int(resp.headers.get("content-length", 0))
                    _download_state["total"] = total

                    # Check disk space
                    if total > 0:
                        try:
                            disk_stat = shutil.disk_usage(MAPS_DIR)
                            if disk_stat.free < total * 1.1:
                                _download_state["status"] = "error"
                                _download_state["error"] = "Insufficient disk space"
                                return
                        except Exception:
                            pass

                    downloaded = 0
                    last_time = time.time()
                    last_bytes = 0
                    speed_window: List[float] = []

                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            if cancel_evt.is_set():
                                _download_state["status"] = "cancelled"
                                break

                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()

                            # Calculate speed every 0.5s
                            elapsed_since = now - last_time
                            if elapsed_since >= 0.5:
                                chunk_speed = (downloaded - last_bytes) / elapsed_since
                                speed_window.append(chunk_speed)
                                if len(speed_window) > 10:
                                    speed_window.pop(0)
                                last_time = now
                                last_bytes = downloaded

                            avg_speed = statistics.mean(speed_window) if speed_window else 0
                            _download_state["downloaded"] = downloaded
                            _download_state["speed"] = avg_speed
                            _download_state["percent"] = round((downloaded / total * 100), 1) if total > 0 else 0
                            _download_state["eta"] = round((total - downloaded) / avg_speed) if avg_speed > 0 and total > 0 else 0

            if _download_state["status"] == "downloading":
                # Move completed file into place
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                os.rename(tmp_path, dest_path)
                _download_state["status"] = "complete"
                _download_state["percent"] = 100
                logger.info("Map download complete: %s (%d bytes)", filename, downloaded)
        except asyncio.CancelledError:
            _download_state["status"] = "cancelled"
        except Exception as e:
            _download_state["status"] = "error"
            _download_state["error"] = str(e)
            logger.error("Map download failed: %s", e)
        finally:
            # Clean up partial file on error/cancel
            if _download_state["status"] in ("error", "cancelled"):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    asyncio.create_task(_do_download())
    return {"status": "started", "filename": filename}


@router.post("/api/map/download/cancel")
async def cancel_map_download(user: User = Depends(verify_csrf)):
    """Cancel an active download."""
    evt = _download_state.get("cancel_event")
    if evt and _download_state.get("status") == "downloading":
        evt.set()
        return {"status": "cancelling"}
    return {"status": "no_active_download"}


@router.get("/api/map/download/progress")
async def download_progress_sse(request: Request):
    """SSE stream of download progress updates."""
    async def gen():
        while True:
            if await request.is_disconnected():
                break
            state = {
                "status": _download_state.get("status", "idle"),
                "filename": _download_state.get("filename", ""),
                "downloaded": _download_state.get("downloaded", 0),
                "total": _download_state.get("total", 0),
                "percent": _download_state.get("percent", 0),
                "speed": round(_download_state.get("speed", 0)),
                "eta": _download_state.get("eta", 0),
                "error": _download_state.get("error"),
            }
            yield {"event": "progress", "data": json.dumps(state)}
            if state["status"] in ("complete", "error", "cancelled", "idle"):
                break
            await asyncio.sleep(0.5)
    return EventSourceResponse(gen())


@router.get("/api/map/debug")
async def debug_map_tiles():
    """Diagnostic endpoint to inspect active mbtiles file and sample tile."""
    cfg = _load_maps_config()
    active = cfg.get("active_file")
    result = {
        "active_file": active,
        "maps_dir": MAPS_DIR,
        "config_file": MAPS_CONFIG_FILE,
    }
    
    if not active:
        result["error"] = "No active mbtiles file configured"
        return result
    
    filepath = os.path.join(MAPS_DIR, active)
    result["filepath"] = filepath
    result["file_exists"] = os.path.isfile(filepath)
    
    if not result["file_exists"]:
        result["error"] = f"File not found: {filepath}"
        return result
    
    def _inspect():
        info = {}
        try:
            conn = sqlite3.connect(filepath)
            # Get metadata
            try:
                rows = conn.execute("SELECT name, value FROM metadata").fetchall()
                info["metadata"] = {r[0]: r[1] for r in rows}
            except Exception as e:
                info["metadata_error"] = str(e)
            
            # Count tiles
            try:
                count = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
                info["tile_count"] = count
            except Exception as e:
                info["tile_count_error"] = str(e)
            
            # Get zoom range
            try:
                zmin = conn.execute("SELECT MIN(zoom_level) FROM tiles").fetchone()[0]
                zmax = conn.execute("SELECT MAX(zoom_level) FROM tiles").fetchone()[0]
                info["zoom_range"] = {"min": zmin, "max": zmax}
            except Exception as e:
                info["zoom_range_error"] = str(e)
            
            # Sample a tile to check format
            try:
                row = conn.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles LIMIT 1"
                ).fetchone()
                if row:
                    z, x, y, data = row
                    info["sample_tile"] = {
                        "z": z, "x": x, "y": y,
                        "size_bytes": len(data),
                        "first_bytes_hex": data[:16].hex() if data else None,
                        "is_gzipped": len(data) >= 2 and data[0] == 0x1f and data[1] == 0x8b,
                    }
            except Exception as e:
                info["sample_tile_error"] = str(e)
            
            conn.close()
        except Exception as e:
            info["connection_error"] = str(e)
        return info
    
    db_info = await asyncio.to_thread(_inspect)
    result.update(db_info)
    return result


@router.get("/api/map/files")
async def list_map_files(user: User = Depends(get_current_active_user)):
    """List all .mbtiles files with metadata."""
    cfg = _load_maps_config()
    active = cfg.get("active_file")
    files = []
    try:
        for fname in sorted(os.listdir(MAPS_DIR)):
            if not fname.endswith(".mbtiles"):
                continue
            fpath = os.path.join(MAPS_DIR, fname)
            stat = os.stat(fpath)
            files.append({
                "filename": fname,
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 1),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "active": fname == active,
            })
    except Exception as e:
        logger.error("Error listing map files: %s", e)
    return {"files": files, "active": active}


@router.put("/api/map/files/{filename}/activate")
async def activate_map_file(
    filename: str,
    user: User = Depends(verify_csrf),
):
    """Set a specific MBTiles file as the active tile source."""
    filepath = os.path.join(MAPS_DIR, filename)
    if not os.path.isfile(filepath) or not filename.endswith(".mbtiles"):
        raise HTTPException(404, "File not found")
    # Validate it's a valid MBTiles file
    def _validate():
        try:
            conn = sqlite3.connect(filepath)
            conn.execute("SELECT COUNT(*) FROM tiles LIMIT 1")
            conn.close()
            return True
        except Exception:
            return False
    valid = await asyncio.to_thread(_validate)
    if not valid:
        raise HTTPException(400, "File does not appear to be a valid MBTiles archive")
    # Close old cached connection if switching
    cfg = _load_maps_config()
    old_active = cfg.get("active_file")
    if old_active and old_active != filename:
        _close_mbtiles_conn(os.path.join(MAPS_DIR, old_active))
    cfg["active_file"] = filename
    _save_maps_config(cfg)
    return {"status": "activated", "filename": filename}


@router.delete("/api/map/files/{filename}")
async def delete_map_file(
    filename: str,
    user: User = Depends(verify_csrf),
):
    """Delete an MBTiles file."""
    filepath = os.path.join(MAPS_DIR, filename)
    if not os.path.isfile(filepath) or not filename.endswith(".mbtiles"):
        raise HTTPException(404, "File not found")
    # Close cached connection
    _close_mbtiles_conn(filepath)
    cfg = _load_maps_config()
    if cfg.get("active_file") == filename:
        cfg["active_file"] = None
        _save_maps_config(cfg)
    try:
        os.remove(filepath)
    except OSError as e:
        raise HTTPException(500, f"Failed to delete file: {e}")
    return {"status": "deleted", "filename": filename}


@router.post("/api/map/upload_tiles")
async def upload_mbtiles(
    file: UploadFile = File(...),
    user: User = Depends(verify_csrf),
):
    """Upload an MBTiles file (multipart form). Streams to disk."""
    if not file.filename or not file.filename.endswith(".mbtiles"):
        raise HTTPException(400, "File must have .mbtiles extension")
    filename = re.sub(r'[^\w\-.]', '_', file.filename)
    dest_path = os.path.join(MAPS_DIR, filename)
    tmp_path = dest_path + ".part"
    try:
        total = 0
        with open(tmp_path, "wb") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        # Validate
        def _validate():
            try:
                conn = sqlite3.connect(tmp_path)
                conn.execute("SELECT COUNT(*) FROM tiles LIMIT 1")
                conn.close()
                return True
            except Exception:
                return False
        valid = await asyncio.to_thread(_validate)
        if not valid:
            os.remove(tmp_path)
            raise HTTPException(400, "Uploaded file is not a valid MBTiles archive")
        # Close any existing connection for this filename
        _close_mbtiles_conn(dest_path)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        os.rename(tmp_path, dest_path)
        # Auto-activate if it's the only file
        cfg = _load_maps_config()
        mbtiles_files = [f for f in os.listdir(MAPS_DIR) if f.endswith(".mbtiles")]
        if len(mbtiles_files) == 1 or not cfg.get("active_file"):
            cfg["active_file"] = filename
            _save_maps_config(cfg)
        return {"status": "uploaded", "filename": filename, "size_bytes": total}
    except asyncio.CancelledError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    except HTTPException:
        raise
    except Exception as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(500, f"Upload failed: {e}")


@router.get("/api/map/status")
async def map_status():
    """Get current offline map status (no auth for toolbar indicator)."""
    cfg = _load_maps_config()
    active = cfg.get("active_file")
    has_file = False
    if active:
        has_file = os.path.isfile(os.path.join(MAPS_DIR, active))
        if not has_file:
            active = None
    return {"active_file": active, "available": has_file}


