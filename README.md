# SnapFile.me - Secure File Sharing

SnapFile.me is a simple and secure file sharing service. 

Upload files and get a unique download link for sharing. Once you download the file, it's immediately deleted from the server. The service also limits uploads per IP address. Files not downloaded within a configurable amount of time are automatically deleted. The default is 24 hours.

NOTE! FILES ARE NOT ENCRYPTED AT REST.

## Features

- Drag and drop file upload
- Progress indicator during upload
- Single-use download link with randomized URL
- Max time to live for uploaded files
- Limit number of uploads per day per IP
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
  -v /path/to/uploads:/app/uploads \
  -v /path/to/database:/app/database \
  --env MAX_FILE_SIZE=524288000 \
  --env MAX_USES_PER_DAY=5 \
  --env INTERNAL_IP=127.0.0.1 \
  --env INTERNAL_PORT=8080 \
  --name snapfile.me-container \
  snapfile.me
```

### Configuration
Change MAX_FILE_SIZE, MAX_USES_PER_DAY and your local paths in the docker command or in docker-compose.yml to reflect your setup.

INTERNAL_IP and INTERNAL_PORT are configurable in order for you to configure a direct network internal download link if you are on the same network as snapfile.me - avoiding proxies for maximum speed.

Also make sure that the uploads and database directories exist on your computer to persist files and the database.

Without the database, the system won't be able to match a download URL to a file.

## Accessing the web interface

Visit http://localhost:8080

## Planned improvements
- General GUI improvements
