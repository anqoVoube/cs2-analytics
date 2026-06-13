@echo off
rem CS2 Scout — local analytics website. Double-click to start; browser opens automatically.
rem The browser auto-download attaches to your installed Google Chrome (no extra download).
cd /d C:\Analytics
.venv\Scripts\python.exe -m streamlit run src\scout\ui\app.py
pause
