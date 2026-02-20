#!/usr/bin/env python3
"""
Data Retention Job – purge old chat messages automatically.

Policy (configurable via env / CLI flags):
  - chat_messages older than RETENTION_DAYS are deleted
  - access_logs are NEVER deleted (immutable audit trail)
  - chat_sessions with zero remaining messages are NOT deleted
    (we keep the session row so we know the employee was a user)

Recommended cron: run once a day, e.g.
  0 2 * * * /usr/bin/python3 /app/scripts/data_retention.py >> /var/log/retention.log 2>&1

Usage:
  python scripts/data_retention.py [--dry-run] [--days 365]
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, func, select

from app.models.database import AsyncSessionLocal, ChatMessage, init_db
from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_RETENTION_DAYS = 365


async def run_retention(retention_days: int, dry_run: bool) -> None:
    await init_db()

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    print(f"Retention policy: delete chat_messages older than {cutoff.date()} "
          f"({retention_days} days)")

    async with AsyncSessionLocal() as db:
        # Count first
        count_result = await db.execute(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.timestamp < cutoff
            )
        )
        count = count_result.scalar_one()
        print(f"Messages eligible for deletion: {count}")

        if count == 0:
            print("Nothing to delete.")
            return

        if dry_run:
            print("DRY RUN – no rows deleted. Remove --dry-run to execute.")
            return

        # Delete in batches to avoid locking the DB for too long
        BATCH_SIZE = 1000
        total_deleted = 0

        while True:
            # Fetch a batch of IDs to delete
            id_result = await db.execute(
                select(ChatMessage.id)
                .where(ChatMessage.timestamp < cutoff)
                .limit(BATCH_SIZE)
            )
            ids = [row[0] for row in id_result.fetchall()]
            if not ids:
                break

            await db.execute(
                delete(ChatMessage).where(ChatMessage.id.in_(ids))
            )
            await db.commit()
            total_deleted += len(ids)
            print(f"  Deleted batch: {len(ids)} rows (total so far: {total_deleted})")

        print(f"\nRetention complete. Total deleted: {total_deleted} messages.")
        logger.info(
            "Data retention completed",
            deleted=total_deleted,
            cutoff=cutoff.isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat message data retention job")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Retain messages for N days (default: {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count eligible rows but do not delete",
    )
    args = parser.parse_args()
    asyncio.run(run_retention(args.days, args.dry_run))


if __name__ == "__main__":
    main()
