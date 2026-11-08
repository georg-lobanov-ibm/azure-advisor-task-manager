"""What a request must prove before a handler runs, and the headers every response carries."""

import pytest

from advisor_agent.web import security
from advisor_agent.web.app import STATIC_FILES
from fakes.web_harness import ORIGIN, PORT, TOKEN

LINK = {"recommendation_type_id": "t1", "resource_id": "/r/1", "jira_key": "ABC-1"}


def test_every_response_carries_the_security_headers(web):
    with web() as app:
        responses = [
            app.get("/"),
            app.get("/static/app.js"),
            app.get("/api/session"),  # signed in
            app.get("/api/nothing-here"),  # 404
            app.client.get("/", headers={"Host": "evil.example"}),  # refused
        ]

    for response in responses:
        for name, value in security.SECURITY_HEADERS:
            assert response.headers[name.decode()] == value.decode(), (response.url, name)
    assert "script-src 'self'" in responses[0].headers["content-security-policy"]
    assert "require-trusted-types-for 'script'" in responses[0].headers["content-security-policy"]


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "127.0.0.1:1", f"evil.example:{PORT}", ""])
def test_a_wrong_host_is_refused(web, host):
    with web() as app:
        response = app.client.get("/api/session", headers={"Host": host})

    assert response.status_code == 400


def test_localhost_is_an_accepted_host(web):
    with web() as app:
        response = app.client.get("/api/session", headers={"Host": f"localhost:{PORT}"})

    assert response.status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.example", f"http://127.0.0.1:{PORT + 1}", "null", f"https://127.0.0.1:{PORT}"])
def test_a_foreign_origin_is_refused_even_on_a_read(web, origin):
    with web() as app:
        read = app.client.get("/api/session", headers={"Origin": origin})
        write = app.post("/api/chat/reset", headers={"Origin": origin})

    assert read.status_code == 403
    assert write.status_code == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_cross_site_fetch_is_refused(web, site):
    with web() as app:
        response = app.client.get("/api/session", headers={"Sec-Fetch-Site": site})

    assert response.status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_same_origin_fetch_metadata_is_accepted(web, site):
    with web() as app:
        response = app.client.get("/api/session", headers={"Sec-Fetch-Site": site})

    assert response.status_code == 200


def test_a_change_needs_our_origin(web):
    with web() as app:
        # No Origin header at all (a plain script or curl, not a page of ours).
        response = app.client.request("PUT", "/api/links", json=LINK)

    assert response.status_code == 403
    assert "origin" in response.json()["error"].lower()


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""])
def test_a_change_must_be_json(web, content_type):
    with web() as app:
        response = app.client.request(
            "PUT",
            "/api/links",
            content=b'{"jira_key": "ABC-1"}',
            headers={"Origin": ORIGIN, "Content-Type": content_type} if content_type else {"Origin": ORIGIN},
        )

    assert response.status_code == 415


def test_json_with_a_charset_is_accepted(web):
    with web() as app:
        response = app.client.request(
            "POST",
            "/api/chat/reset",
            content=b"{}",
            headers={"Origin": ORIGIN, "Content-Type": "application/json; charset=utf-8"},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/session"),
        ("GET", "/api/events"),
        ("GET", "/api/subscriptions"),
        ("GET", "/api/recommendations"),
        ("GET", "/api/predefined"),
        ("GET", "/api/links"),
        ("PUT", "/api/links"),
        ("DELETE", "/api/links"),
        ("POST", "/api/chat"),
        ("POST", "/api/chat/cancel"),
        ("POST", "/api/chat/reset"),
        ("POST", "/api/approvals/abc"),
        ("POST", "/api/azure/retry"),
    ],
)
def test_every_api_route_needs_the_session_cookie(web, method, path):
    with web(signed_in=False) as app:
        response = app.send(method, path, {"decision": "approve"})

    assert response.status_code == 401


def test_a_wrong_cookie_is_refused(web):
    with web(signed_in=False) as app:
        app.client.cookies.set("advisor_session", "not-the-secret")

        assert app.get("/api/session").status_code == 401


def test_the_launch_token_is_not_a_session_cookie(web):
    with web(signed_in=False) as app:
        app.client.cookies.set("advisor_session", TOKEN)

        assert app.get("/api/session").status_code == 401


def test_the_page_and_scripts_need_no_sign_in(web):
    with web(signed_in=False) as app:
        assert app.get("/").status_code == 200
        for name in STATIC_FILES:
            assert app.get(f"/static/{name}").status_code == 200


def test_login_is_open_but_still_checks_origin_and_type(web):
    with web(signed_in=False) as app:
        no_origin = app.client.post("/api/login", json={"token": TOKEN})
        wrong_type = app.client.post(
            "/api/login", content=b"x", headers={"Origin": ORIGIN, "Content-Type": "text/plain"}
        )
        foreign = app.post("/api/login", {"token": TOKEN}, headers={"Origin": "http://evil.example"})

    assert (no_origin.status_code, wrong_type.status_code, foreign.status_code) == (403, 415, 403)


@pytest.mark.parametrize("body", [None, {}, {"token": ""}, {"token": 5}, {"token": ["x"]}, {"token": "x" * 1000}])
def test_login_refuses_anything_but_the_token(web, body):
    with web(signed_in=False) as app:
        response = app.post("/api/login", body)

    assert response.status_code == 401
    assert "set-cookie" not in response.headers


def test_an_oversized_body_is_refused_before_it_is_read(web):
    with web() as app:
        response = app.client.request(
            "PUT",
            "/api/links",
            content=b"x" * (security.MAX_BODY_BYTES + 1),
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )

    assert response.status_code == 413


def test_a_streamed_body_over_the_limit_is_refused_while_it_is_read(web):
    """No Content-Length (chunked), so only the handler's own size limit can stop it."""

    def chunks():
        for _ in range(8):
            yield b"x" * 10_000

    with web() as app:
        request = app.client.build_request(
            "PUT", "/api/links", content=chunks(), headers={"Origin": ORIGIN, "Content-Type": "application/json"}
        )
        assert "content-length" not in request.headers
        response = app.client.send(request)

    assert response.status_code == 413


def test_a_bad_content_length_is_a_400(web):
    with web() as app:
        response = app.client.request(
            "PUT",
            "/api/links",
            content=b"{}",
            headers={"Origin": ORIGIN, "Content-Type": "application/json", "Content-Length": "abc"},
        )

    assert response.status_code == 400


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"', b"123", b"null"])
def test_a_body_that_is_not_a_json_object_is_a_400(web, body):
    with web() as app:
        response = app.client.request(
            "POST", "/api/chat/reset", content=body, headers={"Origin": ORIGIN, "Content-Type": "application/json"}
        )

    assert response.status_code == 400


def test_a_websocket_is_refused(web):
    from starlette.websockets import WebSocketDisconnect

    with web() as app:
        with pytest.raises(WebSocketDisconnect):
            with app.client.websocket_connect("/api/events"):
                pass


def test_an_unexpected_error_is_a_500_with_the_headers_and_no_detail(web, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("the disk said no")

    with web() as app:
        monkeypatch.setattr(app.services.storage.db, "get_all_mappings", boom)

        response = app.get("/api/links")

    assert response.status_code == 500
    assert response.headers["content-security-policy"] == security.CONTENT_SECURITY_POLICY
    # The class and a short message reach the page; the traceback does not.
    assert response.json()["error"] == "RuntimeError: the disk said no"
    assert "Traceback" not in response.text


def test_unknown_static_files_are_a_404(web):
    with web() as app:
        for name in ["../app.py", "..%2fapp.py", "index.html", "nothing.js", "app.js.map"]:
            assert app.get(f"/static/{name}").status_code == 404, name


def test_static_files_have_the_right_types(web):
    with web() as app:
        js = app.get("/static/app.js")
        css = app.get("/static/app.css")

    assert js.headers["content-type"].startswith("text/javascript")
    assert css.headers["content-type"].startswith("text/css")


def test_token_matches_is_strict():
    assert security.token_matches("abc", "abc")
    assert not security.token_matches("abd", "abc")
    assert not security.token_matches("", "")
    assert not security.token_matches(None, "abc")
    assert not security.token_matches(["abc"], "abc")
    assert not security.token_matches("é" * 200, "abc")
