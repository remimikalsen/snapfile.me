import pytest
from aiohttp import web

from app.app import security_headers_middleware, HTTPS_ONLY, ANALYTICS_SCRIPT_CSP


@pytest.fixture
def minimal_app():
    # Create a minimal aiohttp app that uses only the security middleware.
    app = web.Application(middlewares=[security_headers_middleware])

    # Remove Server header using signal handler (same as in create_app)
    # This ensures the header is removed even if aiohttp adds it after middleware runs
    async def on_response_prepare(request, response):
        # Remove Server header that aiohttp automatically adds
        server_keys = [key for key in response.headers.keys() if key.lower() == "server"]
        for key in server_keys:
            del response.headers[key]

    app.on_response_prepare.append(on_response_prepare)

    # Add a simple route that returns a basic response.
    async def handler(request):
        return web.Response(text="Hello, World!")

    app.router.add_get("/", handler)
    return app


@pytest.mark.asyncio
async def test_security_headers(aiohttp_client, minimal_app):
    # Create a test client using the minimal app.
    client = await aiohttp_client(minimal_app)
    resp = await client.get("/")

    # Verify that the security middleware has added the expected headers.

    # Check the Content Security Policy header.
    csp = resp.headers.get("Content-Security-Policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    # Verify that CSP uses nonces instead of unsafe-inline for scripts
    assert "'nonce-" in csp, "CSP should use nonces for script-src"
    # Verify script-src does not use unsafe-inline
    script_src_directive = [d for d in csp.split(";") if "script-src" in d]
    if script_src_directive:
        assert "'unsafe-inline'" not in script_src_directive[0], "CSP script-src should not use unsafe-inline"
    # Also verify that your analytics script CSP, if provided, appears.
    if ANALYTICS_SCRIPT_CSP:
        assert ANALYTICS_SCRIPT_CSP in csp

    # Check the X-Content-Type-Options header.
    xcto = resp.headers.get("X-Content-Type-Options")
    assert xcto == "nosniff"

    # Check the X-Frame-Options header.
    xfo = resp.headers.get("X-Frame-Options")
    assert xfo == "SAMEORIGIN"

    # Check the Referrer-Policy header.
    rp = resp.headers.get("Referrer-Policy")
    assert rp == "same-origin"

    # Check the Permissions-Policy header.
    pp = resp.headers.get("Permissions-Policy")
    assert pp is not None, "Permissions-Policy header should be present"
    assert "fullscreen=(self)" in pp, "Permissions-Policy should allow fullscreen for self"
    assert "geolocation=()" in pp, "Permissions-Policy should restrict geolocation"

    # Verify Server header is not present (security best practice)
    assert "Server" not in resp.headers, "Server header should be removed to avoid information disclosure"

    # If HTTPS_ONLY is True, the Strict-Transport-Security header should be set.
    if HTTPS_ONLY:
        hsts = resp.headers.get("Strict-Transport-Security")
        assert hsts is not None
    else:
        # Otherwise, it should not be present.
        assert "Strict-Transport-Security" not in resp.headers
