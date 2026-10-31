"""The single-container deployment must serve the SPA on client-side routes.

``StaticFiles(html=True)`` only falls back to ``index.html`` for a *directory*
URL, so a deep link such as ``/jobs/abc`` — handled by the client router —
returned 404 in the shipped image. These tests hold the fallback in place.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pact.main import _SPAStaticFiles


def _client(tmp_path: Path) -> TestClient:
    (tmp_path / "index.html").write_text("<!doctype html><title>Pact</title>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('pact')", encoding="utf-8")

    app = FastAPI()
    app.mount("/", _SPAStaticFiles(directory=str(tmp_path), html=True), name="static")
    return TestClient(app)


def test_the_root_serves_the_spa(tmp_path):
    response = _client(tmp_path).get("/")
    assert response.status_code == 200
    assert "Pact" in response.text


def test_a_client_side_route_falls_back_to_the_spa(tmp_path):
    response = _client(tmp_path).get("/jobs/job_abc123")
    assert response.status_code == 200
    assert "Pact" in response.text


def test_a_real_asset_is_served_verbatim(tmp_path):
    response = _client(tmp_path).get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_a_missing_asset_still_404s_rather_than_returning_html(tmp_path):
    """A 404 that names a file must not be answered with the HTML shell.

    Returning the SPA for a missing ``.js`` would surface a syntax error in the
    browser instead of an honest 404, and would hide a broken build.
    """
    response = _client(tmp_path).get("/assets/missing.js")
    assert response.status_code == 404
