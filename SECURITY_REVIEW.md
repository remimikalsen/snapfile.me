# Security Review - Snapfile.me

**Review Date:** 2026-09-03 (supersedes the review of 2025-12-27)
**Reviewer:** AI-assisted security audit (Claude), verified locally with pip-audit, bandit, Trivy, OpenGrep, the unit suite and the Playwright end-to-end suite
**Project:** Snapfile.me, version 1.3.7 at the time of review

---

## Addendum: audit of 2026-09-04 (redesign branch)

A second whole-system pass was made after the ArktIQ IT sponsored redesign. No high or critical issues were found; all medium and low findings below were fixed in the same change set, with regression tests in `tests/unit/test_hardening.py`.

| Severity | Finding | Fix |
|----------|---------|-----|
| Medium | Single-use was not atomic: the row was deleted 5 s after serving, so concurrent or repeated requests within the window all received the file | `DELETE … RETURNING` claims the row before serving; the second request gets 404 and the landing page reports the file as gone |
| Medium | Quota keyed on the exact IPv6 address, giving a /64 holder 2^64 identities | IPv6 normalised to its /64 network before hashing (`normalize_ip`) |
| Medium | No storage budget or free-space check; a pool of addresses could fill the disk | `MIN_FREE_DISK_BYTES` reserve check and optional `STORAGE_BUDGET_BYTES` cap, HTTP 507; file sizes now stored |
| Medium | Compose published port 8080 on all interfaces while trusting `X-Forwarded-For` | Port bound to `127.0.0.1`; optional `TRUSTED_PROXY_IPS` restricts header trust to known proxy addresses; startup warnings when unset |
| Low | Request host reflected into canonical, Open Graph and CLI links when `PUBLIC_BASE_URL` is empty | Documented as required in production; startup warning when unset |
| Low | CSP carried `'unsafe-inline'` for styles and Google Fonts hosts; default analytics allowlist included Google Tag Manager; `ANALYTICS_SCRIPT_CSP` spliced in unvalidated | `style-src 'self'; font-src 'self'`; allowlist reduced to Plausible hosts; CSP origin validated as a single https origin |
| Low | File names over ~200 characters caused an unhandled 500 | Names truncated to 150 characters keeping the extension |
| Low | Cross-site check relied solely on `Sec-Fetch-Site` | `Origin` header fallback: mismatching or `null` origins are rejected; header-less clients (curl) still allowed |
| Low | No upload timeouts or concurrency cap; slow clients held slots indefinitely | 60 s per-chunk timeout (408) and a 20-upload semaphore (503), both configurable |
| Low | Landing page cacheable; showed the internal address even when unset | `Cache-Control: no-store`; direct-IP paragraph rendered only when `INTERNAL_IP` is configured |
| Info | Browser uploads streamed oversized files before rejection | Client-side size check before sending; server rejects clearly oversized `Content-Length` with 413 |
| Info | Health check rendered the front page every 30 s and filled the access log | `/healthz` returns 204 and is excluded from the access log |
| Info | No index on `files.download_code` | Index added at start-up |

Privacy-related changes made in the same period and verified: IP hashes are keyed with a per-instance secret stored with mode 600; download codes are redacted from access log paths and referrers; fonts are self-hosted so a page load makes no third-party request; `robots.txt` disallows the consuming download URLs; landing and error pages are `noindex`.

Residual, accepted: naive local timestamps (a DST change shifts deadlines by an hour); the in-memory salt fallback when the data directory is unwritable resets quotas on restart; the direct-IP download is plain HTTP by design and is explained in the privacy policy.

---

## Executive Summary

The previous review rated the project highly, and the fundamentals (parameterized SQL, autoescaped templates, nonce-based CSP, non-root container, automated scanning) still hold. This audit went a level deeper than a checklist and found a number of concrete weaknesses, all of which have been fixed in the accompanying change set:

| Severity | Finding | Status |
|----------|---------|--------|
| High | 34 known vulnerabilities in pinned dependencies (aiohttp 3.13.3, werkzeug 3.1.5, and transitive deps of dev tools) | Fixed: dependencies upgraded, pip-audit clean |
| High | Development/security tooling (pip-audit, bandit and ~40 transitive packages incl. requests, urllib3, rich) was installed into the production image | Fixed: split into `requirements-dev.txt`, not in image |
| High | `X-Forwarded-For` was trusted unconditionally and the *leftmost* entry was used, so any client could forge its IP and bypass the upload quota entirely | Fixed: `TRUSTED_PROXY_COUNT`, rightmost trusted hop |
| High | An aborted upload (client disconnect) left the partial file on disk without a database row or quota charge; files were only reaped by the daily consistency check. Repeating this fills the disk without ever hitting the quota | Fixed: partial file removed and quota released in `finally` |
| Medium | Quota was checked *before* the upload and incremented *after* it, so N parallel requests could all pass the check (time-of-check/time-of-use) | Fixed: atomic `UPDATE ... WHERE uses < MAX` reservation |
| Medium | Uploaded files were served with a guessed `Content-Type` (e.g. `text/html`, `image/svg+xml`) | Fixed: always `application/octet-stream` + `no-store` |
| Medium | A multipart part without a filename crashed the handler (`secure_filename(None)`), returning a 500 | Fixed: returns 400 |
| Medium | The consistency check deleted *any* file in the upload directory not yet in the database, including uploads still streaming to disk (data loss) | Fixed: one-hour grace period based on mtime |
| Medium | GitHub Actions referenced by mutable tags; OpenGrep binary downloaded without checksum verification | Fixed: all actions pinned to commit SHAs, OpenGrep sha256 verified |
| Low | CSP lacked `object-src`, `base-uri`, `form-action`, `frame-ancestors` | Fixed |
| Low | No CSRF protection on `/upload` (a third-party page could drain a visitor's quota) | Fixed: `Sec-Fetch-Site: cross-site` is rejected |
| Low | `bandit` configuration in `pyproject.toml` was invalid (`tests = ["app/"]`), so `bandit -c pyproject.toml` ran zero checks | Fixed |
| Low | `requirements.in` was UTF-16 encoded with CRLF line endings | Fixed: UTF-8 |
| Low | Container: app user owned the application code; no `read_only`, `cap_drop` or `no-new-privileges`; pip left in image | Fixed |
| Low | `/download/<code>` returned HTTP 200 with a "not found" page; per-client JSON was cacheable | Fixed: 404 and `Cache-Control: no-store` |
| Low | Duplicate unconditional tag/login/push steps in `build.yaml` | Fixed: removed |
| Info | `ip_usage` had no uniqueness constraint; concurrent first uploads could create duplicate rows | Fixed: rows de-duplicated at start-up, unique index added |
| Info | Fire-and-forget deletion tasks were not referenced, so they could be garbage-collected mid-run | Fixed: task registry |

**Overall Security Grade after remediation: A-** (solid controls; residual risks are inherent to an anonymous file drop and listed below).

---

## 1. Dependency Security

- Runtime dependencies (`requirements.txt`) and development tooling (`requirements-dev.txt`) are separate lockfiles; the dev set is constrained to the runtime set.
- `pip-audit` reports no known vulnerabilities in either lockfile as of the review date.
- `aiohttp` 3.14.3, `werkzeug` 3.1.8, `APScheduler` 3.11.3, `jinja2` 3.1.6, `aiohttp-jinja2` 1.6, `aiosqlite` 0.22.1, `aiofiles` 25.1.0.
- Dependabot is configured for pip, GitHub Actions and the Docker base image (weekly).
- **Residual:** the `python:3.14-slim` base image carries Debian-level HIGH findings in `perl` with no fix available upstream (`affected` / `fix_deferred`). Perl is not used by the application. CI blocks only on CRITICAL, which is appropriate here; re-evaluate when a fixed base image ships.

## 2. Input Validation & Sanitization

- Filenames pass through `werkzeug.utils.secure_filename`; the resulting name is ASCII `[A-Za-z0-9_.-]` only, so it is safe inside `Content-Disposition` and file paths.
- A part without a filename, or a name that sanitizes to nothing, returns 400.
- Download codes are validated as exactly 12 alphanumeric characters before any database access.
- The analytics `<script>` tag from the environment is re-built from an allowlist of attributes and domains; inline code is rejected.

## 3. SQL Injection

- All queries are parameterized. No string interpolation into SQL.

## 4. Cross-Site Scripting

- `aiohttp_jinja2` enables Jinja2 autoescaping by default (verified in the installed library, not assumed).
- Scripts are nonce-based; `'unsafe-inline'` is only used for styles.
- Downloads are always `application/octet-stream` with `Content-Disposition: attachment`, so an uploaded HTML/SVG file is never rendered in the site's origin.
- CSP: `default-src 'self'; script-src 'self' 'nonce-…'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'` (+ fonts and optional analytics origin).

## 5. Security Headers

`Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: same-origin`, `Permissions-Policy`, optional `Strict-Transport-Security` (when `HTTPS_ONLY=true`), `Server` header removed. Per-client JSON and file downloads carry `Cache-Control: no-store`.

## 6. Upload Handling & Abuse Prevention

- Quota reservation is a single atomic `UPDATE … WHERE uses < MAX`, so concurrent uploads cannot exceed the limit. A failed upload (bad request, oversize, disconnect) releases its slot and its partial file.
- `X-Forwarded-For` is honoured only for `TRUSTED_PROXY_COUNT` hops counted from the right; `0` ignores the header.
- Cross-site browser uploads are refused via `Sec-Fetch-Site`.
- Files are streamed to disk in chunks and rejected once `MAX_FILE_SIZE` is exceeded.
- **Residual (by design):** a legitimate user can still upload `MAX_USES_QUOTA × MAX_FILE_SIZE` bytes per renewal window per IP; size the volume accordingly. Uploads behind large NATs share one quota.
- **Residual (by design):** a download link stays valid for a 5-second grace period after the first download, so a second request inside that window also succeeds. This is intentional so link-preview bots do not consume the file before the recipient.

## 7. Path Traversal

- File paths are built from a server-generated UUID plus the sanitized name, stored in the database, and never derived from request data at download time.

## 8. Data Protection & Cleanup

- IPs are stored only as SHA-256 hashes.
- Files are deleted after download (5 s grace) or on expiry; the consistency check removes orphans older than one hour and drops database rows whose files are gone.
- **Residual:** data at rest is not encrypted by the application; use an encrypted volume if that matters for your deployment. Hashed IPs are unsalted, which is sufficient for quota bookkeeping but not a strong anonymization of IPv4 addresses.

## 9. Container & Deployment

- Non-root `appuser`; application code root-owned and read-only to the app; only `/app/uploads` and `/app/database` writable.
- No pip or build tooling in the final image.
- `docker-compose.yml`: `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, `tmpfs /tmp`.
- Health check via Python only.
- **Recommendation:** terminate TLS at a reverse proxy and set `HTTPS_ONLY=true`; set `TRUSTED_PROXY_COUNT` to match the number of proxies (or `0` if none).

## 10. CI/CD & Supply Chain

- All GitHub Actions pinned to full commit SHAs with the version in a comment; Dependabot keeps them current.
- OpenGrep binary download is checksum-verified.
- `pip-audit` runs against both lockfiles; `bandit` runs with the project configuration; Trivy scans filesystem and image; SBOMs (CycloneDX, SPDX) are attached to releases.
- **Residual:** `build.yaml` uses `curl … | python3` to parse GitHub API JSON. This is data parsing, not remote code execution, but it is flagged by OpenGrep; consider switching to `gh api` if the warning is unwanted.

## 11. Logging

- Minimal application logging (`snapfile` logger) for failed file removals; aiohttp access logs to stdout. No secrets or download codes are logged by the application.

---

## Testing performed

- Unit tests: 18 (5 pre-existing + 13 new covering upload/download hardening, XFF trust, atomic quota under 10 concurrent uploads, aborted-upload cleanup, consistency-check grace period, legacy database migration) — pass on Python 3.12 and 3.14.
- End-to-end (Playwright) tests: pass against the hardened container started via `docker-compose.yml`.
- `pip-audit`: clean for both lockfiles. `bandit`: no findings. `flake8`/`black`: clean.
- Trivy image scan: 0 fixable CRITICAL/HIGH findings; 0 findings of any severity in Python packages; the only remaining findings are unfixable Debian `perl-base` CVEs in the base image (see section 1).

## Manual Testing Recommendations

1. Behind your real proxy, upload with a forged `X-Forwarded-For: 1.2.3.4` and confirm the quota is still tracked per real client.
2. Upload an `.html` file and confirm the browser downloads it rather than rendering it.
3. Start a large upload, cancel it, and confirm no file remains in the uploads volume and the quota is unchanged.
