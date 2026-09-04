import asyncio
import ipaddress
import logging
import mimetypes
import os
import shutil
import uuid
import secrets
import string
import hmac
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
import xml.etree.ElementTree as ET  # nosec B405 - used only to build the sitemap, never to parse XML

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from yarl import URL
import aiohttp_jinja2
import jinja2
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import sqlite3
import aiosqlite
import aiofiles
import aiofiles.os
from werkzeug.utils import secure_filename

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Determine the application version from file
VERSION_FILE_PATH = os.path.join(APP_DIR, "VERSION")
VERSION = "Development"
if os.path.isfile(VERSION_FILE_PATH):
    with open(VERSION_FILE_PATH, "r") as version_file:
        VERSION = version_file.read().strip() or "Development"
else:
    parent_dir_version_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
    if os.path.isfile(parent_dir_version_path):
        with open(parent_dir_version_path, "r") as version_file:
            VERSION = version_file.read().strip() + "-development"
    else:
        VERSION = "unknown"

# Load configuration from environment variables
HTTPS_ONLY = os.getenv("HTTPS_ONLY", "false").lower() == "true"  # Default to False
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 500 * 1024 * 1024))  # Default to 500 MB
MAX_USES_QUOTA = int(os.getenv("MAX_USES_QUOTA", 10))  # Default to 5 uploads per day
FILE_EXPIRY_MINUTES = int(
    os.getenv("FILE_EXPIRY_MINUTES", 1440)
)  # Default to 1440 minutes (24 hours)
QUOTA_RENEWAL_MINUTES = int(
    os.getenv("QUOTA_RENEWAL_MINUTES", 60)
)  # Default to 60 minutes (1 hour)
PURGE_INTERVAL_MINUTES = int(os.getenv("PURGE_INTERVAL_MINUTES", 5))  # Cleanup every 5 minutes
CONSISTENCY_CHECK_INTERVAL_MINUTES = int(os.getenv("CONSISTENCY_CHECK_INTERVAL_MINUTES", 1440))
# Number of reverse proxies in front of the app that append to X-Forwarded-For.
# 0 disables X-Forwarded-For entirely (use when clients connect directly).
TRUSTED_PROXY_COUNT = int(os.getenv("TRUSTED_PROXY_COUNT", 1))
# Optional list of proxy addresses or CIDR ranges. When set, X-Forwarded-* headers are
# honoured only for connections that come from one of these addresses, so a client that
# reaches the app directly cannot forge its IP.
TRUSTED_PROXY_IPS = [s.strip() for s in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if s.strip()]
# Storage protection: refuse uploads when free disk space would drop below this reserve,
# or when the total stored bytes would exceed the budget (0 = no budget).
MIN_FREE_DISK_BYTES = int(os.getenv("MIN_FREE_DISK_BYTES", 256 * 1024 * 1024))
STORAGE_BUDGET_BYTES = int(os.getenv("STORAGE_BUDGET_BYTES", 0))
# Slow-client protection: uploads in flight at once, and how long to wait for each chunk.
MAX_CONCURRENT_UPLOADS = int(os.getenv("MAX_CONCURRENT_UPLOADS", 20))
UPLOAD_READ_TIMEOUT_SECONDS = int(os.getenv("UPLOAD_READ_TIMEOUT_SECONDS", 60))
# Longest stored file name; longer names are shortened, keeping the extension.
MAX_FILENAME_LENGTH = 150
INTERNAL_IP = os.getenv("INTERNAL_IP", "")
INTERNAL_PORT = os.getenv("INTERNAL_PORT", "")
# Plain HTTP by design: this address is used by clients on the same LAN as the
# server, where TLS is not available. Built once from config, never from request data.
INTERNAL_BASE_URL = f"http://{INTERNAL_IP}:{INTERNAL_PORT}"
ANALYTICS_SCRIPT_RAW = os.getenv("ANALYTICS_SCRIPT", "")
ANALYTICS_SCRIPT_CSP_RAW = os.getenv("ANALYTICS_SCRIPT_CSP", "")

# Allowed analytics script domains. Only cookie-free, anonymised analytics belong here;
# tag managers can load arbitrary further scripts and would void the privacy policy.
ALLOWED_ANALYTICS_DOMAINS = os.getenv(
    "ALLOWED_ANALYTICS_DOMAINS",
    "plausible.remim.com,plausible.io",
).split(",")

# Absolute base URL (e.g. https://snapfile.me) used for links returned to command line
# clients. When empty it is derived from the request (Host / X-Forwarded-* headers).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
# Chunk size used when streaming an upload body to disk.
UPLOAD_CHUNK_SIZE = 64 * 1024

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/app/uploads")
DATABASE_DIR = os.getenv("DATABASE_DIR", "/app/database")
DATABASE_PATH = os.path.join(DATABASE_DIR, "file_links.db")
APP_KEY = "aiohttp_jinja2_environment"
# Files in UPLOAD_DIR that are not in the database are only removed once they have been
# untouched for this long, so an upload that is still streaming to disk is never deleted.
ORPHAN_FILE_GRACE_SECONDS = 60 * 60

logger = logging.getLogger("snapfile")

# Create directories if they don't exist (skip if permission denied, e.g., in CI)
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(DATABASE_DIR, exist_ok=True)
except (PermissionError, OSError):
    # In test environments or CI, directories may not be creatable at import time
    # They will be created when needed or via environment variables
    pass

# --- Adapter and converter for datetime ---


def adapt_datetime_iso(val):
    """Adapt datetime.datetime to timezone-naive ISO 8601 date."""
    return val.isoformat()


sqlite3.register_adapter(datetime, adapt_datetime_iso)


def convert_datetime(val):
    """Convert ISO 8601 datetime to datetime.datetime object."""
    return datetime.fromisoformat(val.decode())


sqlite3.register_converter("DATETIME", convert_datetime)


# --- Helper Functions ---


async def async_isfile(path):
    """Asynchronously check if a file exists."""
    return await asyncio.to_thread(os.path.isfile, path)


async def async_listdir(path):
    """Asynchronously list directory contents."""
    return await asyncio.to_thread(os.listdir, path)


async def remove_file_quietly(path):
    """Delete a file if it exists, logging (not raising) on failure."""
    try:
        await aiofiles.os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove %s: %s", path, exc)


# Keep strong references to fire-and-forget tasks so they are not garbage collected mid-run.
_background_tasks = set()


def spawn_background_task(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def init_db():
    """Initialize the database and create tables if they do not exist."""
    # Ensure database directory exists
    try:
        os.makedirs(DATABASE_DIR, exist_ok=True)
    except (PermissionError, OSError):
        pass  # May fail in test environments, but database will be created in tmp_path
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS files
                             (id TEXT PRIMARY KEY, filename TEXT, path TEXT, download_code TEXT, upload_time DATETIME)"""
        )
        await db.execute("""CREATE TABLE IF NOT EXISTS ip_usage
                             (ip TEXT, uses INTEGER, last_access DATETIME)""")
        # Older databases may hold duplicate rows per IP; collapse them before enforcing
        # uniqueness, which the atomic quota reservation in reserve_upload_slot relies on.
        await db.execute(
            "DELETE FROM ip_usage WHERE rowid NOT IN (SELECT MAX(rowid) FROM ip_usage GROUP BY ip)"
        )
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_usage_ip ON ip_usage (ip)")
        # Stored size per file (used for the storage budget); added to older databases here.
        async with db.execute("PRAGMA table_info(files)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        if "size" not in columns:
            await db.execute("ALTER TABLE files ADD COLUMN size INTEGER DEFAULT 0")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_download_code ON files (download_code)"
        )
        await db.commit()


# --- Utility Functions ---


def validate_and_sanitize_analytics_script(script_html):
    """
    Validate and sanitize analytics script HTML to prevent XSS attacks.

    Only allows:
    - External script tags (with src attribute)
    - Whitelisted domains in src URLs
    - Safe attributes: defer, async, data-* attributes
    - No inline JavaScript

    Returns sanitized script tag or empty string if invalid.
    """
    if not script_html or not script_html.strip():
        return ""

    # Normalize whitespace
    script_html = script_html.strip()

    # Match script tag - handles both <script ...></script> and <script .../>
    # Pattern matches: <script [attributes]>[content]</script> or <script [attributes]/>
    script_pattern = r"<script\s+([^>]*)>(.*?)</script>|<script\s+([^>]*?)\s*/>"
    match = re.search(script_pattern, script_html, re.IGNORECASE | re.DOTALL)

    if not match:
        return ""

    # Get attributes string (either from first match group or third)
    attributes_str = match.group(1) or match.group(3) or ""
    content = match.group(2) or ""

    # Check for inline content - must be empty
    if content.strip():
        return ""  # Inline JavaScript not allowed

    # Extract src attribute value
    src_match = re.search(r'src\s*=\s*["\']([^"\']+)["\']', attributes_str, re.IGNORECASE)
    if not src_match:
        return ""  # Must have src attribute

    src_url = src_match.group(1)

    # Validate URL and extract domain
    try:
        parsed_url = urlparse(src_url)
        if parsed_url.scheme not in ("http", "https"):
            return ""  # Only allow http/https

        domain = parsed_url.netloc.lower()
        if not domain:
            return ""

        # Check if domain is in whitelist
        domain_allowed = False
        for allowed_domain in ALLOWED_ANALYTICS_DOMAINS:
            allowed_domain = allowed_domain.strip().lower()
            if not allowed_domain:
                continue
            # Allow exact match or subdomain match (e.g., subdomain.plausible.remim.com matches plausible.remim.com)
            if domain == allowed_domain or domain.endswith("." + allowed_domain):
                domain_allowed = True
                break

        if not domain_allowed:
            return ""  # Domain not in whitelist
    except Exception:
        return ""  # Invalid URL

    # Collect safe attributes
    safe_attributes = []

    # Extract src attribute first
    safe_attributes.append(f'src="{src_url}"')

    # Extract other attributes
    # Match attributes: key="value" or key='value' or key (boolean attributes)
    attr_pattern = r'(\w+(?:-\w+)*)\s*=\s*["\']([^"\']+)["\']|(\w+(?:-\w+)*)(?=\s|$)'
    for attr_match in re.finditer(attr_pattern, attributes_str, re.IGNORECASE):
        attr_name = (attr_match.group(1) or attr_match.group(3) or "").lower()
        attr_value = attr_match.group(2) or ""

        # Skip src as we've already added it
        if attr_name == "src":
            continue

        # Check if attribute is allowed
        if attr_name in ("defer", "async"):
            # Boolean attributes - add without value
            safe_attributes.append(attr_name)
        elif attr_name.startswith("data-"):
            # Data attributes are allowed (e.g., data-domain)
            # Sanitize value to prevent XSS - remove any HTML/script tags and quotes
            sanitized_value = re.sub(r'[<>"\']', "", attr_value)
            # Get original attribute name (preserve case for data-* attributes)
            orig_attr_name = attr_match.group(1) or attr_match.group(3) or attr_name
            safe_attributes.append(f'{orig_attr_name}="{sanitized_value}"')

    # Build sanitized script tag
    attrs_str = " ".join(safe_attributes)
    sanitized_script = f"<script {attrs_str}></script>"

    return sanitized_script


# Sanitize analytics script
ANALYTICS_SCRIPT = validate_and_sanitize_analytics_script(ANALYTICS_SCRIPT_RAW)


_CSP_ORIGIN_RE = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?")


def validate_csp_origin(value):
    """Accept a single https origin for the CSP allowlist; anything else is dropped."""
    value = (value or "").strip()
    if not value:
        return ""
    if _CSP_ORIGIN_RE.fullmatch(value):
        return value
    logger.warning("ANALYTICS_SCRIPT_CSP ignored: %r is not a single https origin", value)
    return ""


ANALYTICS_SCRIPT_CSP = validate_csp_origin(ANALYTICS_SCRIPT_CSP_RAW)


# --- Context Processors for Templates ---


async def version_context_processor(request):
    # Get nonce from request (set by middleware)
    nonce = request.get("csp_nonce", "")
    # Absolute URLs for social sharing cards (Open Graph requires absolute image URLs).
    base_url = public_base_url(request)
    return {
        "VERSION": VERSION,
        "ANALYTICS_SCRIPT": ANALYTICS_SCRIPT,
        "CSP_NONCE": nonce,
        "BASE_URL": base_url,
        "PAGE_URL": f"{base_url}{request.path}" if base_url else "",
    }


# --- Utility Functions ---


IP_HASH_SALT_PATH = os.path.join(DATABASE_DIR, "ip_hash_salt")
_ip_hash_salt = None


def get_ip_hash_salt():
    """Return the per-instance secret mixed into IP hashes.

    Without a secret, a SHA-256 of an IPv4 address can be reversed in minutes by hashing
    every possible address. The secret is generated once, stored next to the database
    with owner-only permissions, and never leaves the server. If the directory is not
    writable the secret lives in memory only, which still protects the stored hashes.
    """
    global _ip_hash_salt
    if _ip_hash_salt:
        return _ip_hash_salt
    try:
        with open(IP_HASH_SALT_PATH, "r", encoding="ascii") as handle:
            salt = handle.read().strip()
        if len(salt) >= 32:
            _ip_hash_salt = salt
            return salt
    except OSError:
        pass
    salt = secrets.token_hex(32)
    try:
        os.makedirs(DATABASE_DIR, exist_ok=True)
        fd = os.open(IP_HASH_SALT_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(salt)
    except FileExistsError:
        with open(IP_HASH_SALT_PATH, "r", encoding="ascii") as handle:
            salt = handle.read().strip() or salt
    except OSError as exc:
        logger.warning("IP hash salt kept in memory only (%s): %s", IP_HASH_SALT_PATH, exc)
    _ip_hash_salt = salt
    return salt


def hash_ip(ip):
    return hmac.new(get_ip_hash_salt().encode(), ip.encode(), "sha256").hexdigest()


def _parse_trusted_proxy_networks():
    networks = []
    for entry in TRUSTED_PROXY_IPS:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXY_IPS entry %r", entry)
    return networks


def proxy_is_trusted(request):
    """True when X-Forwarded-* headers on this request may be believed.

    With TRUSTED_PROXY_COUNT at zero nothing is trusted. When TRUSTED_PROXY_IPS is set the
    connection must also come from one of those addresses; otherwise a client that bypasses
    the proxy could forge headers.
    """
    if TRUSTED_PROXY_COUNT <= 0:
        return False
    if not TRUSTED_PROXY_IPS:
        return True
    try:
        remote = ipaddress.ip_address(request.remote or "")
    except ValueError:
        return False
    return any(remote in network for network in _parse_trusted_proxy_networks())


def normalize_ip(ip):
    """Quota key for an address: the address itself for IPv4, the /64 network for IPv6.

    Residential IPv6 customers hold a /64, so counting per single address would give an
    attacker 2**64 free identities.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


def resolve_client_ip(request):
    """Determine the client's real IP address.

    X-Forwarded-For is only honoured for TRUSTED_PROXY_COUNT proxies. Each proxy appends
    the address it received the request from, so the real client is that many entries
    from the right; anything further left was supplied by the client and cannot be trusted.
    """
    ip = request.remote or ""
    if proxy_is_trusted(request):
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if hops:
            ip = hops[-TRUSTED_PROXY_COUNT] if len(hops) >= TRUSTED_PROXY_COUNT else hops[0]
    return ip


def get_client_ip(request):
    """Return the client's quota key: the resolved address, normalised and hashed."""
    return hash_ip(normalize_ip(resolve_client_ip(request)))


def truncate_filename(name, limit=MAX_FILENAME_LENGTH):
    """Shorten an over-long file name, keeping a short extension, so the path fits on disk."""
    if len(name) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem and 0 < len(ext) <= 16:
        return stem[: max(1, limit - len(ext) - 1)] + "." + ext
    return name[:limit]


_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:[0-9]{1,5})?$|^\[[0-9A-Fa-f:.]+\](:[0-9]{1,5})?$")


def public_base_url(request):
    """Return the absolute base URL clients should use to reach this instance.

    PUBLIC_BASE_URL wins when configured. Otherwise the URL is rebuilt from the request,
    honouring X-Forwarded-Proto / X-Forwarded-Host only when a trusted proxy is configured.
    Returns an empty string if no sane host can be determined, so callers fall back to
    a relative path.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    scheme = "https" if request.secure else "http"
    host = request.host or ""
    if proxy_is_trusted(request):
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        forwarded_proto = forwarded_proto.split(",")[0].strip().lower()
        if forwarded_proto in ("http", "https"):
            scheme = forwarded_proto
        forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
        if forwarded_host:
            host = forwarded_host
    if HTTPS_ONLY:
        scheme = "https"
    if not _HOST_RE.match(host):
        return ""
    return str(URL.build(scheme=scheme, authority=host))


_DOWNLOAD_CODE_IN_PATH = re.compile(r"(/(?:landing/download|download|time-left)/)[A-Za-z0-9]+")


def redact_download_codes(text):
    """Replace download codes in a path or URL so log lines cannot be tied to a file."""
    return _DOWNLOAD_CODE_IN_PATH.sub(r"\1[code]", text)


class ClientIPAccessLogger(AbstractAccessLogger):
    """aiohttp access logger that reports the resolved client IP.

    The default logger prints the socket peer, which behind a reverse proxy is always
    the proxy itself. This mirrors the default log line but uses the same
    TRUSTED_PROXY_COUNT-aware resolution as the upload quota. Download codes are
    redacted from the path and the referrer, as promised in the privacy policy.
    """

    def log(self, request, response, elapsed):
        if request.path == "/healthz":
            return  # container health probes would otherwise dominate the log
        try:
            started = datetime.now().astimezone() - timedelta(seconds=elapsed)
            self.logger.info(
                '%s [%s] "%s %s HTTP/%s.%s" %s %s "%s" "%s"',
                resolve_client_ip(request) or "-",
                started.strftime("%d/%b/%Y:%H:%M:%S %z"),
                request.method,
                redact_download_codes(request.path_qs),
                request.version.major,
                request.version.minor,
                response.status,
                response.body_length,
                redact_download_codes(request.headers.get("Referer", "-")),
                request.headers.get("User-Agent", "-"),
            )
        except Exception:
            self.logger.exception("Error in logging")


async def ip_reached_quota(ip):
    """Check if the IP has reached its upload quota; reset if the renewal time has passed."""
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute("SELECT uses, last_access FROM ip_usage WHERE ip=?", (ip,)) as cursor:
            row = await cursor.fetchone()
        current_time = datetime.now()
        if row:
            uses, last_access = row
            if last_access < (current_time - timedelta(minutes=QUOTA_RENEWAL_MINUTES)):
                await db.execute("DELETE FROM ip_usage WHERE ip=?", (ip,))
                await db.commit()
                return False
            elif int(uses) >= MAX_USES_QUOTA:
                return True
    return False


async def reserve_upload_slot(ip):
    """Atomically consume one upload from the IP's quota.

    Returns False when the quota is exhausted. Doing the check and the increment in a single
    UPDATE means concurrent uploads cannot slip past the limit.
    """
    await ip_reached_quota(ip)  # Drops the record if the renewal window has passed
    now = datetime.now()
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute(
            "INSERT OR IGNORE INTO ip_usage (ip, uses, last_access) VALUES (?, 0, ?)",
            (ip, now),
        )
        cursor = await db.execute(
            "UPDATE ip_usage SET uses = uses + 1, last_access = ? WHERE ip = ? AND uses < ?",
            (now, ip, MAX_USES_QUOTA),
        )
        await db.commit()
        return cursor.rowcount == 1


async def release_upload_slot(ip):
    """Give back a reserved upload slot after a failed upload."""
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute("UPDATE ip_usage SET uses = MAX(uses - 1, 0) WHERE ip = ?", (ip,))
        await db.commit()


def generate_download_code(length=12):
    """Generate a cryptographically secure random download code."""
    characters = string.ascii_letters + string.digits
    return "".join(secrets.choice(characters) for i in range(length))


def validate_download_code(download_code):
    """Validate that download code matches expected format (12 alphanumeric characters)."""
    if not download_code or not isinstance(download_code, str):
        return False
    if len(download_code) != 12:
        return False
    return all(c in string.ascii_letters + string.digits for c in download_code)


# --- Request Handlers ---


async def index(request):
    file_expiry_hours = FILE_EXPIRY_MINUTES // 60
    file_expiry_minutes = FILE_EXPIRY_MINUTES % 60
    context = {
        "max_file_size": int(MAX_FILE_SIZE / 1024 / 1024),
        "max_file_bytes": MAX_FILE_SIZE,
        "file_expiry_hours": file_expiry_hours,
        "file_expiry_minutes": file_expiry_minutes,
        "base_url": public_base_url(request) or str(request.url.origin()),
    }
    return aiohttp_jinja2.render_template("index.html", request, context, app_key=APP_KEY)


class UploadError(Exception):
    """An upload was rejected; carries the HTTP status and message for the client."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def expected_hosts(request):
    """Host names this instance answers to, for comparing against an Origin header."""
    hosts = {(request.host or "").lower()}
    if proxy_is_trusted(request):
        forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
        if forwarded_host:
            hosts.add(forwarded_host.lower())
    if PUBLIC_BASE_URL:
        hosts.add(urlparse(PUBLIC_BASE_URL).netloc.lower())
    hosts.discard("")
    return hosts


def is_cross_site(request):
    """Reject uploads a third-party page triggers in a visitor's browser.

    Browsers label cross-origin requests with Sec-Fetch-Site; older ones at least send
    Origin. Non-browser clients such as curl send neither and are allowed through.
    """
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return False
    if origin.lower() == "null":
        return True
    return urlparse(origin).netloc.lower() not in expected_hosts(request)


def storage_has_headroom():
    """False when accepting one more maximum-size upload would exhaust the disk reserve."""
    try:
        free = shutil.disk_usage(UPLOAD_DIR).free
    except OSError:
        return True
    return free >= MAX_FILE_SIZE + MIN_FREE_DISK_BYTES


async def storage_within_budget():
    """False when the configured total storage budget would be exceeded."""
    if STORAGE_BUDGET_BYTES <= 0:
        return True
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute("SELECT COALESCE(SUM(size), 0) FROM files") as cursor:
            (stored,) = await cursor.fetchone()
    return stored + MAX_FILE_SIZE <= STORAGE_BUDGET_BYTES


_upload_semaphore = None


def upload_semaphore():
    global _upload_semaphore
    if _upload_semaphore is None:
        _upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
    return _upload_semaphore


async def store_upload(request, open_source):
    """Reserve a quota slot, stream an upload to disk and register it in the database.

    ``open_source`` is awaited once the slot is reserved and must return
    ``(filename, read_chunk)`` where ``read_chunk()`` is a coroutine yielding the next
    chunk of bytes, or an empty value at end of stream.

    Returns the download code. Raises UploadError when the upload is rejected; any
    failure leaves no partial file behind and does not count against the quota.
    """
    if not storage_has_headroom() or not await storage_within_budget():
        raise UploadError(507, "The server is out of storage space. Please try again later.")
    semaphore = upload_semaphore()
    if semaphore.locked():
        raise UploadError(503, "Too many uploads are in progress. Please try again in a moment.")

    ip = get_client_ip(request)
    if not await reserve_upload_slot(ip):
        raise UploadError(429, "You have exceeded the maximum number of uploads for today.")

    file_path = None
    completed = False
    try:
        async with semaphore:
            filename, read_chunk = await open_source()
            filename = truncate_filename(secure_filename(filename or ""))
            if not filename:
                raise UploadError(400, "Invalid file name.")

            file_id = str(uuid.uuid4())
            download_code = generate_download_code()
            file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{filename}")

            size = 0
            async with aiofiles.open(file_path, "wb") as f:
                while True:
                    try:
                        async with asyncio.timeout(UPLOAD_READ_TIMEOUT_SECONDS):
                            chunk = await read_chunk()
                    except TimeoutError:
                        raise UploadError(408, "The upload stalled and was cancelled.")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        raise UploadError(413, "File size exceeds the maximum limit.")
                    await f.write(chunk)

            upload_time = datetime.now()
            async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
                await db.execute(
                    "INSERT INTO files (id, filename, path, download_code, upload_time, size)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (file_id, filename, file_path, download_code, upload_time, size),
                )
                await db.commit()
            completed = True
            return download_code
    finally:
        # Any early exit, client disconnect or error leaves no partial file behind and
        # does not count against the quota.
        if not completed:
            if file_path is not None:
                await remove_file_quietly(file_path)
            await release_upload_slot(ip)


async def upload_file(request):
    """Multipart upload used by the web front page. Responds with the download path."""
    if is_cross_site(request):
        return web.Response(text="Cross-site uploads are not allowed.", status=403)
    # Multipart framing adds a little overhead; anything clearly beyond the limit is
    # refused before the body is read.
    if content_length_exceeds_limit(request, slack=1024 * 1024):
        return web.Response(text="File size exceeds the maximum limit.", status=413)

    async def open_multipart():
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            raise UploadError(400, "No file field in form.")
        return field.filename, field.read_chunk

    try:
        download_code = await store_upload(request, open_multipart)
    except UploadError as exc:
        return web.Response(text=exc.message, status=exc.status)
    return web.Response(text=f"/download/{download_code}")


def content_length_exceeds_limit(request, slack=0):
    content_length = request.headers.get("Content-Length", "")
    return content_length.isdigit() and int(content_length) > MAX_FILE_SIZE + slack


async def cli_upload_expect_handler(request):
    """Handle ``Expect: 100-continue`` for command line uploads.

    curl sends this header before large bodies. Rejecting an oversized upload here means
    the client never has to send the body at all.
    """
    expect = request.headers.get("Expect", "").lower()
    if expect != "100-continue":
        raise web.HTTPExpectationFailed(text=f"Unknown Expect: {expect}")
    if content_length_exceeds_limit(request):
        return web.Response(text="File size exceeds the maximum limit.\n", status=413)
    await request.writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
    request.writer.output_size = 0
    return None


async def cli_upload(request):
    """Raw-body upload for curl, wget and friends: ``curl -T file https://host/``.

    The request body is stored as-is under the file name taken from the URL path. The
    response is plain text: the absolute single-use download link on the first line, so
    it can be piped straight into other tools, and the remaining quota on the second.
    """
    if is_cross_site(request):
        return web.Response(text="Cross-site uploads are not allowed.\n", status=403)
    if content_length_exceeds_limit(request):
        return web.Response(text="File size exceeds the maximum limit.\n", status=413)

    async def open_body():
        filename = request.match_info.get("filename", "")
        return filename or "file", lambda: request.content.read(UPLOAD_CHUNK_SIZE)

    try:
        download_code = await store_upload(request, open_body)
    except UploadError as exc:
        return web.Response(text=f"{exc.message}\n", status=exc.status)

    base_url = public_base_url(request)
    download_url = f"{base_url}/download/{download_code}"
    landing_url = f"{base_url}/landing/download/{download_code}"
    quota = await quota_status(get_client_ip(request))
    quota_line = (
        f"You have {quota['quota_left']} uploads left. Quota resets in "
        f"{quota['quota_renewal_hours']} hours, {quota['quota_renewal_minutes']} minutes."
    )
    return web.Response(
        text=f"{download_url}\n{quota_line}\n",
        headers={"X-Landing-Url": landing_url, "Cache-Control": "no-store"},
    )


async def landing_page_download(request):
    download_code = request.match_info["download_code"]
    if not validate_download_code(download_code):
        response = aiohttp_jinja2.render_template(
            "file_not_found.html", request, {}, app_key=APP_KEY
        )
        response.set_status(404)
        return response
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute(
            "SELECT filename, path FROM files WHERE download_code=?", (download_code,)
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        filename, file_path = row
        if await async_isfile(file_path):
            download_link = f"/download/{download_code}"
            context = {
                "filename": filename,
                "download_link": download_link,
                "download_code": download_code,
                "internal_base_url": INTERNAL_BASE_URL if INTERNAL_IP else "",
            }
            response = aiohttp_jinja2.render_template(
                "download.html", request, context, app_key=APP_KEY
            )
            # The page names the file; never let a shared browser serve it from cache.
            response.headers["Cache-Control"] = "no-store"
            return response

    # Key not found, so returning a 404 response
    response = aiohttp_jinja2.render_template("file_not_found.html", request, {}, app_key=APP_KEY)
    response.set_status(404)
    return response


async def delayed_file_deletion(file_path):
    """Remove the file a few seconds after its single download began, so the transfer
    that is already streaming is not interrupted. The database row is gone already."""
    await asyncio.sleep(5)
    await remove_file_quietly(file_path)


async def claim_download(download_code):
    """Atomically consume a download code.

    The row is deleted in the same statement that reads it, so of two simultaneous
    requests exactly one receives the file and the other sees "already downloaded".
    Returns (filename, path) or None.
    """
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute(
            "DELETE FROM files WHERE download_code=? RETURNING filename, path",
            (download_code,),
        ) as cursor:
            row = await cursor.fetchone()
        await db.commit()
    return row


async def download_file(request):
    download_code = request.match_info["download_code"]
    row = None
    if validate_download_code(download_code):
        row = await claim_download(download_code)
    if row:
        filename, file_path = row
        if await async_isfile(file_path):
            response = web.FileResponse(file_path)
            # Serve every upload as an opaque attachment so a browser never renders it
            # (e.g. an uploaded HTML or SVG file) in the site's origin.
            response.headers["Content-Type"] = "application/octet-stream"
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            response.headers["Cache-Control"] = "no-store"
            spawn_background_task(delayed_file_deletion(file_path))
            return response
    response = aiohttp_jinja2.render_template("file_not_found.html", request, {}, app_key=APP_KEY)
    response.set_status(404)
    return response


async def healthz(request):
    """Cheap liveness probe for the container health check; not logged."""
    return web.Response(status=204, headers={"Cache-Control": "no-store"})


async def handle_404(request):
    response = aiohttp_jinja2.render_template("404.html", request, {}, app_key=APP_KEY)
    response.set_status(404)
    return response


def policy_context():
    """Values the privacy and cookie policy pages state about this instance."""
    return {
        "file_expiry_hours": FILE_EXPIRY_MINUTES // 60,
        "file_expiry_minutes": FILE_EXPIRY_MINUTES % 60,
        "quota_renewal_minutes": QUOTA_RENEWAL_MINUTES,
        "purge_interval_minutes": PURGE_INTERVAL_MINUTES,
        "max_file_size": int(MAX_FILE_SIZE / 1024 / 1024),
        "quota_window_text": humanize_minutes(QUOTA_RENEWAL_MINUTES),
        "analytics_enabled": bool(ANALYTICS_SCRIPT),
        "analytics_host": analytics_script_host(),
    }


def humanize_minutes(minutes):
    """60 -> 'an hour', 120 -> '2 hours', 90 -> '90 minutes'."""
    if minutes == 60:
        return "an hour"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


def analytics_script_host():
    """Host name the analytics script is loaded from, or '' when analytics is off."""
    match = re.search(r'src\s*=\s*["\']([^"\']+)["\']', ANALYTICS_SCRIPT, re.IGNORECASE)
    return urlparse(match.group(1)).netloc if match else ""


async def privacy_policy(request):
    return aiohttp_jinja2.render_template(
        "privacy.html", request, policy_context(), app_key=APP_KEY
    )


async def cookie_policy(request):
    return aiohttp_jinja2.render_template(
        "cookies.html", request, policy_context(), app_key=APP_KEY
    )


async def robots_txt(request):
    # Landing and download URLs must never be crawled: fetching a direct
    # download link consumes the single use and deletes the file.
    lines = [
        "User-agent: *",
        "Disallow: /landing/",
        "Disallow: /download/",
        "Disallow: /check-limit",
        "Disallow: /time-left/",
        "Disallow: /healthz",
        "Allow: /",
    ]
    base_url = public_base_url(request)
    if base_url:
        lines.append(f"Sitemap: {base_url}/sitemap.xml")
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


async def sitemap_xml(request):
    base_url = public_base_url(request) or str(request.url.origin())
    pages = ["/", "/privacy", "/cookies"]
    urlset = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for page in pages:
        url = ET.SubElement(urlset, "url")
        ET.SubElement(url, "loc").text = base_url + page
    ET.indent(urlset)
    body = ET.tostring(urlset, encoding="unicode", xml_declaration=True) + "\n"
    return web.Response(text=body, content_type="application/xml")


async def quota_status(ip):
    """Return the remaining uploads and time until renewal for a (hashed) IP."""
    await ip_reached_quota(ip)  # Clean up expired records if needed
    quota_left = MAX_USES_QUOTA
    current_time = datetime.now()
    next_quota_renewal = timedelta(minutes=QUOTA_RENEWAL_MINUTES)

    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute("SELECT uses, last_access FROM ip_usage WHERE ip=?", (ip,)) as cursor:
            row = await cursor.fetchone()
    if row:
        uses, last_access = row
        if last_access >= (current_time - timedelta(minutes=QUOTA_RENEWAL_MINUTES)):
            quota_left = MAX_USES_QUOTA - uses
            next_quota_renewal = (
                last_access + timedelta(minutes=QUOTA_RENEWAL_MINUTES)
            ) - current_time
    quota_renewal_hours = int(next_quota_renewal.total_seconds() // 3600)
    quota_renewal_minutes = int((next_quota_renewal.total_seconds() % 3600) // 60)
    return {
        "limit_reached": await ip_reached_quota(ip),
        "quota_left": max(quota_left, 0),
        "quota_renewal_hours": quota_renewal_hours,
        "quota_renewal_minutes": quota_renewal_minutes,
    }


async def check_limit(request):
    status = await quota_status(get_client_ip(request))
    return web.json_response(status, headers={"Cache-Control": "no-store"})


async def time_left(request):
    download_code = request.match_info["download_code"]
    if not validate_download_code(download_code):
        return web.json_response({"message": "Download code not found."}, status=404)
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute(
            "SELECT upload_time FROM files WHERE download_code=?", (download_code,)
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        upload_time = row[0]
        expiry_time = upload_time + timedelta(minutes=FILE_EXPIRY_MINUTES)
        current_time = datetime.now()
        remaining = expiry_time - current_time
        if remaining.total_seconds() > 0:
            hours_left = int(remaining.total_seconds() // 3600)
            minutes_left = int((remaining.total_seconds() % 3600) // 60)
            return web.json_response(
                {
                    "hours_left": hours_left,
                    "minutes_left": minutes_left,
                    "message": "The file is available",
                }
            )
        else:
            return web.json_response({"message": "The file has already expired."}, status=410)
    else:
        return web.json_response({"message": "Download code not found."}, status=404)


# --- Background Tasks ---


async def purge_expired():
    """Delete files older than the expiry time and clean up the ip_usage table."""
    expiry_time = datetime.now() - timedelta(minutes=FILE_EXPIRY_MINUTES)
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute(
            "SELECT id, path FROM files WHERE upload_time < ?", (expiry_time,)
        ) as cursor:
            old_files = await cursor.fetchall()
        for file_id, file_path in old_files:
            await remove_file_quietly(file_path)
            await db.execute("DELETE FROM files WHERE id=?", (file_id,))
        cutoff_time = datetime.now() - timedelta(minutes=QUOTA_RENEWAL_MINUTES)
        await db.execute("DELETE FROM ip_usage WHERE last_access < ?", (cutoff_time,))
        await db.commit()


async def check_database_file_consistency():
    """Ensure that database entries and actual files on disk are in sync."""
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute("SELECT id, path FROM files") as cursor:
            files = await cursor.fetchall()
        for file_id, file_path in files:
            if not await async_isfile(file_path):
                await db.execute("DELETE FROM files WHERE id=?", (file_id,))
        upload_files = await async_listdir(UPLOAD_DIR)
        now = time.time()
        for filename in upload_files:
            file_path = os.path.join(UPLOAD_DIR, filename)
            async with db.execute("SELECT id FROM files WHERE path=?", (file_path,)) as cursor:
                exists = await cursor.fetchone()
            if exists:
                continue
            # Uploads are registered in the database only once fully written, so a recent
            # unregistered file is most likely still being uploaded. Leave it alone.
            try:
                mtime = await asyncio.to_thread(os.path.getmtime, file_path)
            except OSError:
                continue
            if now - mtime < ORPHAN_FILE_GRACE_SECONDS:
                continue
            await remove_file_quietly(file_path)
        await db.commit()


# --- Middleware ---


@web.middleware
async def security_headers_middleware(request, handler):
    # Generate nonce for this request (used in CSP and made available to templates)
    nonce = secrets.token_urlsafe(16)
    request["csp_nonce"] = nonce

    response = await handler(request)

    # Set Content Security Policy with nonce
    csp = (
        "default-src 'self' {analytics_script_csp}; "
        "script-src 'self' 'nonce-{nonce}' {analytics_script_csp}; "
        "style-src 'self'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self';"
    ).format(analytics_script_csp=ANALYTICS_SCRIPT_CSP, nonce=nonce)
    response.headers["Content-Security-Policy"] = csp

    # Prevent MIME type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Prevent clickjacking
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # Use HSTS if serving over HTTPS
    if HTTPS_ONLY:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains; preload"
        )
    # Referrer information policy
    response.headers["Referrer-Policy"] = "same-origin"
    # Permissions-Policy header to restrict browser features
    response.headers["Permissions-Policy"] = (
        "geolocation=(), "
        "microphone=(), "
        "camera=(), "
        "payment=(), "
        "usb=(), "
        "magnetometer=(), "
        "gyroscope=(), "
        "accelerometer=(), "
        "ambient-light-sensor=(), "
        "fullscreen=(self)"
    )
    return response


# --- Application Factory ---


async def create_app(
    purge_interval_minutes=PURGE_INTERVAL_MINUTES,
    consistency_check_interval_minutes=CONSISTENCY_CHECK_INTERVAL_MINUTES,
):
    # Ensure upload directory exists (may be overridden in tests)
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except (PermissionError, OSError):
        pass  # May fail in test environments

    await init_db()
    if not PUBLIC_BASE_URL:
        logger.warning(
            "PUBLIC_BASE_URL is not set; absolute links are derived from request headers."
        )
    if TRUSTED_PROXY_COUNT > 0 and not TRUSTED_PROXY_IPS:
        logger.warning(
            "X-Forwarded-For is trusted from any connection. Bind the app to the proxy only, "
            "or set TRUSTED_PROXY_IPS."
        )
    app = web.Application(middlewares=[security_headers_middleware])

    # Remove Server header using signal handler (aiohttp adds it automatically)
    # This ensures the header is removed even if aiohttp adds it after middleware runs
    async def on_response_prepare(request, response):
        # Remove Server header that aiohttp automatically adds
        server_keys = [key for key in response.headers.keys() if key.lower() == "server"]
        for key in server_keys:
            del response.headers[key]

    app.on_response_prepare.append(on_response_prepare)

    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(os.path.join(APP_DIR, "templates")),
        app_key=APP_KEY,
        context_processors=[version_context_processor],
    )

    app.router.add_get("/", index)
    app.router.add_post("/upload", upload_file)
    # Command line uploads: PUT the raw file body to / or /<filename>.
    app.router.add_put("/", cli_upload, expect_handler=cli_upload_expect_handler)
    app.router.add_put("/{filename}", cli_upload, expect_handler=cli_upload_expect_handler)
    app.router.add_get("/landing/download/{download_code}", landing_page_download)
    app.router.add_get("/download/{download_code}", download_file)
    app.router.add_get("/check-limit", check_limit)
    app.router.add_get("/time-left/{download_code}", time_left)
    app.router.add_get("/privacy", privacy_policy)
    app.router.add_get("/cookies", cookie_policy)
    app.router.add_get("/robots.txt", robots_txt)
    app.router.add_get("/sitemap.xml", sitemap_xml)
    app.router.add_get("/healthz", healthz)
    mimetypes.add_type("font/woff2", ".woff2")  # self-hosted fonts
    app.router.add_static("/static", os.path.join(APP_DIR, "static"))
    app.router.add_get("/{tail:.*}", handle_404)

    # Run initial cleanup tasks
    await purge_expired()
    await check_database_file_consistency()

    # Schedule periodic background tasks
    scheduler = AsyncIOScheduler()
    scheduler.add_job(purge_expired, "interval", minutes=purge_interval_minutes)
    scheduler.add_job(
        check_database_file_consistency,
        "interval",
        minutes=consistency_check_interval_minutes,
    )
    scheduler.start()

    async def shutdown_scheduler(app):
        scheduler.shutdown(wait=False)

    app.on_cleanup.append(shutdown_scheduler)

    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Containerized app: binding to all interfaces is intended.
    web.run_app(
        create_app(),
        host="0.0.0.0",  # nosec B104
        port=8080,
        access_log_class=ClientIPAccessLogger,
    )
