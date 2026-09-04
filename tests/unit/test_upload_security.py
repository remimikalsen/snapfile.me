"""Behavioural tests for the upload/download hardening in app.py."""

import asyncio
import os
import time

import pytest
import pytest_asyncio
from aiohttp import FormData
from aiohttp.test_utils import make_mocked_request

import app.app as snapfile

DUMMY_CODE = "A" * 12


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch, aiohttp_client):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(snapfile, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(snapfile, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(snapfile, "MAX_USES_QUOTA", 3)
    monkeypatch.setattr(snapfile, "MAX_FILE_SIZE", 1024)
    app = await snapfile.create_app(purge_interval_minutes=60, consistency_check_interval_minutes=60)
    test_client = await aiohttp_client(app)
    test_client.upload_dir = upload_dir
    return test_client


def _form(content=b"hello", filename="hello.txt"):
    form = FormData()
    form.add_field("file", content, filename=filename)
    return form


async def _quota_left(client):
    resp = await client.get("/check-limit")
    assert resp.status == 200
    return (await resp.json())["quota_left"]


async def test_upload_then_download_roundtrip(client):
    resp = await client.post("/upload", data=_form(b"round trip"))
    assert resp.status == 200
    path = await resp.text()
    assert path.startswith("/download/")
    assert await _quota_left(client) == 2

    resp = await client.get(path)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/octet-stream"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="hello.txt"'
    assert resp.headers["Cache-Control"] == "no-store"
    assert await resp.read() == b"round trip"


async def test_uploaded_html_is_never_served_as_html(client):
    resp = await client.post("/upload", data=_form(b"<script>alert(1)</script>", "evil.html"))
    assert resp.status == 200
    resp = await client.get(await resp.text())
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/octet-stream"


async def test_unknown_or_malformed_download_code_is_404(client):
    for code in ("short", DUMMY_CODE, "../../etc/passwd"):
        resp = await client.get(f"/download/{code}")
        assert resp.status == 404, code
    resp = await client.get(f"/landing/download/{DUMMY_CODE}")
    assert resp.status == 404
    resp = await client.get(f"/time-left/{DUMMY_CODE}")
    assert resp.status == 404


async def test_cross_site_upload_is_rejected_without_consuming_quota(client):
    resp = await client.post("/upload", data=_form(), headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403
    assert await _quota_left(client) == 3
    assert list(client.upload_dir.iterdir()) == []


async def test_same_origin_upload_is_accepted(client):
    resp = await client.post("/upload", data=_form(), headers={"Sec-Fetch-Site": "same-origin"})
    assert resp.status == 200


async def test_part_without_filename_is_400_not_500(client):
    boundary = "snapfiletestboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"\r\n\r\n'
        "no filename on this part\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    resp = await client.post("/upload", data=body, headers=headers)
    assert resp.status == 400
    assert await _quota_left(client) == 3
    assert list(client.upload_dir.iterdir()) == []


async def test_oversize_upload_leaves_no_file_and_releases_quota(client):
    resp = await client.post("/upload", data=_form(b"x" * 2048))
    assert resp.status == 413
    assert await _quota_left(client) == 3
    assert list(client.upload_dir.iterdir()) == []


async def test_aborted_upload_leaves_no_file_and_releases_quota(client):
    boundary = "snapfiletestboundary"

    async def body():
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="partial.bin"\r\n\r\n'
        ).encode()
        yield b"some bytes then the client dies"
        raise ConnectionError("client went away")

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    with pytest.raises(Exception):
        await client.post("/upload", data=body(), headers=headers)

    # The handler finishes asynchronously after the connection drops.
    for _ in range(50):
        if not list(client.upload_dir.iterdir()) and await _quota_left(client) == 3:
            break
        await asyncio.sleep(0.1)
    assert list(client.upload_dir.iterdir()) == []
    assert await _quota_left(client) == 3


async def test_quota_is_enforced_under_concurrent_uploads(client):
    responses = await asyncio.gather(
        *(client.post("/upload", data=_form(b"c", f"f{i}.txt")) for i in range(10))
    )
    statuses = sorted(r.status for r in responses)
    assert statuses.count(200) == 3
    assert statuses.count(429) == 7
    assert len(list(client.upload_dir.iterdir())) == 3
    assert await _quota_left(client) == 0


async def test_csp_contains_hardening_directives(client):
    resp = await client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    for directive in ("object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'self'"):
        assert directive in csp, directive


async def test_consistency_check_spares_in_progress_uploads(client):
    fresh = client.upload_dir / "fresh_upload_in_progress.bin"
    fresh.write_bytes(b"streaming")
    stale = client.upload_dir / "stale_orphan.bin"
    stale.write_bytes(b"leftover")
    two_hours_ago = time.time() - 2 * 60 * 60
    os.utime(stale, (two_hours_ago, two_hours_ago))

    await snapfile.check_database_file_consistency()

    assert fresh.exists()
    assert not stale.exists()


def _hashed_ip_for(headers, trusted_proxies, monkeypatch):
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_COUNT", trusted_proxies)
    request = make_mocked_request("GET", "/", headers=headers)
    return snapfile.get_client_ip(request)


def test_x_forwarded_for_only_trusts_configured_proxy_hops(monkeypatch):
    spoofed_chain = {"X-Forwarded-For": "1.1.1.1, 9.9.9.9"}
    # One trusted proxy: the rightmost entry is what the proxy saw; the left one is attacker-supplied.
    assert _hashed_ip_for(spoofed_chain, 1, monkeypatch) == snapfile.hash_ip("9.9.9.9")
    # Two trusted proxies: step one further left.
    assert _hashed_ip_for(spoofed_chain, 2, monkeypatch) == snapfile.hash_ip("1.1.1.1")
    # Header disabled: the socket peer is used regardless of the header.
    direct = make_mocked_request("GET", "/")
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_COUNT", 0)
    assert snapfile.get_client_ip(direct) == snapfile.hash_ip(direct.remote or "")
    assert _hashed_ip_for(spoofed_chain, 0, monkeypatch) not in (
        snapfile.hash_ip("1.1.1.1"),
        snapfile.hash_ip("9.9.9.9"),
    )


def test_access_log_reports_resolved_client_ip(monkeypatch, caplog):
    import logging

    from aiohttp import web

    logger = logging.getLogger("test.access")
    access_logger = snapfile.ClientIPAccessLogger(logger, "")
    # Two proxies: the outer one saw the client, the inner one saw the outer proxy.
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_COUNT", 2)
    request = make_mocked_request(
        "GET",
        "/check-limit",
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.2", "User-Agent": "UA/1"},
    )
    response = web.Response(status=200, text="ok")

    with caplog.at_level(logging.INFO, logger="test.access"):
        access_logger.log(request, response, 0.01)

    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert line.startswith("203.0.113.7 [")
    assert '"GET /check-limit HTTP/1.1" 200' in line
    assert line.endswith('"-" "UA/1"')
    # Neither the inner proxy's hop nor anything else from the chain leaks in.
    assert "10.0.0.2" not in line


def test_ip_usage_duplicates_are_collapsed_on_init(tmp_path, monkeypatch):
    import sqlite3

    db_file = tmp_path / "legacy.db"
    monkeypatch.setattr(snapfile, "DATABASE_PATH", str(db_file))
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE ip_usage (ip TEXT, uses INTEGER, last_access DATETIME)")
    conn.executemany(
        "INSERT INTO ip_usage VALUES (?, ?, ?)",
        [("dup", 1, "2026-01-01T00:00:00"), ("dup", 2, "2026-01-01T00:00:01")],
    )
    conn.commit()
    conn.close()

    asyncio.run(snapfile.init_db())

    conn = sqlite3.connect(db_file)
    rows = conn.execute("SELECT uses FROM ip_usage WHERE ip='dup'").fetchall()
    conn.close()
    assert rows == [(2,)]
