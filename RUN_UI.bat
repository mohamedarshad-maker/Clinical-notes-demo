@echo off
echo =========================================
echo Clinical Notes PoC - Starting UI
echo =========================================
echo.
echo The UI will be available at:
echo   http://localhost:8000
echo.
echo Press Ctrl+C to stop the server
echo =========================================
echo.

.venv\Scripts\python.exe start_ui_only.py

pause
