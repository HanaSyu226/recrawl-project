#!/usr/bin/env python3
"""
Back up change_data.db using SQLite's online backup API (safe to run
while the crawler is writing to the database), then apply retention:

  - The EARLIEST backup taken in each calendar month is kept forever
    (the "monthly" backup for that month).
  - All other backups are kept for the last 8 weeks (56 days).
  - Weekly backups older than 8 weeks are deleted. Monthly backups
    are never deleted, regardless of age.

Intended to be run weekly (see scheduling notes in the project README
or ask for help setting up Windows Task Scheduler).

Usage:
    python backup.py
"""

import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "change_data.db"
BACKUP_DIR = PROJECT_DIR / "backups"
WEEKLY_RETENTION_DAYS = 8 * 7  # 56 days

FILENAME_RE = re.compile(r"^change_data_(\d{4}-\d{2}-\d{2})_(\d{6})\.db$")


def make_backup():
    """Create a new dated backup using SQLite's backup API."""
    if not DB_PATH.exists():
        sys.exit(f"Error: {DB_PATH} not found.")

    BACKUP_DIR.mkdir(exist_ok=True)

    now = datetime.now()
    dest_path = BACKUP_DIR / f"change_data_{now:%Y-%m-%d}_{now:%H%M%S}.db"

    src = sqlite3.connect(str(DB_PATH))
    dest = sqlite3.connect(str(dest_path))
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()

    print(f"Created backup: {dest_path.name}")
    return dest_path


def parse_backup_date(path):
    """Return the date encoded in a backup filename, or None if it doesn't match."""
    m = FILENAME_RE.match(path.name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d").date()


def apply_retention():
    """Classify each backup as monthly (permanent) or weekly (8-week window),
    delete expired weekly backups, and report what was kept/deleted."""
    backups = []
    for path in BACKUP_DIR.iterdir():
        date = parse_backup_date(path)
        if date is not None:
            backups.append((date, path))

    if not backups:
        print("No backups found to evaluate.")
        return

    # The earliest backup in each (year, month) is the permanent monthly backup.
    monthly_anchors = {}
    for date, path in sorted(backups):
        key = (date.year, date.month)
        if key not in monthly_anchors:
            monthly_anchors[key] = path
    monthly_paths = set(monthly_anchors.values())

    cutoff = datetime.now().date() - timedelta(days=WEEKLY_RETENTION_DAYS)

    kept_monthly, kept_weekly, deleted = [], [], []

    for date, path in sorted(backups):
        if path in monthly_paths:
            kept_monthly.append(path)
        elif date >= cutoff:
            kept_weekly.append(path)
        else:
            path.unlink()
            deleted.append(path)

    print("\nKept (monthly, permanent):")
    for p in kept_monthly:
        print(f"  {p.name}")

    print("\nKept (weekly, within last 8 weeks):")
    for p in kept_weekly:
        print(f"  {p.name}")

    print("\nDeleted (weekly, older than 8 weeks):")
    for p in deleted:
        print(f"  {p.name}")

    print(
        f"\nTotal: {len(kept_monthly)} monthly, {len(kept_weekly)} weekly kept, "
        f"{len(deleted)} deleted"
    )


def main():
    make_backup()
    apply_retention()


if __name__ == "__main__":
    main()
