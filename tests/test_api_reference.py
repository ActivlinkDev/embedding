"""The /docs reference page and the schema behind it.

The page is documentation only, but two things about it can silently go wrong: a router adds a
tag that no sidebar group lists (its endpoints then drop out of the grouped sidebar), and the
page or schema is served when ENABLE_API_DOCS is off.
"""

import importlib
import os

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from utils.api_reference import OPENAPI_TAGS, TAG_GROUPS, build_openapi, reference_html


def _main(monkeypatch, enabled: bool):
    monkeypatch.setenv("ENABLE_API_DOCS", "true" if enabled else "false")
    # Routers whose secrets are absent are skipped by _include_router; that is fine here.
    import main

    return importlib.reload(main)


def test_every_tag_is_described_and_grouped_once():
    described = [tag["name"] for tag in OPENAPI_TAGS]
    grouped = [name for group in TAG_GROUPS for name in group["tags"]]
    assert len(described) == len(set(described))
    assert sorted(grouped) == sorted(described)


def test_every_route_tag_is_in_the_sidebar(monkeypatch):
    main = _main(monkeypatch, enabled=True)
    schema = main.app.openapi()
    grouped = {name for group in schema["x-tagGroups"] for name in group["tags"]}
    used = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        for tag in operation.get("tags", [])
    }
    assert used, "no routes were loaded"
    assert used <= grouped, f"tags missing from TAG_GROUPS: {sorted(used - grouped)}"
    assert "## Going live" in schema["info"]["description"]


def test_docs_page_served_when_enabled(monkeypatch):
    main = _main(monkeypatch, enabled=True)
    client = TestClient(main.app)

    page = client.get("/docs")
    assert page.status_code == 200
    assert "Scalar.createApiReference" in page.text
    # Not id="api-reference": the Scalar bundle auto-mounts a second, unconfigured viewer there.
    assert 'id="api-reference"' not in page.text
    assert '"url": "/openapi.json"' in page.text
    assert client.get("/openapi.json").status_code == 200

    moved = client.get("/redoc", follow_redirects=False)
    assert moved.status_code == 308 and moved.headers["location"] == "/docs"


def test_docs_and_schema_hidden_when_disabled(monkeypatch):
    main = _main(monkeypatch, enabled=False)
    client = TestClient(main.app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_build_openapi_describes_bearer_scheme():
    from fastapi import Depends
    from fastapi.security import HTTPBearer

    app = FastAPI(title="t", version="1", openapi_tags=OPENAPI_TAGS)
    router = APIRouter(tags=["Auth"])

    @router.get("/x")
    def x(_=Depends(HTTPBearer())):
        return {}

    app.include_router(router)
    schema = build_openapi(app)
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["bearerFormat"] == "JWT"
    assert build_openapi(app) is schema  # cached


def test_page_config_cannot_break_out_of_its_script_tag():
    html = reference_html("/openapi.json?</script><script>alert(1)</script>")
    assert "</script><script>alert(1)" not in html
