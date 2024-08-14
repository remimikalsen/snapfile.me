import aiohttp
import aiohttp_jinja2
import jinja2
import os
import uuid
import sqlite3
from aiohttp import web
from werkzeug.utils import secure_filename
from datetime import datetime
import random
import string

# Load configuration from environment variables
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 500 * 1024 * 1024))  # Default to 500 MB
MAX_USES_PER_DAY = int(os.getenv('MAX_USES_PER_DAY', 5))  # Default to 5 uses per day
UPLOAD_DIR = './uploads'
DATABASE_DIR = '/database'
DATABASE_PATH = os.path.join(DATABASE_DIR, 'file_links.db')
APP_KEY = 'aiohttp_jinja2_environment'

# Ensure the upload directory and database directory exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)

# Initialize the database
conn = sqlite3.connect(DATABASE_PATH)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS files
             (id TEXT PRIMARY KEY, filename TEXT, path TEXT, download_code TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS ip_usage
             (ip TEXT, uses INTEGER, last_access DATE)''')
conn.commit()

# Configure Jinja2 templating
aiohttp_jinja2.setup(
    app=web.Application(),
    loader=jinja2.FileSystemLoader('./templates'),
    app_key=APP_KEY
)

def get_client_ip(request):
    """Retrieve the client's IP address from the request."""
    peername = request.transport.get_extra_info('peername')
    if peername is not None:
        host, port = peername
        return host
    return None

def check_ip_usage(ip):
    """Check the IP usage and reset the count if it's a new day."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT uses, last_access FROM ip_usage WHERE ip=?", (ip,))
    result = c.fetchone()
    today = datetime.now().date()

    if result:
        uses, last_access = result
        last_access_date = datetime.strptime(last_access, '%Y-%m-%d').date()

        if last_access_date < today:
            # Reset the usage count for a new day
            c.execute("UPDATE ip_usage SET uses=1, last_access=? WHERE ip=?", (today, ip))
            conn.commit()
            conn.close()
            return True
        elif uses < MAX_USES_PER_DAY:
            # Increment the usage count
            c.execute("UPDATE ip_usage SET uses=uses+1 WHERE ip=?", (ip,))
            conn.commit()
            conn.close()
            return True
        else:
            conn.close()
            return False
    else:
        # First time usage for this IP
        c.execute("INSERT INTO ip_usage (ip, uses, last_access) VALUES (?, 1, ?)", (ip, today))
        conn.commit()
        conn.close()
        return True

def generate_download_code(length=12):
    """Generate a random download code."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

async def index(request):
    return aiohttp_jinja2.render_template('index.html', request, {}, app_key=APP_KEY)

async def upload_file(request):
    ip = get_client_ip(request)
    if not check_ip_usage(ip):
        return web.Response(text="You have exceeded the maximum number of uploads/downloads for today.", status=429)

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
            chunk = await field.read_chunk()  # 8192 bytes by default
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                os.remove(file_path)
                return web.Response(text="File size exceeds limit.", status=400)
            f.write(chunk)

    # Store the file info in the database
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO files (id, filename, path, download_code) VALUES (?, ?, ?, ?)", (file_id, filename, file_path, download_code))
    conn.commit()
    conn.close()

    # Generate the download link
    download_url = f"/download/{download_code}"

    return web.Response(text=download_url)

async def download_file(request):
    ip = get_client_ip(request)
    if not check_ip_usage(ip):
        return web.Response(text="You have exceeded the maximum number of uploads/downloads for today.", status=429)

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

def create_app():
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
    app.router.add_get('/download/{download_code}', download_file)
    app.router.add_static('/static', './static')
    app.router.add_get('/{tail:.*}', handle_404)

    return app

if __name__ == '__main__':
    web.run_app(create_app(), host='0.0.0.0', port=8080)

