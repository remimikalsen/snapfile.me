# SnapFile.me - Secure File Sharing

SnapFile.me is a simple and secure file sharing service. Upload files and get a download link that you can share. The service limits uploads and downloads per IP address and ensures secure file handling.

## Features

- Drag and drop file upload
- Progress indicator during upload
- Single-use download link with randomized URL
- Error handling for exceeded upload limits and invalid paths
- Responsive design with a custom favicon

## Setup Instructions

### Clone the Repository

```
git clone https://github.com/yourusername/snapfile.me
```

### Run docker compose
```
cd snapfile.me
docker compose up -d
```

### Configuration
In docker compose, change these values to your liking:
```
MAX_FILE_SIZE=524288000 # 500 MB in bytes
MAX_USES_PER_DAY=5      # Maximum uploads/downloads per day per IP
```

### If you prefer Docker only
```
docker build -t snapfile.me .
docker run -d -p 8080:8080 --env-file .env --name snapfile.me-container snapfile.me
```

## Accessing the web interface

Visit http://localhost:8080
