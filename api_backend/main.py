"""Uvicorn entrypoint module for PreviewManager.

This file exists so the container can be started with:

  uvicorn main:app --host 0.0.0.0 --port 3002

It simply re-exports the FastAPI app defined in src.api.main.
"""

from src.api.main import app

__all__ = ["app"]
