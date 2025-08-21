from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from .api.v1.routers import api_v1_router


def create_app() -> FastAPI:
    app = FastAPI(title="New IT System API", version="v1")

    # CORS: keep permissive for now; tighten via settings later
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount versioned routers
    app.include_router(api_v1_router, prefix="/api/v1")

    # Serve static files under /static from local ./public directory
    app.mount("/static", StaticFiles(directory="public"), name="static")

    # Provide a favicon endpoint (redirect to your SVG)
    @app.get("/favicon.ico")
    def favicon_redirect():
        return RedirectResponse(url="/static/Favicon-01.svg", status_code=307)

    return app


app = create_app()


