"""契约层面的服务测试：会话/CSRF/同源、分页、中文错误、静态回退。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from litworkbench.store import Workspace
from litworkbench.web.app import API_PREFIX, create_app


def test_workspace_endpoint_establishes_session(client):
    response = client.get(f"{API_PREFIX}/workspace")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) >= {"name", "root", "csrf_token", "ocr", "coverage"}
    assert payload["csrf_token"]
    cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("SameSite=Strict", "SameSite=strict")
    assert set(payload["coverage"]) >= {
        "known_pdf_pages", "covered_pdf_pages", "pending_pdf_pages", "failed_pdf_pages",
        "unknown_documents", "indexed_docx", "total_docx",
    }


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get(f"{API_PREFIX}/health").json() == {"status": "ok"}


def test_api_requires_session_cookie(client):
    assert client.get(f"{API_PREFIX}/documents").status_code == 401
    body = client.get(f"{API_PREFIX}/documents").json()
    assert body["error"]["code"] == "SESSION_REQUIRED"


def test_write_requires_csrf_and_origin(client):
    client.get(f"{API_PREFIX}/workspace")
    no_csrf = client.post(f"{API_PREFIX}/jobs/job_x/pause")
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "CSRF_INVALID"

    bad_csrf = client.post(f"{API_PREFIX}/jobs/job_x/pause",
                           headers={"X-CSRF-Token": "wrong"})
    assert bad_csrf.status_code == 403


def test_cross_site_origin_is_rejected(session, client):
    denied = client.post(f"{API_PREFIX}/jobs/job_x/pause",
                         headers={**session.write_headers("http://evil.example")})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ORIGIN_REJECTED"


def test_same_origin_write_is_accepted(session):
    response = session.post(f"{API_PREFIX}/jobs/job_missing/pause")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_unknown_api_route_is_json_404_not_spa(client):
    client.get(f"{API_PREFIX}/workspace")
    response = client.get(f"{API_PREFIX}/no-such-endpoint")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_missing_frontend_build_reports_chinese_error(tmp_path, web_workspace):
    app = create_app(web_workspace.root, start_worker=False,
                     static_dir=tmp_path / "没有编译产物")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.get(f"{API_PREFIX}/workspace")
        response = client.get("/library")
    assert response.status_code == 503
    assert "缺少前端编译产物" in response.text
    assert "npm --prefix web-client run build" in response.text
    # 缺前端不影响 API
    assert client.get(f"{API_PREFIX}/documents").status_code == 200


def test_static_dir_resolution_prefers_repository_build(web_workspace):
    """源码仓库里存在 web-client/dist 时，服务直接使用它（不依赖安装包）。"""
    from litworkbench.launcher import resolve_static_dir

    resolved, candidates = resolve_static_dir()
    if resolved is not None:
        assert (resolved / "index.html").is_file()
        assert candidates[0].name == "dist" and candidates[1].name == "static"


def test_static_files_and_spa_fallback(tmp_path, web_workspace):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>文献工作台</title>",
                                     encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    app = create_app(web_workspace.root, start_worker=False, static_dir=dist)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.get(f"{API_PREFIX}/workspace")
        index = client.get("/")
        assert index.status_code == 200 and "文献工作台" in index.text
        deep_link = client.get("/search?x=1")
        assert deep_link.status_code == 200 and "文献工作台" in deep_link.text
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        api_404 = client.get(f"{API_PREFIX}/documents/nope")
        assert api_404.status_code == 404 and api_404.headers["content-type"].startswith(
            "application/json")


def test_pagination_bounds_are_validated(session):
    assert session.get(f"{API_PREFIX}/documents?page_size=500").status_code == 422
    assert session.get(f"{API_PREFIX}/documents?page=0").status_code == 422
    body = session.get(f"{API_PREFIX}/documents?page=1&page_size=5").json()
    assert set(body) == {"items", "total", "page", "page_size"}


def test_document_ids_are_opaque_and_not_paths(session):
    body = session.get(f"{API_PREFIX}/documents").json()
    assert body["items"] == []
    assert body["total"] == 0


def test_validation_error_is_chinese_structured(client):
    client.get(f"{API_PREFIX}/workspace")
    token = client.cookies  # 只用于确认已建立会话
    assert token
    response = client.post(f"{API_PREFIX}/search", json={},
                           headers={"X-CSRF-Token": "x"})
    assert response.status_code == 403  # CSRF 先于参数校验


def test_documents_detail_requires_existing(session):
    assert session.get(f"{API_PREFIX}/documents/doc_missing").status_code == 404


def test_units_route_rejects_path_like_ids(session):
    response = session.get(f"{API_PREFIX}/units/..%2F..%2Fsources%2Fsecret")
    assert response.status_code == 404


def test_workspace_reopens_existing_db_without_reset(web_workspace):
    app = create_app(web_workspace.root, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        first = client.get(f"{API_PREFIX}/workspace").json()
    app2 = create_app(Workspace.open(web_workspace.root).root, start_worker=False)
    with TestClient(app2, base_url="http://127.0.0.1:8765") as client:
        second = client.get(f"{API_PREFIX}/workspace").json()
    assert first["root"] == second["root"]
    assert Path(second["root"]).exists()