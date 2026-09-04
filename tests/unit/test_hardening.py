"""Tests for the hardening introduced by the September 2026 security audit."""

import collections

import pytest
import pytest_asyncio
from aiohttp import FormData

import app.app as snapfile


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch, aiohttp_client):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(snapfile, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(snapfile, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(snapfile, "MAX_USES_QUOTA", 5)
    monkeypatch.setattr(snapfile, "MAX_FILE_SIZE", 1024)
    monkeypatch.setattr(snapfile, "INTERNAL_IP", "")
    monkeypatch.setattr(snapfile, "_upload_semaphore", None)
    app = await snapfile.create_app(purge_interval_minutes=60, consistency_check_interval_minutes=60)
    test_client = await aiohttp_client(app)
    test_client.upload_dir = upload_dir
    return test_client


def _form(content=b"hello", filename="hello.txt"):
    form = FormData()
    form.add_field("file", content, filename=filename)
    return form


async def _upload(client, **kwargs):
    resp = await client.post("/upload", data=_form(**kwargs))
    assert resp.status == 200, await resp.text()
    return (await resp.text()).rsplit("/", 1)[1]


# --- single-use claim ---------------------------------------------------------------


async def test_second_download_of_same_code_is_refused(client):
    code = await _upload(client)
    first = await client.get(f"/download/{code}")
    assert first.status == 200
    assert await first.read() == b"hello"
    second = await client.get(f"/download/{code}")
    assert second.status == 404
    landing = await client.get(f"/landing/download/{code}")
    assert landing.status == 404


async def test_landing_page_is_not_cacheable(client):
    code = await _upload(client)
    resp = await client.get(f"/landing/download/{code}")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert "direct IP download" not in await resp.text()  # INTERNAL_IP unset


# --- quota key normalisation -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("203.0.113.9", "203.0.113.9"),
        ("2001:db8:abcd:1234:aaaa:bbbb:cccc:dddd", "2001:db8:abcd:1234::/64"),
        ("2001:db8:abcd:1234::1", "2001:db8:abcd:1234::/64"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("not-an-ip", "not-an-ip"),
    ],
)
def test_normalize_ip(raw, expected):
    assert snapfile.normalize_ip(raw) == expected


def test_ipv6_addresses_in_same_64_share_a_quota_key():
    a = snapfile.hash_ip(snapfile.normalize_ip("2001:db8:1:2::1"))
    b = snapfile.hash_ip(snapfile.normalize_ip("2001:db8:1:2:ffff:ffff:ffff:ffff"))
    c = snapfile.hash_ip(snapfile.normalize_ip("2001:db8:1:3::1"))
    assert a == b
    assert a != c


# --- file names -------------------------------------------------------------------------


def test_truncate_filename_keeps_extension():
    name = "a" * 300 + ".tar.gz"
    short = snapfile.truncate_filename(name)
    assert len(short) <= snapfile.MAX_FILENAME_LENGTH
    assert short.endswith(".gz")
    assert snapfile.truncate_filename("short.txt") == "short.txt"
    assert len(snapfile.truncate_filename("x" * 400)) == snapfile.MAX_FILENAME_LENGTH


async def test_over_long_filename_is_accepted_and_shortened(client):
    code = await _upload(client, filename="b" * 300 + ".txt")
    resp = await client.get(f"/landing/download/{code}")
    assert resp.status == 200
    stored = [p.name for p in client.upload_dir.iterdir()]
    assert len(stored) == 1
    assert len(stored[0]) < 200


# --- cross-site protection -----------------------------------------------------------------


async def test_origin_mismatch_is_rejected(client):
    resp = await client.post("/upload", data=_form(), headers={"Origin": "https://evil.example"})
    assert resp.status == 403


async def test_null_origin_is_rejected(client):
    resp = await client.post("/upload", data=_form(), headers={"Origin": "null"})
    assert resp.status == 403


async def test_matching_origin_is_allowed(client):
    origin = f"http://{client.host}:{client.port}"
    resp = await client.post("/upload", data=_form(), headers={"Origin": origin})
    assert resp.status == 200


# --- storage protection ----------------------------------------------------------------------


async def test_upload_refused_when_disk_reserve_would_be_breached(client, monkeypatch):
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(snapfile.shutil, "disk_usage", lambda path: usage(1, 1, 10))
    resp = await client.post("/upload", data=_form())
    assert resp.status == 507
    quota = await (await client.get("/check-limit")).json()
    assert quota["quota_left"] == 5  # a refused upload costs nothing


async def test_upload_refused_when_storage_budget_exhausted(client, monkeypatch):
    monkeypatch.setattr(snapfile, "STORAGE_BUDGET_BYTES", 1500)
    await _upload(client)  # 5 bytes stored; the next maximum-size upload would exceed 1500
    monkeypatch.setattr(snapfile, "STORAGE_BUDGET_BYTES", 1000)
    resp = await client.post("/upload", data=_form())
    assert resp.status == 507


async def test_oversized_content_length_is_refused_early(client):
    resp = await client.post(
        "/upload", data=_form(), headers={"Content-Length": str(5 * 1024 * 1024)}
    )
    assert resp.status == 413


# --- proxy trust -------------------------------------------------------------------------------


async def test_forwarded_headers_ignored_from_untrusted_peer(client, monkeypatch):
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_COUNT", 1)
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_IPS", ["10.0.0.0/8"])
    from aiohttp.test_utils import make_mocked_request

    req = make_mocked_request("GET", "/", headers={"X-Forwarded-For": "1.2.3.4"})
    req._transport_peername = ("192.0.2.1", 1234)
    assert snapfile.proxy_is_trusted(req) is False
    assert snapfile.resolve_client_ip(req) == "192.0.2.1"
    monkeypatch.setattr(snapfile, "TRUSTED_PROXY_IPS", ["192.0.2.0/24"])
    assert snapfile.proxy_is_trusted(req) is True
    assert snapfile.resolve_client_ip(req) == "1.2.3.4"


# --- headers, health, analytics config ------------------------------------------------------


async def test_csp_has_no_unsafe_inline_styles_or_font_hosts(client):
    resp = await client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    assert "style-src 'self';" in csp
    assert "font-src 'self';" in csp
    assert "'unsafe-inline'" not in csp
    assert "googleapis" not in csp


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status == 204


def test_csp_origin_validation():
    assert snapfile.validate_csp_origin("https://plausible.example") == "https://plausible.example"
    assert snapfile.validate_csp_origin("https://plausible.example:8443") == "https://plausible.example:8443"
    assert snapfile.validate_csp_origin("https://a.example 'unsafe-inline'") == ""
    assert snapfile.validate_csp_origin("http://a.example") == ""
    assert snapfile.validate_csp_origin("") == ""


def test_default_analytics_allowlist_has_no_tag_managers():
    joined = ",".join(snapfile.ALLOWED_ANALYTICS_DOMAINS)
    assert "googletagmanager" not in joined
    assert "google-analytics" not in joined
