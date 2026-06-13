@echo off
rem CS2 Scout — local analytics website. Double-click to start; browser opens automatically.
cd /d C:\Analytics
.venv\Scripts\python.exe -m streamlit run src\scout\ui\app.py
pause
