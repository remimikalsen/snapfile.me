![Test Status](https://img.shields.io/github/actions/workflow/status/remimikalsen/snapfile.me/tests.yaml?label=tests)
![Coverage Badge](https://raw.githubusercontent.com/remimikalsen/snapfile.me/refs/heads/main/tests/coverage-badge.svg)
![Build Status](https://img.shields.io/github/actions/workflow/status/remimikalsen/snapfile.me/build.yaml)
![License](https://img.shields.io/github/license/remimikalsen/snapfile.me)
![Version](https://img.shields.io/github/tag/remimikalsen/snapfile.me)

# Snapfile

**Snapfile is a simple, fast and secure file sharing service.**

With Snapfile, you upload a file and get a single-use download link back. The provided link is valid for a limited amount of time. You can share the link, or use it yourself, but once you have downloaded the file, it's deleted.

Snapfile is anonymous, but still limits usage through hashing the client IP and storing it in the Snapfile database. Quotas, quota renewal, uploaded file expiration and much more can be customized.

Snapfile doesn't by itself encrypt traffic, but it's easy enough to put it behind a reverse proxy like Nginx or Traefik. Snapfile will read the X-Forwarded-For headers to get the originating client's public IP address.

Snapfile doesn't by itself encrypt the data at rest, but you may encrypt the uploads directory (ecryptfs) or the entire volume the directory is on (Luks) if you wish to increase security somewhat. Files do however have a very short life on the server.

Snapfile is asynchronous by nature, allowing it to scale efficiently even on modest hardware. For additional scalability, you can deploy multiple Snapfile containers behind a load balancer.


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
- [Command line usage](#command-line-usage)
- [Developer Notes](#developer-notes)
  - [Running locally for development](#running-locally-for-development)
  - [Managing Python dependencies](#managing-python-dependencies)



## How it works?

### File upload

- Users can upload files on the front page, or from a terminal with `curl -T file https://your-snapfile/` (see [Command line usage](#command-line-usage)).
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
  -e INTERNAL_PORT=8080 \
  -e TRUSTED_PROXY_COUNT=1 \
  -v /snapfile/uploads:/app/uploads \
  -v /snapfile/database:/app/database \
  snapfile-image
```

#### Container health check

The Docker image includes a built-in `HEALTHCHECK` instruction that pings the app every 30 seconds. This means Docker automatically tracks whether the container is healthy. You can see the status in `docker ps` (shown as `healthy`, `unhealthy`, or `starting`). Combined with `restart: unless-stopped` in Docker Compose, Docker will automatically restart the container if the health check fails repeatedly.

### Using a pre-built image

If you just want to use the latest version, use the pre-built images. Check out `docker-compose.yml` for a reference.

Or if you want to pull the image directly with docker with the latest v1 image.

```
docker pull ghcr.io/remimikalsen/snapfile:v1
```


## Configuration

There are ample configuration opportunities whether you run through Docker or Docker Compose. Change --env variables and your local paths in the docker command or in docker-compose.yml to reflect your setup.

- `HTTPS_ONLY`: Set to true to enable Strict-Transport-Security header (default: false)
- `MAX_FILE_SIZE`: Maximum allowed file size for uploads (default: 500 MB).
- `MAX_USES_QUOTA`: Maximum number of uploads allowed per IP address (default: 10).
- `FILE_EXPIRY_MINUTES`: Time in minutes after which uploaded files expire (default: 1440 minutes or 24 hours).
- `QUOTA_RENEWAL_MINUTES`: Interval in minutes for resetting the usage quota (default: 60 minutes).
- `PURGE_INTERVAL_MINUTES`: Interval in minutes for purging expired files and cleaning up the database (default: 5 minutes).
- `CONSISTENCY_CHECK_INTERVAL_MINUTES`: Interval in minutes for checking database/file consistency and cleaning up (default: 1440 minutes or 24 hours).
- `TRUSTED_PROXY_COUNT`: Number of reverse proxies in front of Snapfile that append to `X-Forwarded-For` (default: 1). Set to `0` if clients connect directly, otherwise a client can forge the header and bypass the upload quota. See [Reverse proxies and client IPs](#reverse-proxies-and-client-ips).
  Publish the container port on loopback only (`127.0.0.1:8080:8080`, as in `docker-compose.yml`) or set `TRUSTED_PROXY_IPS`, so nobody can bypass the proxy. IPv6 clients are counted per /64 network, not per address.
- `PUBLIC_BASE_URL`: Absolute URL clients use to reach Snapfile, e.g. `https://snapfile.me`. Used for command line links, canonical URLs and social sharing cards. **Always set this in production.** When empty (default) the URL is derived from the request's `Host` header, plus `X-Forwarded-Proto` / `X-Forwarded-Host` when a trusted proxy is configured, which lets a misconfigured proxy pass client-chosen hosts through.
- `TRUSTED_PROXY_IPS`: Optional comma-separated proxy addresses or CIDR ranges (e.g. `172.18.0.0/16,127.0.0.1`). When set, `X-Forwarded-*` headers are honoured only for connections from these addresses, so a client that reaches Snapfile directly cannot forge its IP (default: empty, meaning any connection is assumed to come through the proxy).
- `MIN_FREE_DISK_BYTES`: Uploads are refused with HTTP 507 when accepting one more maximum-size file would leave less free disk than this (default: 268435456, 256 MB).
- `STORAGE_BUDGET_BYTES`: Optional cap on total stored bytes; uploads are refused with 507 when it would be exceeded (default: 0, no cap).
- `MAX_CONCURRENT_UPLOADS`: Uploads in flight at once; further uploads get HTTP 503 until one finishes (default: 20).
- `UPLOAD_READ_TIMEOUT_SECONDS`: A client that sends no data for this long has its upload cancelled with HTTP 408 (default: 60).
- `INTERNAL_IP`: Internal IP address for direct download links.
- `INTERNAL_PORT`: Internal port for direct download links.
- `ANALYTICS_SCRIPT`: The complete script tag needed for tracking from e.g. Plausible (default: empty)
- `ANALYTICS_SCRIPT_CSP`: If the analytics script is located on a different domain, add its origin to the CSP header; e.g. `https://plausible.yourdomain.com`. Must be a single `https://` origin; anything else is ignored with a warning (default: empty). Only anonymised, cookie-free analytics belong here; the default domain allowlist (`ALLOWED_ANALYTICS_DOMAINS`) contains Plausible hosts only, because tag managers can load arbitrary scripts and would void the privacy policy.

These environment variables allow the app to be configured for different deployment scenarios and usage patterns.

INTERNAL_IP and INTERNAL_PORT are configurable in order for you to configure a direct network internal download link if you are on the same network as Snapfile - avoiding proxies for maximum speed.

Also make sure that the uploads and database directories exist on your computer to persist files and the database.

### Reverse proxies and client IPs

Snapfile rate-limits uploads per (hashed) client IP. When it runs behind a reverse proxy, every request arrives from the proxy's address, so Snapfile reads the real client address from the `X-Forwarded-For` header instead.

Each proxy *appends* the address it received the request from, so the trustworthy entry is the one added by your own proxy, counted from the right. `TRUSTED_PROXY_COUNT` tells Snapfile how many proxies to trust:

- `1` (default): one proxy such as Nginx, Traefik or Caddy directly in front of Snapfile.
- `2`: for example a CDN in front of your own proxy.
- `0`: clients connect directly to Snapfile. The header is ignored completely.

Anything to the left of the trusted entries is client-supplied and never used, so a forged header cannot be used to dodge the quota.

### Container hardening

The provided `docker-compose.yml` runs the container with `no-new-privileges`, drops all Linux capabilities and mounts the root filesystem read-only; only the two data volumes are writable. The image runs as a non-root user, keeps the application code root-owned, and contains only the runtime dependencies (no pip, no development tooling).

## Accessing the web interface

Visit http://localhost:8080

## Command line usage

You don't need a browser to share a file. `PUT` the raw file body to `/` or `/<filename>` and Snapfile answers in plain text: the absolute, single-use download link on the first line and your remaining quota on the second. That makes it trivial to use from scripts, servers without a desktop, or a quick `ssh` session.

```sh
# Upload a file; the file name is taken from the URL path (curl -T appends it for you)
curl -T ./report.pdf https://snapfile.me/
# https://snapfile.me/download/Ab3dEf9hIjKl
# You have 4 uploads left. Quota resets in 0 hours, 59 minutes.

# Give the file a different name
curl -T ./report.pdf https://snapfile.me/q3-report.pdf

# Pipe from stdin
tar cz ./project | curl -T - https://snapfile.me/project.tgz

# Capture just the link in a variable
LINK=$(curl -sS --fail -T ./report.pdf https://snapfile.me/ | head -n1)
```

Other tools work the same way:

```sh
# wget
wget -qO- --method=PUT --body-file=./report.pdf https://snapfile.me/report.pdf

# Python
python -c "import sys,urllib.request as u; print(u.urlopen(u.Request('https://snapfile.me/report.pdf', data=open('report.pdf','rb').read(), method='PUT')).read().decode())"
```

Whoever receives the link downloads it with `curl -OJ <link>` (or a browser). The `X-Landing-Url` response header carries the link to the human-friendly landing page for the same file, which is safer to post in chat tools that preview links.

Things to know:

- The same quota, size limit and expiry apply as for uploads from the web page.
- A file larger than `MAX_FILE_SIZE` is refused with `413` before the body is sent when the client uses `Expect: 100-continue` (curl does for bodies over 1 MB). Quota exhaustion returns `429`.
- File names are sanitised on the server; a missing name (for example `curl -T - https://snapfile.me/`) becomes `file`.
- The multipart endpoint the web page uses also works from the shell, but returns a relative path: `curl -F file=@report.pdf https://snapfile.me/upload`.
- Set `PUBLIC_BASE_URL` if the links come back with the wrong scheme or host, for example when the proxy in front of Snapfile does not send `X-Forwarded-Proto`.

## Security

Snapfile.me implements comprehensive security measures:

- **Security Headers:** Content-Security-Policy (with nonces), X-Content-Type-Options, X-Frame-Options, Strict-Transport-Security, Referrer-Policy, Permissions-Policy
- **Input Validation:** All user inputs validated and sanitized
- **SQL Injection Protection:** All database queries use parameterized statements
- **XSS Protection:** CSP with nonces, template auto-escaping, analytics script sanitization
- **File Upload Security:** Filename sanitization, size limits, path traversal protection, downloads always served as opaque attachments, partial uploads cleaned up immediately
- **Rate Limiting:** IP-based quota system with hashed IP addresses, atomic quota accounting, configurable trust in `X-Forwarded-For`
- **Docker Security:** Non-root user, root-owned code, read-only root filesystem, no capabilities, minimal base image without pip, regular vulnerability scanning
- **Automated Security Scanning:** Trivy, pip-audit, bandit, and OpenGrep integrated in CI/CD; GitHub Actions pinned to commit SHAs; Dependabot updates
- **SBOM Generation:** Software Bill of Materials (CycloneDX and SPDX) generated for releases

For detailed security information, see [SECURITY_REVIEW.md](SECURITY_REVIEW.md).

### Security Scanning

The project includes automated security scanning:

- **Pre-commit hooks:** Security checks before committing code
- **CI/CD pipelines:** Automated security scanning on every PR and push
- **Dependency scanning:** pip-audit checks for vulnerable Python packages
- **Code analysis:** bandit scans for security issues in Python code
- **Vulnerability scanning:** Trivy scans filesystem and Docker images
- **SAST scanning:** OpenGrep performs static application security testing

To run security scans locally:

```sh
# Install pre-commit hooks
pip install pre-commit
pre-commit install

# Run all pre-commit hooks
pre-commit run --all-files

# Run individual security tools
pip-audit -r requirements.txt -r requirements-dev.txt
bandit -c pyproject.toml -r app/
trivy fs --skip-dirs venv .
```

## Developer notes
This app is set up with automatic versioning with git tags, Docker image deployment and app deployment. That's nice to know if you fork it! Read about [building and deploying automatically](https://theawesomegarage.com/blog/build-and-deploy-locally-using-github-actions-and-webhooks).

### Running locally for development

You can run Snapfile directly on your machine (without Docker) with automatic reload on file changes.

**1. Create and activate a Python virtual environment:**

```sh
python3 -m venv venv
source venv/bin/activate
```

**2. Install dependencies:**

```sh
pip install -r requirements.txt
```

**3. Install the file watcher:**

```sh
pip install watchfiles
```

**4. Create local upload and database directories:**

```sh
mkdir -p /tmp/snapfile/uploads /tmp/snapfile/database
```

**5. Start the app with auto-reload:**

```sh
cd app
UPLOAD_DIR=/tmp/snapfile/uploads DATABASE_DIR=/tmp/snapfile/database watchfiles "python app.py"
```

The app will be available at `http://localhost:8080`.

`watchfiles` watches the current working directory (`app/`) and all its subdirectories for changes to any file type — including Python files, HTML templates, static assets, etc. The app automatically restarts whenever a change is detected.

> **Note:** Templates and static files are resolved relative to `app.py`, so the app can be started from any directory. Running from `app/` simply keeps the watcher scoped to the application files.

### Managing Python dependencies

The project uses `pip-tools` to keep dependencies pinned and reproducible. There are two dependency sets:

- `requirements.in` → `requirements.txt`: runtime dependencies. This is all that goes into the Docker image.
- `requirements-dev.in` → `requirements-dev.txt`: development and security tooling (`pip-audit`, `bandit`, `pip-tools`). It is constrained to the runtime lockfile so both sets always agree on shared packages.

**Install the development tooling** (provides `pip-compile`, `pip-sync`, `pip-audit` and `bandit`):

```sh
pip install -r requirements-dev.txt
```

**Regenerate the lockfiles** (resolve current versions without upgrading):

```sh
pip-compile --strip-extras requirements.in
pip-compile --strip-extras requirements-dev.in
```

**Audit dependencies for known vulnerabilities:**

```sh
pip-audit -r requirements.txt -r requirements-dev.txt
```

`pip-audit` checks all packages against the Python Packaging Advisory Database (PyPI) and OSV. Fix any reported vulnerabilities before deploying. Dependabot is configured to open weekly pull requests for Python packages, GitHub Actions and the Docker base image.

**Upgrade dependencies safely:**

It's recommended to audit before and after upgrading, and to test the app between steps.

```sh
# 1. Check for vulnerabilities in current dependencies
pip-audit -r requirements.txt

# 2. Upgrade all dependencies to their latest compatible versions
pip-compile --upgrade --strip-extras requirements.in
pip-compile --upgrade --strip-extras requirements-dev.in

# 3. Install the upgraded dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 4. Sync your environment (removes packages not in the lockfiles)
pip-sync requirements.txt requirements-dev.txt

# 5. Audit the upgraded dependencies for new vulnerabilities
pip-audit -r requirements.txt -r requirements-dev.txt

# 6. Run the app and verify everything works
cd app
UPLOAD_DIR=/tmp/snapfile/uploads DATABASE_DIR=/tmp/snapfile/database python app.py
```

To upgrade a **single package** instead of everything:

```sh
pip-compile --upgrade-package aiohttp --strip-extras requirements.in
pip-compile --strip-extras requirements-dev.in
pip install -r requirements.txt -r requirements-dev.txt
pip-sync requirements.txt requirements-dev.txt
```

**Run static security analysis** on the application code:

```sh
bandit -c pyproject.toml -r app/
```

`bandit` scans Python code for common security issues.
