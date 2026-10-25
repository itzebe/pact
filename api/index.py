"""Vercel entrypoint.

Vercel's Python runtime serves an ASGI ``app`` exported from a module in ``api/``.
Pact lives under ``backend/``, so that directory is put on the import path first.
No application code changes are needed: this only bridges the platform's layout to
the existing package.

The built frontend is served by Vercel's static output, not by FastAPI, so
``PACT_STATIC_DIR`` is deliberately left unset here and the API handles ``/api/*``
only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pact.main import app  # noqa: E402

__all__ = ["app"]
