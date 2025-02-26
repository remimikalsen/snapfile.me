import asyncio
import os
import uuid
import random
import string
import hashlib
from datetime import datetime, timedelta

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
VERSION_FILE_PATH = os.path.join(os.path.dirname(__file__), 'VERSION')
VERSION = "Development"
if os.path.isfile(VERSION_FILE_PATH):
    with open(VERSION_FILE_PATH, 'r') as version_file:
        VERSION = version_file.read().strip() or "Development"
else:
    parent_dir_version_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'VERSION')
    if os.path.isfile(parent_dir_version_path):
        with open(parent_dir_version_path, 'r') as version_file:
            VERSION = version_file.read().strip() + "-development"
    else:
        VERSION = "unknown"

# Load configuration from environment variables
HTTPS_ONLY = os.getenv('HTTPS_ONLY', 'false').lower() == 'true' # Default to False
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 500 * 1024 * 1024))  # Default to 500 MB
MAX_USES_QUOTA = int(os.getenv('MAX_USES_QUOTA', 5))                # Default to 5 uploads per day
FILE_EXPIRY_MINUTES = int(os.getenv('FILE_EXPIRY_MINUTES', 1440))     # Default to 1440 minutes (24 hours)
QUOTA_RENEWAL_MINUTES = int(os.getenv('QUOTA_RENEWAL_MINUTES', 60))     # Default to 60 minutes (1 hour)
PURGE_INTERVAL_MINUTES = int(os.getenv('PURGE_INTERVAL_MINUTES', 5))  # Cleanup every 5 minutes
CONSISTENCY_CHECK_INTERVAL_MINUTES = int(os.getenv('CONSISTENCY_CHECK_INTERVAL_MINUTES', 1440))
INTERNAL_IP = os.getenv('INTERNAL_IP', '')
INTERNAL_PORT = os.getenv('INTERNAL_PORT', '')
ANALYTICS_SCRIPT = os.getenv('ANALYTICS_SCRIPT', '')
ANALYTICS_SCRIPT_CSP = os.getenv('ANALYTICS_SCRIPT_CSP', '')

UPLOAD_DIR = '/app/uploads'
DATABASE_DIR = '/app/database'
DATABASE_PATH = os.path.join(DATABASE_DIR, 'file_links.db')
APP_KEY = 'aiohttp_jinja2_environment'

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
        await db.execute('''CREATE TABLE IF NOT EXISTS files
                             (id TEXT PRIMARY KEY, filename TEXT, path TEXT, download_code TEXT, upload_time DATETIME)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS ip_usage
                             (ip TEXT, uses INTEGER, last_access DATETIME)''')
        await db.commit()

# --- Context Processors for Templates ---

async def version_context_processor(_):
    return {'VERSION': VERSION, 'ANALYTICS_SCRIPT': ANALYTICS_SCRIPT}

# --- Utility Functions ---

def hash_ip(ip):
    return hashlib.sha256(ip.encode()).hexdigest()

def get_client_ip(request):
    """Retrieve and hash the client's IP address."""
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        ip = forwarded_for.split(',')[0].strip()
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
    """Generate a random download code."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

# --- Request Handlers ---

async def index(request):
    file_expiry_hours = FILE_EXPIRY_MINUTES // 60
    file_expiry_minutes = FILE_EXPIRY_MINUTES % 60
    context = {
        'max_file_size': int(MAX_FILE_SIZE / 1024 / 1024),
        'file_expiry_hours': file_expiry_hours,
        'file_expiry_minutes': file_expiry_minutes
    }
    return aiohttp_jinja2.render_template('index.html', request, context, app_key=APP_KEY)

async def upload_file(request):
    ip = get_client_ip(request)
    if await ip_reached_quota(ip):
        return web.Response(text="You have exceeded the maximum number of uploads for today.", status=429)
    
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != 'file':
        return web.Response(text="No file field in form.", status=400)
    
    filename = secure_filename(field.filename)
    if not filename:
        return web.Response(text="Invalid file name.", status=400)
    
    file_id = str(uuid.uuid4())
    download_code = generate_download_code()
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{filename}")
    
    size = 0
    async with aiofiles.open(file_path, 'wb') as f:
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
            (file_id, filename, file_path, download_code, upload_time)
        )
        async with db.execute("SELECT 1 FROM ip_usage WHERE ip=?", (ip,)) as cursor:
            exists = await cursor.fetchone()
        if exists:
            await db.execute("UPDATE ip_usage SET uses = uses + 1, last_access = ? WHERE ip=?", (upload_time, ip))
        else:
            await db.execute("INSERT INTO ip_usage (ip, uses, last_access) VALUES (?, 1, ?)", (ip, upload_time))
        await db.commit()
    
    download_url = f"/download/{download_code}"
    return web.Response(text=download_url)

async def landing_page_download(request):
    download_code = request.match_info['download_code']
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute("SELECT filename, path FROM files WHERE download_code=?", (download_code,)) as cursor:
            row = await cursor.fetchone()
    if row:
        filename, file_path = row
        if await async_isfile(file_path):
            download_link = f"/download/{download_code}"
            context = {
                'filename': filename,
                'download_link': download_link,
                'download_code': download_code,
                'internal_ip': INTERNAL_IP,
                'internal_port': INTERNAL_PORT
            }
            return aiohttp_jinja2.render_template('download.html', request, context, app_key=APP_KEY)
    
    # Key not found, so returning a 404 response
    response = aiohttp_jinja2.render_template('file_not_found.html', request, {}, app_key=APP_KEY)
    response.set_status(404)
    return response

async def delayed_file_deletion(file_path, download_code):
    """Delay deletion of a file (and DB record update) so that the download is not interrupted."""
    await asyncio.sleep(5)
    if await async_isfile(file_path):
        try:
            await aiofiles.os.remove(file_path)
        except Exception:
            pass
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute("DELETE FROM files WHERE download_code=?", (download_code,))
        await db.commit()

async def download_file(request):
    download_code = request.match_info['download_code']
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute("SELECT filename, path FROM files WHERE download_code=?", (download_code,)) as cursor:
            row = await cursor.fetchone()
    if row:
        filename, file_path = row
        if await async_isfile(file_path):
            response = web.FileResponse(file_path)
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            # Schedule the deletion task so it runs in the background
            asyncio.create_task(delayed_file_deletion(file_path, download_code))
            return response
    return aiohttp_jinja2.render_template('file_not_found.html', request, {}, app_key=APP_KEY)

async def handle_404(request):
    response = aiohttp_jinja2.render_template('404.html', request, {}, app_key=APP_KEY)
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
            next_quota_renewal = (last_access + timedelta(minutes=QUOTA_RENEWAL_MINUTES)) - current_time
    quota_renewal_hours = int(next_quota_renewal.total_seconds() // 3600)
    quota_renewal_minutes = int((next_quota_renewal.total_seconds() % 3600) // 60)
    if await ip_reached_quota(ip):
        return web.json_response({
            "limit_reached": True,
            "quota_left": quota_left,
            "quota_renewal_hours": quota_renewal_hours,
            "quota_renewal_minutes": quota_renewal_minutes
        })
    else:
        return web.json_response({
            "limit_reached": False,
            "quota_left": quota_left,
            "quota_renewal_hours": quota_renewal_hours,
            "quota_renewal_minutes": quota_renewal_minutes
        })

async def time_left(request):
    download_code = request.match_info['download_code']
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute("SELECT upload_time FROM files WHERE download_code=?", (download_code,)) as cursor:
            row = await cursor.fetchone()
    if row:
        upload_time = row[0]
        expiry_time = upload_time + timedelta(minutes=FILE_EXPIRY_MINUTES)
        current_time = datetime.now()
        remaining = expiry_time - current_time
        if remaining.total_seconds() > 0:
            hours_left = int(remaining.total_seconds() // 3600)
            minutes_left = int((remaining.total_seconds() % 3600) // 60)
            return web.json_response({
                "hours_left": hours_left,
                "minutes_left": minutes_left,
                "message": "The file is available"
            })
        else:
            return web.json_response({
                "message": "The file has already expired."
            }, status=410)
    else:
        return web.json_response({
            "message": "Download code not found."
        }, status=404)

# --- Background Tasks ---

async def purge_expired():
    """Delete files older than the expiry time and clean up the ip_usage table."""
    expiry_time = datetime.now() - timedelta(minutes=FILE_EXPIRY_MINUTES)
    async with aiosqlite.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute("SELECT id, path FROM files WHERE upload_time < ?", (expiry_time,)) as cursor:
            old_files = await cursor.fetchall()
        for file_id, file_path in old_files:
            if await async_isfile(file_path):
                try:
                    await aiofiles.os.remove(file_path)
                except Exception:
                    pass
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
                    pass
        await db.commit()

# --- Middleware ---

@web.middleware
async def security_headers_middleware(request, handler):
    response = await handler(request)
    # Set Content Security Policy
    csp = (
        "default-src 'self' {analytics_script_csp}; "
        "script-src 'self' 'unsafe-inline' {analytics_script_csp}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "font-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "img-src 'self' data:;"
    ).format(analytics_script_csp=ANALYTICS_SCRIPT_CSP)
    response.headers["Content-Security-Policy"] = csp
    # Prevent MIME type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Prevent clickjacking
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # Use HSTS if serving over HTTPS
    if HTTPS_ONLY:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    # Referrer information policy
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# --- Application Factory ---

async def create_app(purge_interval_minutes=PURGE_INTERVAL_MINUTES,
                     consistency_check_interval_minutes=CONSISTENCY_CHECK_INTERVAL_MINUTES):
    await init_db()
    app = web.Application(middlewares=[security_headers_middleware])
    
    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader('./templates'),
        app_key=APP_KEY,
        context_processors=[version_context_processor]
    )
    
    app.router.add_get('/', index)
    app.router.add_post('/upload', upload_file)
    app.router.add_get('/landing/download/{download_code}', landing_page_download)
    app.router.add_get('/download/{download_code}', download_file)
    app.router.add_get('/check-limit', check_limit)
    app.router.add_get('/time-left/{download_code}', time_left)
    app.router.add_static('/static', './static')
    app.router.add_get('/{tail:.*}', handle_404)
    
    # Run initial cleanup tasks
    await purge_expired()
    await check_database_file_consistency()
    
    # Schedule periodic background tasks
    scheduler = AsyncIOScheduler()
    scheduler.add_job(purge_expired, 'interval', minutes=purge_interval_minutes)
    scheduler.add_job(check_database_file_consistency, 'interval', minutes=consistency_check_interval_minutes)
    scheduler.start()
    
    return app

if __name__ == '__main__':
    web.run_app(create_app(), host='0.0.0.0', port=8080)
