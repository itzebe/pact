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


def test_an_unknown_api_path_is_not_swallowed_by_the_spa_mount(tmp_path):
    """The single-container image must answer an unknown ``/api`` path with a JSON 404.

    The mount sits at ``/`` and its fallback returns ``index.html`` for any
    extension-less path, so a mistyped API route such as ``/api/nope`` was served
    the HTML shell with a 200. A client that parses the response as JSON then fails
    confusingly, and the API surface silently lies about which routes exist. The
    API routes are matched before the mount in the real app, so any ``/api`` path
    reaching the mount is by definition unknown and must stay a 404 — including the
    bare ``/api``, which the Vercel deployment also answers with a JSON 404.
    """
    client = _client(tmp_path)
    for path in ("/api", "/api/", "/api/definitely-not-a-route"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "text/html" not in response.headers["content-type"], path
