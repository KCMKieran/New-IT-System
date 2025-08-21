"""
Compatibility entrypoint to expose FastAPI app for ASGI servers.
Run with: uvicorn backend.app.main:app (or equivalent) depending on working dir.
"""

from app.main import app  # noqa: F401


