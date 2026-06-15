@echo off
rem CS2 Scout — LOCAL sender. One page: paste a match link, it logs into FACEIT,
rem signs the demo links, and sends them to your server (set in Settings).
rem View the analytics on the server's website (http://SERVER_IP:8501).
cd /d C:\Analytics
set SCOUT_ROLE=local
.venv\Scripts\python.exe -m streamlit run src\scout\ui\app.py
pause
