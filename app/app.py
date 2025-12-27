import asyncio
import os
import uuid
import secrets
import string
import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

from aiohttp import web
import aiohttp_jinja2
import jinja2
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import sqlite3
import aiosqlite
import aiofiles
import aiofiles.os
from werkzeug.utils import secure_filename

# Determine the application version from file
VERSION_FILE_PATH = os.path.join(os.path.dirname(__file__), "VERSION")
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
INTERNAL_IP = os.getenv("INTERNAL_IP", "")
INTERNAL_PORT = os.getenv("INTERNAL_PORT", "")
ANALYTICS_SCRIPT_RAW = os.getenv("ANALYTICS_SCRIPT", "")
ANALYTICS_SCRIPT_CSP = os.getenv("ANALYTICS_SCRIPT_CSP", "")

# Whitelist of allowed analytics script domains
# Add trusted analytics domains here (e.g., plausible.io, googletagmanager.com, etc.)
ALLOWED_ANALYTICS_DOMAINS = os.getenv(
    "ALLOWED_ANALYTICS_DOMAINS",
    "plausible.remim.com,plausible.io,www.googletagmanager.com,www.google-analytics.com",
).split(",")

UPLOAD_DIR = "/app/uploads"
DATABASE_DIR = "/app/database"
DATABASE_PATH = os.path.join(DATABASE_DIR, "file_links.db")
APP_KEY = "aiohttp_jinja2_environment"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)

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


async def init_db():
    """Initialize the database and create tables if they do not exist."""
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS files
                             (id TEXT PRIMARY KEY, filename TEXT, path TEXT, download_code TEXT, upload_time DATETIME)"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS ip_usage
                             (ip TEXT, uses INTEGER, last_access DATETIME)"""
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


# --- Context Processors for Templates ---


async def version_context_processor(request):
    # Get nonce from request (set by middleware)
    nonce = request.get("csp_nonce", "")
    return {"VERSION": VERSION, "ANALYTICS_SCRIPT": ANALYTICS_SCRIPT, "CSP_NONCE": nonce}


# --- Utility Functions ---


def hash_ip(ip):
    return hashlib.sha256(ip.encode()).hexdigest()


def get_client_ip(request):
    """Retrieve and hash the client's IP address."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    else:
        ip = request.remote
    return hash_ip(ip)


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
        "file_expiry_hours": file_expiry_hours,
        "file_expiry_minutes": file_expiry_minutes,
    }
    return aiohttp_jinja2.render_template("index.html", request, context, app_key=APP_KEY)


async def upload_file(request):
    ip = get_client_ip(request)
    if await ip_reached_quota(ip):
        return web.Response(
            text="You have exceeded the maximum number of uploads for today.",
            status=429,
        )

    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        return web.Response(text="No file field in form.", status=400)

    filename = secure_filename(field.filename)
    if not filename:
        return web.Response(text="Invalid file name.", status=400)

    file_id = str(uuid.uuid4())
    download_code = generate_download_code()
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{filename}")

    size = 0
    async with aiofiles.open(file_path, "wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                if await async_isfile(file_path):
                    await aiofiles.os.remove(file_path)
                return web.Response(text="File size exceeds the maximum limit.", status=400)
            await f.write(chunk)

    upload_time = datetime.now()
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute(
            "INSERT INTO files (id, filename, path, download_code, upload_time) VALUES (?, ?, ?, ?, ?)",
            (file_id, filename, file_path, download_code, upload_time),
        )
        async with db.execute("SELECT 1 FROM ip_usage WHERE ip=?", (ip,)) as cursor:
            exists = await cursor.fetchone()
        if exists:
            await db.execute(
                "UPDATE ip_usage SET uses = uses + 1, last_access = ? WHERE ip=?",
                (upload_time, ip),
            )
        else:
            await db.execute(
                "INSERT INTO ip_usage (ip, uses, last_access) VALUES (?, 1, ?)",
                (ip, upload_time),
            )
        await db.commit()

    download_url = f"/download/{download_code}"
    return web.Response(text=download_url)


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
                "internal_ip": INTERNAL_IP,
                "internal_port": INTERNAL_PORT,
            }
            return aiohttp_jinja2.render_template(
                "download.html", request, context, app_key=APP_KEY
            )

    # Key not found, so returning a 404 response
    response = aiohttp_jinja2.render_template("file_not_found.html", request, {}, app_key=APP_KEY)
    response.set_status(404)
    return response


async def delayed_file_deletion(file_path, download_code):
    """Delay deletion of a file (and DB record update) so that the download is not interrupted."""
    await asyncio.sleep(5)
    if await async_isfile(file_path):
        try:
            await aiofiles.os.remove(file_path)
        except Exception:
            pass  # nosec B110 - File may already be deleted, ignore errors
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute("DELETE FROM files WHERE download_code=?", (download_code,))
        await db.commit()


async def download_file(request):
    download_code = request.match_info["download_code"]
    if not validate_download_code(download_code):
        return aiohttp_jinja2.render_template("file_not_found.html", request, {}, app_key=APP_KEY)
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute(
            "SELECT filename, path FROM files WHERE download_code=?", (download_code,)
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        filename, file_path = row
        if await async_isfile(file_path):
            response = web.FileResponse(file_path)
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            # Schedule the deletion task so it runs in the background
            asyncio.create_task(delayed_file_deletion(file_path, download_code))
            return response
    return aiohttp_jinja2.render_template("file_not_found.html", request, {}, app_key=APP_KEY)


async def handle_404(request):
    response = aiohttp_jinja2.render_template("404.html", request, {}, app_key=APP_KEY)
    response.set_status(404)
    return response


async def check_limit(request):
    ip = get_client_ip(request)
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
    if await ip_reached_quota(ip):
        return web.json_response(
            {
                "limit_reached": True,
                "quota_left": quota_left,
                "quota_renewal_hours": quota_renewal_hours,
                "quota_renewal_minutes": quota_renewal_minutes,
            }
        )
    else:
        return web.json_response(
            {
                "limit_reached": False,
                "quota_left": quota_left,
                "quota_renewal_hours": quota_renewal_hours,
                "quota_renewal_minutes": quota_renewal_minutes,
            }
        )


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
            if await async_isfile(file_path):
                try:
                    await aiofiles.os.remove(file_path)
                except Exception:
                    pass  # nosec B110 - File may already be deleted, ignore errors
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
        for filename in upload_files:
            file_path = os.path.join(UPLOAD_DIR, filename)
            async with db.execute("SELECT id FROM files WHERE path=?", (file_path,)) as cursor:
                exists = await cursor.fetchone()
            if not exists:
                try:
                    await aiofiles.os.remove(file_path)
                except Exception:
                    pass  # nosec B110 - File may already be deleted, ignore errors
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
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "font-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "img-src 'self' data:;"
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
    await init_db()
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
        loader=jinja2.FileSystemLoader("./templates"),
        app_key=APP_KEY,
        context_processors=[version_context_processor],
    )

    app.router.add_get("/", index)
    app.router.add_post("/upload", upload_file)
    app.router.add_get("/landing/download/{download_code}", landing_page_download)
    app.router.add_get("/download/{download_code}", download_file)
    app.router.add_get("/check-limit", check_limit)
    app.router.add_get("/time-left/{download_code}", time_left)
    app.router.add_static("/static", "./static")
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

    return app


if __name__ == "__main__":
    web.run_app(
        create_app(),
        host="0.0.0.0",  # nosec B104 - Containerized app, binding to all interfaces is safe
        port=8080,
    )
