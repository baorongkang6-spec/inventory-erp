@echo off
REM ERP 数据库一键备份。默认备份到 C:\erp_backup，建议改成 U盘/移动硬盘盘符。
REM 使用 sqlite3 backup API，避免直接复制运行中的 db.sqlite3 导致备份不一致。
cd /d C:\inventory-erp
powershell -NoProfile -Command "$bk='C:\erp_backup'; if(!(Test-Path $bk)){New-Item -ItemType Directory $bk ^| Out-Null}; $f=Join-Path $bk ('db_'+(Get-Date -Format yyyyMMdd_HHmmss)+'.sqlite3'); $code='import sqlite3, pathlib; src=pathlib.Path(r\"C:\inventory-erp\db.sqlite3\"); dst=pathlib.Path(r\"' + $f + '\"); s=sqlite3.connect(src); d=sqlite3.connect(dst); s.backup(d); d.close(); s.close(); print(\"Backup OK -> \" + str(dst))'; uv run python -c $code"
if errorlevel 1 (
  echo Backup failed.
  if /I "%1" NEQ "nopause" pause
  exit /b 1
)
echo.
if /I "%1" NEQ "nopause" pause
