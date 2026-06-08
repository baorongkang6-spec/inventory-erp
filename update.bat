@echo off
REM ERP 一键更新（右键 → 以管理员身份运行）
cd /d C:\inventory-erp
echo [1/5] git pull ...
git pull
echo [2/5] uv sync ...
uv sync
echo [3/5] collectstatic ...
uv run python manage.py collectstatic --noinput
echo [4/5] migrate ...
uv run python manage.py migrate
echo [5/5] restart service ...
nssm.exe restart InventoryERP
nssm.exe status InventoryERP
echo.
echo ===== Done =====
pause
