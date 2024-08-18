# SnapFile.me - Secure File Sharing

## About SnapFile.me

SnapFile.me is a simple and secure file sharing service built using Python's aiohttp framework. 

### File Upload

- Users can upload files on the front page.
- The server saves the file, generates a unique download code, and stores the file information in a SQLite database.
- The user receives a download link that can be used to download the file once.

### File Download

- Users can download the file using the provided download link.
- The server verifies the download code, retrieves the file information from the database, and serves the file.
- The download link is valid for a single use and expires after a specified time.

### Quota Management

- The app tracks the number of uploads per IP address to enforce a time based usage quota.
- The quota is reset periodically based on the configured interval.

### File Expiry and Purging

- Uploaded files have an expiry time after which they are deleted.
- A scheduled task periodically purges expired files and cleans up the database.

## Setup Instructions

### Clone the Repository

```
git clone https://github.com/remimikalsen/snapfile.me
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
  --env MAX_USES_QUOTA=5 \
  --env FILE_EXPIRY_MINUTES=1440 \
  --env QUOTA_RENEWAL_MINUTES=60 \
  --env PURGE_INTERVAL_MINUTES=5 \
  --env CONSISTENCY_CHECK_INTERVAL_MINUTES=1440 \
  --env INTERNAL_IP=127.0.0.1 \
  --env INTERNAL_PORT=8080 \
  --name snapfile.me-container \
  snapfile.me
```


### Configuration
Change --env variables and your local paths in the docker command or in docker-compose.yml to reflect your setup.

- `MAX_FILE_SIZE`: Maximum allowed file size for uploads (default: 500 MB).
- `MAX_USES_QUOTA`: Maximum number of uploads allowed per IP address (default: 5).
- `FILE_EXPIRY_MINUTES`: Time in minutes after which uploaded files expire (default: 1440 minutes or 24 hours).
- `QUOTA_RENEWAL_MINUTES`: Interval in minutes for resetting the usage quota (default: 60 minutes).
- `PURGE_INTERVAL_MINUTES`: Interval in minutes for purging expired files and cleaning up the datbase (default: 5 minutes).
- `CONSISTENCY_CHECK_INTERVAL_MINUTES`: Interval in minutes for checking database/file consistency and cleaning up (default: 1440 minutes or 24 hours).
- `INTERNAL_IP`: Internal IP address for direct download links.
- `INTERNAL_PORT`: Internal port for direct download links.

These environment variables allow the app to be configured for different deployment scenarios and usage patterns.

INTERNAL_IP and INTERNAL_PORT are configurable in order for you to configure a direct network internal download link if you are on the same network as snapfile.me - avoiding proxies for maximum speed.

Also make sure that the uploads and database directories exist on your computer to persist files and the database.

## Accessing the web interface

Visit http://localhost:8080

## Planned improvements
- General GUI improvements
