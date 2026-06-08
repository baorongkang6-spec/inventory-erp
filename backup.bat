@echo off
REM ERP 数据库一键备份。默认备份到 C:\erp_backup，建议改成 U盘/移动硬盘盘符。
powershell -NoProfile -Command "$bk='C:\erp_backup'; if(!(Test-Path $bk)){New-Item -ItemType Directory $bk ^| Out-Null}; $f=Join-Path $bk ('db_'+(Get-Date -Format yyyyMMdd_HHmmss)+'.sqlite3'); Copy-Item C:\inventory-erp\db.sqlite3 $f; Write-Host ('Backup OK -> '+$f)"
echo.
pause
