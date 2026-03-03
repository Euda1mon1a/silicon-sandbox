"""SiliconSandbox Web UI — lightweight FastAPI server on port 8095.

Serves a single-page web app and proxies API calls to the sandbox engine
(8093) and orchestrator (8094).
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent
ASSETS_DIR = UI_DIR / "assets"

ENGINE_URL = "http://127.0.0.1:8093"
ORCHESTRATOR_URL = "http://127.0.0.1:8094"

app = FastAPI(title="SiliconSandbox UI", version="0.2.0")


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(UI_DIR / "index.html", media_type="text/html")


@app.get("/assets/{path:path}")
async def static_asset(path: str):
    file = ASSETS_DIR / path
    if not file.exists() or not str(file.resolve()).startswith(str(ASSETS_DIR.resolve())):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(file)


# ---------------------------------------------------------------------------
# API proxy — forward to backend services
# ---------------------------------------------------------------------------

@app.api_route("/api/engine/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy_engine(path: str, request: Request):
    return await _proxy(ENGINE_URL, path, request)


@app.api_route("/api/orchestrator/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy_orchestrator(path: str, request: Request):
    return await _proxy(ORCHESTRATOR_URL, path, request)


async def _proxy(base_url: str, path: str, request: Request) -> JSONResponse:
    url = f"{base_url}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if request.method == "GET":
                resp = await client.get(url)
            elif request.method == "POST":
                body = await request.body()
                resp = await client.post(
                    url,
                    content=body,
                    headers={"content-type": request.headers.get("content-type", "application/json")},
                )
            elif request.method == "DELETE":
                resp = await client.delete(url)
            else:
                return JSONResponse({"error": "method not allowed"}, status_code=405)

        return JSONResponse(resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        return JSONResponse({"error": f"service unavailable at {base_url}"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    engine_ok = await _check_health(ENGINE_URL)
    orchestrator_ok = await _check_health(ORCHESTRATOR_URL)
    return {
        "status": "ok" if engine_ok and orchestrator_ok else "degraded",
        "engine": "ok" if engine_ok else "down",
        "orchestrator": "ok" if orchestrator_ok else "down",
    }


async def _check_health(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base_url}/health")
            return resp.status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8095, log_level="info")
