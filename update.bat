@echo off
REM ERP one-click update (Run as Administrator).
cd /d C:\inventory-erp
echo [1/7] stop service (unlock SQLite for migrate) ...
nssm.exe stop InventoryERP
echo [2/7] backup ...
call backup.bat nopause
if errorlevel 1 exit /b 1
echo [3/7] git pull ...
git pull
echo [4/7] uv sync ...
uv sync
echo [5/7] collectstatic ...
uv run python manage.py collectstatic --noinput
echo [6/7] migrate ...
uv run python manage.py migrate
if errorlevel 1 (
  echo.
  echo ===== MIGRATE FAILED — service NOT restarted. Fix errors above, then: nssm start InventoryERP =====
  exit /b 1
)
echo [7/7] start service ...
nssm.exe start InventoryERP
nssm.exe status InventoryERP
echo.
echo ===== Done =====
pause
