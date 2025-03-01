import pytest
import sqlite3
from datetime import datetime, timedelta

import pytest_asyncio
import aiosqlite

from app.app import ip_reached_quota, init_db, MAX_USES_QUOTA, QUOTA_RENEWAL_MINUTES


# Fixture to set up a temporary database
@pytest_asyncio.fixture
async def test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr("app.app.DATABASE_PATH", str(db_file))
    await init_db()
    yield str(db_file)


@pytest.mark.asyncio
async def test_quota_no_record(test_db):
    # For an IP that has no record, quota should not be reached.
    ip_hash = "no_record_ip"
    result = await ip_reached_quota(ip_hash)
    assert result is False


@pytest.mark.asyncio
async def test_quota_not_reached(test_db):
    ip_hash = "not_reached_ip"
    now = datetime.now()
    # Insert a record with uses less than MAX_USES_QUOTA and a recent last_access.
    async with aiosqlite.connect(test_db, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute(
            "INSERT INTO ip_usage (ip, uses, last_access) VALUES (?, ?, ?)",
            (ip_hash, MAX_USES_QUOTA - 1, now),
        )
        await db.commit()
    result = await ip_reached_quota(ip_hash)
    assert result is False


@pytest.mark.asyncio
async def test_quota_reached(test_db):
    ip_hash = "quota_reached_ip"
    now = datetime.now()
    # Insert a record with uses equal to MAX_USES_QUOTA.
    async with aiosqlite.connect(test_db, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute(
            "INSERT INTO ip_usage (ip, uses, last_access) VALUES (?, ?, ?)",
            (ip_hash, MAX_USES_QUOTA, now),
        )
        await db.commit()
    result = await ip_reached_quota(ip_hash)
    assert result is True


@pytest.mark.asyncio
async def test_quota_expired(test_db):
    ip_hash = "expired_ip"
    # Create a last_access timestamp that is older than the renewal window.
    past_time = datetime.now() - timedelta(minutes=QUOTA_RENEWAL_MINUTES + 1)
    async with aiosqlite.connect(test_db, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        await db.execute(
            "INSERT INTO ip_usage (ip, uses, last_access) VALUES (?, ?, ?)",
            (ip_hash, MAX_USES_QUOTA, past_time),
        )
        await db.commit()
    # The expired record should be removed, and quota not reached.
    result = await ip_reached_quota(ip_hash)
    assert result is False
    # Verify that the record has been deleted.
    async with aiosqlite.connect(test_db, detect_types=sqlite3.PARSE_DECLTYPES) as db:
        async with db.execute(
            "SELECT * FROM ip_usage WHERE ip=?", (ip_hash,)
        ) as cursor:
            row = await cursor.fetchone()
    assert row is None
