from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import os
import sqlite3
import uuid
import random
import string
import aiohttp_jinja2
import jinja2
import hashlib

# Load configuration from environment variables
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 500 * 1024 * 1024))  # Default to 500 MB
MAX_USES_QUOTA = int(os.getenv('MAX_USES_QUOTA', 5))  # Default to 5 uses per day
FILE_EXPIRY_MINUTES = int(os.getenv('FILE_EXPIRY_MINUTES', 1440))  # Default to 1440 minutes (24 hours)
QUOTA_RENEWAL_MINUTES = int(os.getenv('QUOTA_RENEWAL_MINUTES', 60))  # Default to 60 minutes (1 hour)
PURGE_INTERVAL_MINUTES = int(os.getenv('PURGE_INTERVAL_MINUTES', 5))  # Default to purge every 5 minutes
CONSISTENCY_CHECK_INTERVAL_MINUTES = int(os.getenv('CONSISTENCY_CHECK_INTERVAL_MINUTES', 1440))  # Default to database/file consistency check every 24 hours
INTERNAL_IP = os.getenv('INTERNAL_IP', '')  # Default to empty string
INTERNAL_PORT = os.getenv('INTERNAL_PORT', '')  # Default to emtpy string

UPLOAD_DIR = '/app/uploads'
DATABASE_DIR = '/app/database'
DATABASE_PATH = os.path.join(DATABASE_DIR, 'file_links.db')
APP_KEY = 'aiohttp_jinja2_environment'

# Ensure the upload directory and database directory exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)


def adapt_datetime_iso(val):
    """Adapt datetime.datetime to timezone-naive ISO 8601 date."""
    return val.isoformat()
sqlite3.register_adapter(datetime, adapt_datetime_iso)

def convert_datetime(val):
    """Convert ISO 8601 datetime to datetime.datetime object."""
    return datetime.fromisoformat(val.decode())
sqlite3.register_converter("DATETIME", convert_datetime)



# Initialize the database
conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS files
             (id TEXT PRIMARY KEY, filename TEXT, path TEXT, download_code TEXT, upload_time DATETIME)''')
c.execute('''CREATE TABLE IF NOT EXISTS ip_usage
             (ip TEXT, uses INTEGER, last_access DATETIME)''')
conn.commit()

# Configure Jinja2 templating
aiohttp_jinja2.setup(
    app=web.Application(),
    loader=jinja2.FileSystemLoader('./templates'),
    app_key=APP_KEY
)


def hash_ip(ip):
    return hashlib.sha256(ip.encode()).hexdigest()

def get_client_ip(request):
    """Retrieve the client's IP address from the request."""
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        # The X-Forwarded-For header can contain multiple IPs, the first one is the client's original IP
        ip = forwarded_for.split(',')[0].strip()
    else:
        ip = request.remote
    return hash_ip(ip)

def ip_reached_quota(ip):
    """Check the IP usage and delete the record if it has expired."""
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c = conn.cursor()
    c.execute("SELECT uses, last_access FROM ip_usage WHERE ip=?", (ip,))
    result = c.fetchone()
    current_time = datetime.now()

    if result:
        uses, last_access = result

        if last_access < (current_time - timedelta(minutes=QUOTA_RENEWAL_MINUTES)):
            # Reset the usage count for a new day
            c.execute("DELETE FROM ip_usage WHERE ip=?", (ip,))
            conn.commit()
            conn.close()
            return False
        elif int(uses) >= MAX_USES_QUOTA:
            conn.close()
            return True

    conn.close()
    return False
    
def generate_download_code(length=12):
    """Generate a random download code."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

async def index(request):
    ip = get_client_ip(request)
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
    if ip_reached_quota(ip):
        return web.Response(text="You have exceeded the maximum number of uploads for today.", status=429)

    reader = await request.multipart()
    field = await reader.next()
    
    if field.name != 'file':
        return web.Response(text="No file field in form.", status=400)

    filename = secure_filename(field.filename)
    if not filename:
        return web.Response(text="Invalid file name.", status=400)

    # Generate a unique file ID and create the path
    file_id = str(uuid.uuid4())
    download_code = generate_download_code()
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{filename}")

    size = 0
    with open(file_path, 'wb') as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                os.remove(file_path)
                return web.Response(text="File size exceeds the maximum limit.", status=400)
            f.write(chunk)

    # Store the file info in the database
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c = conn.cursor()
    upload_time = datetime.now()
    c.execute("INSERT INTO files (id, filename, path, download_code, upload_time) VALUES (?, ?, ?, ?, ?)", (file_id, filename, file_path, download_code, upload_time))

    # Incrment the usage count for the IP
    c.execute("SELECT 1 FROM ip_usage WHERE ip=?", (ip,))
    result = c.fetchone()
    if result:
        c.execute("UPDATE ip_usage SET uses=uses+1, last_access=? WHERE ip=?", (upload_time, ip))
    else:
        c.execute("INSERT INTO ip_usage (ip, uses, last_access) VALUES (?, 1, ?)", (ip, upload_time))
    
    conn.commit()
    conn.close()

    # Generate the download link
    download_url = f"/download/{download_code}"

    return web.Response(text=download_url)

async def landing_page_download(request):
    download_code = request.match_info['download_code']

    # Retrieve the file info from the database
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c = conn.cursor()
    c.execute("SELECT filename, path FROM files WHERE download_code=?", (download_code,))
    result = c.fetchone()
    if result:
        filename, file_path = result
        if os.path.isfile(file_path):
            download_link = f"/download/{download_code}"
            context = {
                'filename': filename,
                'download_link': download_link,
                'download_code': download_code,
                'internal_ip': INTERNAL_IP,
                'internal_port': INTERNAL_PORT
            }
            conn.close()
            return aiohttp_jinja2.render_template('download.html', request, context, app_key=APP_KEY)

    conn.close()
    return aiohttp_jinja2.render_template('file_not_found.html', request, {}, app_key=APP_KEY)

async def download_file(request):
    download_code = request.match_info['download_code']

    # Retrieve the file info from the database
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c = conn.cursor()
    c.execute("SELECT filename, path FROM files WHERE download_code=?", (download_code,))
    result = c.fetchone()
    if result:
        filename, file_path = result
        if os.path.isfile(file_path):
            response = web.FileResponse(file_path)
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            await response.prepare(request)
            os.remove(file_path)  # Delete the file after downloading
            c.execute("DELETE FROM files WHERE download_code=?", (download_code,))
            conn.commit()
            conn.close()
            return response
    conn.close()
    return aiohttp_jinja2.render_template('file_not_found.html', request, {}, app_key=APP_KEY)
    

async def handle_404(request):
    return aiohttp_jinja2.render_template('404.html', request, {}, app_key=APP_KEY)

async def check_limit(request):
    ip = get_client_ip(request)

    ip_reached_quota(ip) # To delete expired records

    quota_left = MAX_USES_QUOTA
    current_time = datetime.now()
    next_quota_renewal = (current_time + timedelta(minutes=QUOTA_RENEWAL_MINUTES)) - current_time
    
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c = conn.cursor()
    c.execute("SELECT uses, last_access FROM ip_usage WHERE ip=?", (ip,))
    result = c.fetchone()

    if result:
        uses, last_access = result

        if last_access >= (current_time - timedelta(minutes=QUOTA_RENEWAL_MINUTES)):
            quota_left = MAX_USES_QUOTA - uses
            next_quota_renewal = last_access + timedelta(minutes=QUOTA_RENEWAL_MINUTES) - current_time

    conn.close()

    quota_renewal_hours = int(next_quota_renewal.total_seconds() // 3600)
    quota_renewal_minutes = int((next_quota_renewal.total_seconds() % 3600) // 60)

    if ip_reached_quota(ip):
        return web.json_response({"limit_reached": True,
                                  "quota_left": quota_left,
                                  "quota_renewal_hours": quota_renewal_hours,
                                  "quota_renewal_minutes": quota_renewal_minutes})
    else:
        return web.json_response({"limit_reached": False,
                                  "quota_left": quota_left,
                                  "quota_renewal_hours": quota_renewal_hours,
                                  "quota_renewal_minutes": quota_renewal_minutes})

async def time_left(request):
    download_code = request.match_info['download_code']

    # Retrieve the file info from the database
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c = conn.cursor()
    c.execute("SELECT upload_time FROM files WHERE download_code=?", (download_code,))
    result = c.fetchone()
    conn.close()

    if result:
        upload_time = result[0]
        expiry_time = upload_time + timedelta(minutes=FILE_EXPIRY_MINUTES)
        current_time = datetime.now()
        time_left = expiry_time - current_time

        if time_left.total_seconds() > 0:
            hours_left = int(time_left.total_seconds() // 3600)
            minutes_left = int((time_left.total_seconds() % 3600) // 60)
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

def purge_expired():
    """Delete files older than the configured expiry time and clean up the ip_usage database."""
    expiry_time = datetime.now() - timedelta(minutes=FILE_EXPIRY_MINUTES)
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c = conn.cursor()

    # Delete old files
    c.execute("SELECT id, path FROM files WHERE upload_time < ?", (expiry_time,))
    old_files = c.fetchall()
    for file_id, file_path in old_files:
        if os.path.isfile(file_path):
            os.remove(file_path)
        c.execute("DELETE FROM files WHERE id=?", (file_id,))

    # Clean up ip_usage database
    cutoff_time = datetime.now() - timedelta(minutes=QUOTA_RENEWAL_MINUTES)
    #cutoff_time = cutoff_time.strftime('%Y-%m-%d %H:%M:%S')
    c.execute("DELETE FROM ip_usage WHERE last_access < ?", (cutoff_time,))

    conn.commit()
    conn.close()

def check_database_file_consistency():
    """Ensure database entries and files are consistent."""
    conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    c = conn.cursor()

    # Check for files in the database that do not exist on disk
    c.execute("SELECT id, path FROM files")
    files = c.fetchall()
    for file_id, file_path in files:
        if not os.path.isfile(file_path):
            c.execute("DELETE FROM files WHERE id=?", (file_id,))

    # Check for files on disk that are not in the database
    for filename in os.listdir(UPLOAD_DIR):
        file_path = os.path.join(UPLOAD_DIR, filename)
        c.execute("SELECT id FROM files WHERE path=?", (file_path,))
        if not c.fetchone():
            os.remove(file_path)

    conn.commit()
    conn.close()

def create_app(purge_interval_minutes=PURGE_INTERVAL_MINUTES, consistency_check_interval_minutes=CONSISTENCY_CHECK_INTERVAL_MINUTES):
    app = web.Application()
    
    # Setup Jinja2 with the application key
    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader('./templates'),
        app_key=APP_KEY
    )
    
    # Define routes in the correct order
    app.router.add_get('/', index)
    app.router.add_post('/upload', upload_file)
    app.router.add_get('/landing/download/{download_code}', landing_page_download)
    app.router.add_get('/download/{download_code}', download_file)
    app.router.add_get('/check-limit', check_limit)
    app.router.add_get('/time-left/{download_code}', time_left)
    app.router.add_static('/static', './static')
    app.router.add_get('/{tail:.*}', handle_404)

    # Perform cleanup and consistency check on startup
    purge_expired()
    check_database_file_consistency()

    # Schedule the background cleanup task using APScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(purge_expired, 'interval', minutes=purge_interval_minutes)
    scheduler.add_job(check_database_file_consistency, 'interval', minutes=consistency_check_interval_minutes)
    scheduler.start()

    return app

if __name__ == '__main__':
    web.run_app(create_app(), host='0.0.0.0', port=8080)