"""Tests for the command line (raw body PUT) upload endpoint."""

import asyncio

import pytest_asyncio
from aiohttp.test_utils import make_mocked_request

import app.app as snapfile


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch, aiohttp_client):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(snapfile, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(snapfile, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(snapfile, "MAX_USES_QUOTA", 3)
    monkeypatch.setattr(snapfile, "MAX_FILE_SIZE", 1024)
    monkeypatch.setattr(snapfile, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_COUNT", 1)
    app = await snapfile.create_app(purge_interval_minutes=60, consistency_check_interval_minutes=60)
    test_client = await aiohttp_client(app)
    test_client.upload_dir = upload_dir
    return test_client


async def _quota_left(client):
    resp = await client.get("/check-limit")
    return (await resp.json())["quota_left"]


def _link(body):
    return body.splitlines()[0]


async def test_put_returns_absolute_single_use_link(client):
    resp = await client.put("/notes.txt", data=b"from the shell")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    body = await resp.text()
    lines = body.splitlines()
    assert len(lines) == 2 and body.endswith("\n")
    url = lines[0]
    assert url.startswith(f"http://{client.host}:{client.port}/download/")
    assert lines[1].startswith("You have 2 uploads left. Quota resets in ")
    assert "minutes." in lines[1]
    code = url.rsplit("/", 1)[1]
    assert snapfile.validate_download_code(code)
    assert resp.headers["X-Landing-Url"] == url.replace("/download/", "/landing/download/")
    assert await _quota_left(client) == 2

    download = await client.get(f"/download/{code}")
    assert download.status == 200
    assert download.headers["Content-Disposition"] == 'attachment; filename="notes.txt"'
    assert await download.read() == b"from the shell"

    # Single use: the file is gone after the grace period.
    await asyncio.sleep(5.5)
    assert (await client.get(f"/download/{code}")).status == 404


async def test_put_to_root_uses_fallback_filename(client):
    resp = await client.put("/", data=b"stdin data")
    assert resp.status == 200
    code = _link(await resp.text()).rsplit("/", 1)[1]
    download = await client.get(f"/download/{code}")
    assert download.headers["Content-Disposition"] == 'attachment; filename="file"'


async def test_put_filename_is_sanitised(client):
    resp = await client.put("/..%2F..%2Fetc%2Fpasswd", data=b"nope")
    assert resp.status == 200
    stored = [p.name for p in client.upload_dir.iterdir()]
    assert len(stored) == 1
    assert stored[0].endswith("_etc_passwd")
    assert ".." not in stored[0]


async def test_put_oversize_is_rejected_before_reading_body(client):
    resp = await client.put("/big.bin", data=b"x" * 2048)
    assert resp.status == 413
    assert await _quota_left(client) == 3
    assert list(client.upload_dir.iterdir()) == []


async def test_put_oversize_with_expect_continue_is_rejected(client):
    resp = await client.put("/big.bin", data=b"x" * 2048, expect100=True)
    assert resp.status == 413
    assert await _quota_left(client) == 3
    assert list(client.upload_dir.iterdir()) == []


async def test_put_with_expect_continue_succeeds(client):
    resp = await client.put("/ok.bin", data=b"y" * 100, expect100=True)
    assert resp.status == 200
    assert await _quota_left(client) == 2


async def test_put_chunked_oversize_leaves_no_file_and_releases_quota(client):
    async def body():
        yield b"x" * 1000
        yield b"x" * 1000

    resp = await client.put("/streamed.bin", data=body())
    assert resp.status == 400
    assert await _quota_left(client) == 3
    assert list(client.upload_dir.iterdir()) == []


async def test_put_respects_quota(client):
    for i in range(3):
        assert (await client.put(f"/f{i}", data=b"a")).status == 200
    resp = await client.put("/f4", data=b"a")
    assert resp.status == 429
    assert "exceeded" in await resp.text()
    assert len(list(client.upload_dir.iterdir())) == 3


async def test_put_cross_site_is_rejected(client):
    resp = await client.put("/x.txt", data=b"a", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403
    assert await _quota_left(client) == 3


async def test_put_link_honours_forwarded_headers_from_trusted_proxy(client):
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "snapfile.example"}
    resp = await client.put("/a.txt", data=b"a", headers=headers)
    assert _link(await resp.text()).startswith("https://snapfile.example/download/")


async def test_put_link_ignores_forwarded_headers_without_trusted_proxy(client, monkeypatch):
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_COUNT", 0)
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"}
    resp = await client.put("/a.txt", data=b"a", headers=headers)
    assert (await resp.text()).startswith(f"http://{client.host}:{client.port}/download/")


async def test_put_link_uses_public_base_url_when_configured(client, monkeypatch):
    monkeypatch.setattr(snapfile, "PUBLIC_BASE_URL", "https://snapfile.me")
    resp = await client.put("/a.txt", data=b"a")
    assert (await resp.text()).startswith("https://snapfile.me/download/")


def test_public_base_url_rejects_garbage_host(monkeypatch):
    monkeypatch.setattr(snapfile, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_COUNT", 0)
    monkeypatch.setattr(snapfile, "HTTPS_ONLY", False)
    request = make_mocked_request("PUT", "/a", headers={"Host": "bad host/../x"})
    assert snapfile.public_base_url(request) == ""
    request = make_mocked_request("PUT", "/a", headers={"Host": "snapfile.me:8443"})
    assert snapfile.public_base_url(request) == "http://snapfile.me:8443"
    monkeypatch.setattr(snapfile, "HTTPS_ONLY", True)
    assert snapfile.public_base_url(request) == "https://snapfile.me:8443"


async def test_front_page_shows_cli_helper(client):
    resp = await client.get("/")
    html = await resp.text()
    assert "curl -T" in html
    assert f"http://{client.host}:{client.port}/" in html
