@echo off
rem Launcher for the Zwift ECRO Auto Results scheduled task.
rem Appends all script output to task_run.log for troubleshooting.
cd /d "%~dp0"
echo ===== Run started %date% %time% ===== >> task_run.log
"C:\Users\dirty\AppData\Local\Python\pythoncore-3.14-64\python.exe" -u "ZwiftRacing to Discord (ECRO).py" >> task_run.log 2>&1
echo ===== Run finished %date% %time% (exit %errorlevel%) ===== >> task_run.log
