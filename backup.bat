@echo off
REM ERP database backup. Default: C:\erp_backup (USB drive recommended).
REM Uses sqlite3 backup API via scripts/backup_db.py (consistent hot backup).
cd /d C:\inventory-erp
uv run python scripts/backup_db.py
if errorlevel 1 (
  echo Backup failed.
  if /I "%1" NEQ "nopause" pause
  exit /b 1
)
echo.
if /I "%1" NEQ "nopause" pause
