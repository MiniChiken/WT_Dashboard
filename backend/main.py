import asyncio
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response

import analysis
import db
from poller import TelemetryPoller

poller = TelemetryPoller()

# Frozen (PyInstaller) builds extract bundled data next to sys._MEIPASS, not
# next to this file - the build script adds frontend/ there under the same
# "frontend" name, so this resolves the same relative layout either way.
if getattr(sys, "frozen", False):
    FRONTEND_DIR = Path(sys._MEIPASS) / "frontend"
else:
    FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# Some browsers will silently reuse a cached app.js/style.css from before a
# restart even with Cache-Control: no-store, if the original entry was cached
# without one (heuristic freshness never expires without a network check).
# A version query string on the asset URLs sidesteps this entirely - each
# restart gets URLs the browser has never cached before.
ASSET_VERSION = str(int(time.time()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(poller.run())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("/static/app.js", f"/static/app.js?v={ASSET_VERSION}")
    html = html.replace("/static/style.css", f"/static/style.css?v={ASSET_VERSION}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    # This is an actively-edited local dashboard, not a deployed site - always
    # serve the current file/data from disk rather than letting the browser
    # cache things indefinitely. Originally just index.html/app.js/style.css
    # (StaticFiles sets no explicit Cache-Control by default, so browsers were
    # free to skip revalidation entirely and keep executing stale JS after an
    # edit + reload) - extended to /api/ too after the session dropdown was
    # confirmed to get stuck showing a stale (e.g. empty, pre-first-session)
    # response indefinitely, the same no-explicit-header symptom as the
    # earlier JS/CSS bug, just on a fetch() response instead of a script.
    response = await call_next(request)
    if (
        request.url.path.startswith("/static/")
        or request.url.path.startswith("/api/")
        or request.url.path == "/"
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/live")
async def get_live():
    return poller.latest


@app.get("/api/map-image")
async def get_map_image():
    if poller.map_image is None:
        return JSONResponse({"error": "no map image yet"}, status_code=404)
    return Response(
        content=poller.map_image,
        media_type=poller.map_image_type,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/stats/sessions")
async def get_sessions():
    # Filtered to sessions with an actual friendly/enemy roster, since
    # matches where identity was never resolved (or nothing was ever logged -
    # a hangar visit, a match that ended before any tracked interaction)
    # otherwise cluttered the session dropdown with entries that show
    # "no players logged" no matter which session view you pick.
    sessions = db.list_sessions()
    result = []
    for s in sessions:
        roster = analysis.build_player_roster(s["id"])
        if roster["friendly"] or roster["enemy"]:
            result.append(s)
    return result


@app.get("/api/stats/sessions/{session_id}")
async def get_session(session_id: int):
    result = db.get_session(session_id)
    if result is None:
        return {"error": "not found"}
    return result


@app.get("/api/stats/sessions/{session_id}/telemetry")
async def get_session_telemetry(session_id: int):
    return db.get_session_telemetry(session_id)


@app.get("/api/stats/sessions/{session_id}/map-image")
async def get_session_map_image(session_id: int):
    image, image_type = db.get_session_map_image(session_id)
    if image is None:
        return JSONResponse({"error": "no map image for this session"}, status_code=404)
    return Response(content=image, media_type=image_type or "image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/stats/sessions/{session_id}/kill-locations")
async def get_session_kill_locations(session_id: int):
    return analysis.compute_kill_locations(session_id)


@app.get("/api/stats/sessions/{session_id}/roster")
async def get_session_roster(session_id: int):
    return analysis.build_player_roster(session_id)


@app.get("/api/stats/sessions/{session_id}/export")
async def export_session(session_id: int):
    result = db.get_session_export(session_id)
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    headers = {"Content-Disposition": f"attachment; filename=wt-session-{session_id}.json"}
    return JSONResponse(result, headers=headers)


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    queue = poller.subscribe()
    try:
        while True:
            data = await queue.get()
            await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    finally:
        poller.unsubscribe(queue)


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn

    port = 8765

    # Only auto-open a browser for the packaged build - running from source
    # during development already has a browser tab open/managed manually,
    # and popping a new one on every restart would get old fast.
    if getattr(sys, "frozen", False):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

    uvicorn.run(app, host="127.0.0.1", port=port)
