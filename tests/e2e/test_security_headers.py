import pytest
from playwright.sync_api import Page


@pytest.mark.e2e
def test_security_headers(page: Page, base_url: str):

    # Capture the response returned by page.goto.
    response = page.goto(base_url, timeout=10000)

    # Use the response to get headers.
    headers = response.headers

    # Check that the Content Security Policy header is present and includes expected directives.
    csp = headers.get("content-security-policy")
    assert csp is not None, "Content-Security-Policy header is missing"
    assert (
        "default-src 'self'" in csp
    ), "CSP does not include expected default-src directive"

    # Verify other security headers.
    assert (
        headers.get("x-content-type-options") == "nosniff"
    ), "X-Content-Type-Options header is not set to nosniff"
    assert (
        headers.get("x-frame-options") == "SAMEORIGIN"
    ), "X-Frame-Options header is not set to SAMEORIGIN"
    assert (
        headers.get("referrer-policy") == "same-origin"
    ), "Referrer-Policy header is not set to same-origin"

    # Check for HSTS if HTTPS_ONLY is enabled in your app.
    if "strict-transport-security" in headers:
        hsts = headers.get("strict-transport-security")
        assert (
            hsts is not None
        ), "Strict-Transport-Security header should be present when HTTPS_ONLY is enabled"
