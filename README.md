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

### Docker compose
```
cd snapfile.me
docker compose up -d
```

### Docker only
```
docker build -t snapfile.me .
docker run -d \
  -p 8080:8080 \
  -v /path/to/uploads:/uploads \
  -v /path/to/database:/database \
  --env MAX_FILE_SIZE=524288000 \
  --env MAX_USES_PER_DAY=5 \
  --name snapfile.me-container \
  snapfile.me
```

### Configuration
Change MAX_FILE_SIZE, MAX_USES_PER_DAY and your local paths in the docker command or in docker-compose.yml to reflect your setup.

Also make sure that the uploads and database directories exist on your computer to persist files and the database.

Without the database, the system won't be able to match a download URL to a file.

## Accessing the web interface

Visit http://localhost:8080

## Pending improvements
- Set up a MAX_TTL on all files uploaded
- Make sure the database and files folder are in sync on each startup
- Give notice of upload limit reached BEFORE attempting to upload
