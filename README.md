![Build Status](https://img.shields.io/github/actions/workflow/status/remimikalsen/snapfile.me/build.yaml)
![License](https://img.shields.io/github/license/remimikalsen/snapfile.me)
![Version](https://img.shields.io/github/tag/remimikalsen/snapfile.me)

# Snapfile

**Snapfile is a simple and secure file sharing service.**

With Snapfile, you upload a file and get a single-use download link back. The provided link is valid for a limited amount of time. You can share the link, or use it yourself, but once you have downloaded the file, it's deleted.

Snapfile is anonymous, but still limits usage through hashing the client IP and storing it in the Snapfile database. Quotas, quota renewal, uploaded file expiration and much more can be customized.

Snapfile doesn't by itself encrypt traffic, but it's easy enough to put it behind a reverse proxy like Nginx or Traefik. Snapfile will read the X-Forwarded-For headers to get the originating client's public IP address.

Snapfile doesn't by itself encrypt the data at rest, but you may encrypt the uploads directory (ecryptfs) or the entire volume the directory is on (Luks) if you wish to increase security somewhat. Files do however have a very short life on the server.


## Table of Contents
- [How It Works](#how-it-works)
  - [File Upload](#file-upload)
  - [File Download](#file-download)
  - [Quota Management](#quota-management)
  - [File Expiry and Purging](#file-expiry-and-purging)
- [Setup and Configuration](#setup-and-configuration)
  - [Building yourself](#building-yourself)
    - [Docker Compose](#docker-compose)
    - [Docker](#docker)
  - [Using a pre-build image](#using-a-pre-built-image)
- [Configuration](#configuration)
- [Accessing the Web Interface](#accessing-the-web-interface)
- [Developer Notes](#developer-notes)



## How it works?

### File upload

- Users can upload files on the front page.
- The file is stored on the server.
- A unique download code is generated and stored with the file information in a SQLite database.
- The user receives a download link that can be used to download the file once.

### File download

- Users can download the file using the provided download link.
- The server verifies the download code, retrieves the file information from the database, and serves the file.
- The file will be deleted once it's downloaded (with a 5 second grace period in case of external inspection prior to downloading it).
- The download link is valid for a single use and expires after a specified time.

### Quota management

- The app tracks the number of uploads per anonymized IP address to enforce a time based usage quota.
- The quota is reset periodically based on the configured interval.

### File expiry and purging

- Uploaded files have an expiry time after which they are deleted.
- A scheduled task periodically purges expired files and cleans up the database.

## Setup and configuration

You may build the application yourself, or use a pre-built image.

### Building yourself

```
git clone https://github.com/remimikalsen/snapfile.me
```

#### Docker compose

Alter the `docker-compose.yml` file so you have:

```
    #image: ghcr.io/remimikalsen/snapfile:v1
    build: 
      context: .
      dockerfile: Dockerfile.snapfile
```

This will make sure you build from source. Now, run docker compose do build from source:

```
docker compose up -d
```

#### Docker

If you prefer to build and run explicitly with Docker only:

```
cd snapfile.me
docker build -t snapfile-image -f Dockerfile.snapfile .
docker run -d \
  --name snapfile \
  --restart unless-stopped \
  -p 8080:8080 \
  -e MAX_FILE_SIZE=524288000 \
  -e MAX_USES_QUOTA=5 \
  -e FILE_EXPIRY_MINUTES=1440 \
  -e QUOTA_RENEWAL_MINUTES=60 \
  -e PURGE_INTERVAL_MINUTES=5 \
  -e CONSISTENCY_CHECK_INTERVAL_MINUTES=1440 \
  -e INTERNAL_IP=127.0.0.1 \
  -e INTERNAL_PORT=127.0.0.1 \
  -v /snapfile/uploads:/app/uploads \
  -v /snapfile/database:/app/database \
  snapfile-image
```

### Using a pre-built image

If you just want to use the latest version, use the pre-built images. Check out `docker-compose.yml` for a reference. 

Or if you want to pull the image directly with docker with the latest v1 image.

```
docker pull ghcr.io/remimikalsen/snapfile:v1
```


## Configuration

There are ample configuration opportunities whether you run through Docker or Docker Compose. Change --env variables and your local paths in the docker command or in docker-compose.yml to reflect your setup.

- `MAX_FILE_SIZE`: Maximum allowed file size for uploads (default: 500 MB).
- `MAX_USES_QUOTA`: Maximum number of uploads allowed per IP address (default: 5).
- `FILE_EXPIRY_MINUTES`: Time in minutes after which uploaded files expire (default: 1440 minutes or 24 hours).
- `QUOTA_RENEWAL_MINUTES`: Interval in minutes for resetting the usage quota (default: 60 minutes).
- `PURGE_INTERVAL_MINUTES`: Interval in minutes for purging expired files and cleaning up the database (default: 5 minutes).
- `CONSISTENCY_CHECK_INTERVAL_MINUTES`: Interval in minutes for checking database/file consistency and cleaning up (default: 1440 minutes or 24 hours).
- `INTERNAL_IP`: Internal IP address for direct download links.
- `INTERNAL_PORT`: Internal port for direct download links.
- `ANALYTICS_SCRIPT`: The complete script tag needed for tracking from e.g. Plausible (default: empty)

These environment variables allow the app to be configured for different deployment scenarios and usage patterns.

INTERNAL_IP and INTERNAL_PORT are configurable in order for you to configure a direct network internal download link if you are on the same network as Snapfile - avoiding proxies for maximum speed.

Also make sure that the uploads and database directories exist on your computer to persist files and the database.

## Accessing the web interface

Visit http://localhost:8080

## Developer notes
This app is set up with automatic versioning with git tags, Docker image deployment and app deployment. That's nice to know if you fork it! Read about [building and deploying automatically](https://theawesomegarage.com/blog/build-and-deploy-locally-using-github-actions-and-webhooks).


### Python dependencies

Set up a Python virtual environment for local development to manage Python package versions correctly:

- `pip-tools` is used to compile canonical requirements in `requirements.in`.
- `requirements.txt` is generated using:

  ```sh
  pip-compile requirements.in
  ```

- To upgrade `requirements.txt`, run:

  ```sh
  pip-compile --upgrade requirements.in
  pip install -r requirements.txt
  pip-sync requirements.txt
  ```
