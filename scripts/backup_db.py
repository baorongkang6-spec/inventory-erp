"""SQLite hot backup via backup API (used by backup.bat on Windows).

Avoids raw Copy-Item on a live db.sqlite3; works while Waitress holds the DB open.
Default destination: C:\\erp_backup (override with ERP_BACKUP_DIR).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "db.sqlite3"
DEFAULT_BK = Path(r"C:\erp_backup") if sys.platform == "win32" else BASE / "backups"


def main() -> int:
    bk_dir = Path(os.environ.get("ERP_BACKUP_DIR", str(DEFAULT_BK)))
    if not SRC.is_file():
        print(f"ERROR: database not found: {SRC}", file=sys.stderr)
        return 1
    try:
        bk_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"ERROR: cannot create backup dir {bk_dir}: {e}", file=sys.stderr)
        return 1
    dst = bk_dir / f"db_{datetime.now():%Y%m%d_%H%M%S}.sqlite3"
    try:
        src = sqlite3.connect(str(SRC))
        dest = sqlite3.connect(str(dst))
        with dest:
            src.backup(dest)
        src.close()
    except sqlite3.Error as e:
        print(f"ERROR: sqlite backup failed: {e}", file=sys.stderr)
        if dst.exists():
            dst.unlink(missing_ok=True)
        return 1
    print(f"Backup OK -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
