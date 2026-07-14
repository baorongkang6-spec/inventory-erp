@echo off
REM ERP one-click update (Run as Administrator).
cd /d C:\inventory-erp
echo [1/6] backup ...
call backup.bat nopause
if errorlevel 1 exit /b 1
echo [2/6] git pull ...
git pull
echo [3/6] uv sync ...
uv sync
echo [4/6] collectstatic ...
uv run python manage.py collectstatic --noinput
echo [5/6] migrate ...
uv run python manage.py migrate
echo [6/6] restart service ...
nssm.exe restart InventoryERP
nssm.exe status InventoryERP
echo.
echo ===== Done =====
pause
