@echo off
rem CS2 Scout — local analytics website. Double-click to start; browser opens automatically.
cd /d C:\Analytics
rem Ensure a Chromium is available for the browser auto-download (no-op once installed).
rem Uses system Chrome at runtime when present; this is the fallback browser.
.venv\Scripts\python.exe -m playwright install chromium >nul 2>&1
.venv\Scripts\python.exe -m streamlit run src\scout\ui\app.py
pause
