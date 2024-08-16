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

# Load configuration from environment variables
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 500 * 1024 * 1024))  # Default to 500 MB
MAX_USES_PER_DAY = int(os.getenv('MAX_USES_PER_DAY', 5))  # Default to 5 uses per day
FILE_EXPIRY_HOURS = int(os.getenv('FILE_EXPIRY_HOURS', 24))  # Default to 24 hours
INTERNAL_IP = os.getenv('INTERNAL_IP', '')  # Default to empty string
INTERNAL_PORT = os.getenv('INTERNAL_PORT', '')  # Default to emtpy string

UPLOAD_DIR = '/app/uploads'
DATABASE_DIR = '/app/database'
DATABASE_PATH = os.path.join(DATABASE_DIR, 'file_links.db')
APP_KEY = 'aiohttp_jinja2_environment'

# Ensure the upload directory and database directory exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)

# Initialize the database
conn = sqlite3.connect(DATABASE_PATH)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS files
             (id TEXT PRIMARY KEY, filename TEXT, path TEXT, download_code TEXT, upload_time TIMESTAMP)''')
c.execute('''CREATE TABLE IF NOT EXISTS ip_usage
             (ip TEXT, uses INTEGER, last_access TEXT)''')
conn.commit()

# Configure Jinja2 templating
aiohttp_jinja2.setup(
    app=web.Application(),
    loader=jinja2.FileSystemLoader('./templates'),
    app_key=APP_KEY
)

def get_client_ip(request):
    """Retrieve the client's IP address from the request."""
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        # The X-Forwarded-For header can contain multiple IPs, the first one is the client's original IP
        ip = forwarded_for.split(',')[0].strip()
    else:
        ip = request.remote
    return ip

def ip_reached_quota(ip):
    """Check the IP usage and reset the count if it's a new day."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT uses, last_access FROM ip_usage WHERE ip=?", (ip,))
    result = c.fetchone()
    current_time = datetime.now()

    if result:
        uses, last_access = result

        last_access_time = datetime.strptime(last_access, '%Y-%m-%d %H:%M:%S')
        if last_access_time < (current_time - timedelta(hours=24)):
            # Reset the usage count for a new day
            c.execute("UPDATE ip_usage SET uses=0, last_access=? WHERE ip=?", (current_time.strftime('%Y-%m-%d %H:%M:%S'), ip))
            conn.commit()
            conn.close()
            return False
        elif int(uses) >= MAX_USES_PER_DAY:
            conn.close()
            return True
    else:
        c.execute("INSERT INTO ip_usage (ip, uses, last_access) VALUES (?, 0, ?)", (ip, current_time.strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return False
    
def generate_download_code(length=12):
    """Generate a random download code."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

async def index(request):
    return aiohttp_jinja2.render_template('index.html', request, {}, app_key=APP_KEY)


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
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    upload_time = datetime.now()
    c.execute("INSERT INTO files (id, filename, path, download_code, upload_time) VALUES (?, ?, ?, ?, ?)", (file_id, filename, file_path, download_code, upload_time))

    # Incrment the usage count for the IP
    c.execute("UPDATE ip_usage SET uses=uses+1, last_access=? WHERE ip=?", (upload_time.strftime('%Y-%m-%d %H:%M:%S'), ip))
    conn.commit()
    conn.close()

    # Generate the download link
    download_url = f"/download/{download_code}"

    return web.Response(text=download_url)

async def landing_page_download(request):
    download_code = request.match_info['download_code']

    # Retrieve the file info from the database
    conn = sqlite3.connect(DATABASE_PATH)
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
    conn = sqlite3.connect(DATABASE_PATH)
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
    if ip_reached_quota(ip):
        return web.json_response({"limit_reached": True})
    else:
        return web.json_response({"limit_reached": False})

def cleanup_files():
    """Delete files older than the configured expiry time and clean up the ip_usage database."""
    expiry_time = datetime.now() - timedelta(hours=FILE_EXPIRY_HOURS)
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()

    # Delete old files
    c.execute("SELECT id, path FROM files WHERE upload_time < ?", (expiry_time,))
    old_files = c.fetchall()
    for file_id, file_path in old_files:
        if os.path.isfile(file_path):
            os.remove(file_path)
        c.execute("DELETE FROM files WHERE id=?", (file_id,))

    # Clean up ip_usage database
    cutoff_time = datetime.now() - timedelta(hours=24)
    c.execute("DELETE FROM ip_usage WHERE last_access < ?", (cutoff_time.strftime('%Y-%m-%d %H:%M:%S'),))

    conn.commit()
    conn.close()

def check_database_file_consistency():
    """Ensure database entries and files are consistent."""
    conn = sqlite3.connect(DATABASE_PATH)
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

def create_app(cleanup_interval_hours=1, cleanup_interval_minutes=0, cleanup_interval_seconds=0):
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
    app.router.add_static('/static', './static')
    app.router.add_get('/{tail:.*}', handle_404)

    # Perform cleanup and consistency check on startup
    cleanup_files()
    check_database_file_consistency()

    # Schedule the background cleanup task using APScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(cleanup_files, 'interval', hours=cleanup_interval_hours, minutes=cleanup_interval_minutes, seconds=cleanup_interval_seconds)
    scheduler.start()

    return app

if __name__ == '__main__':
    web.run_app(create_app(), host='0.0.0.0', port=8080)