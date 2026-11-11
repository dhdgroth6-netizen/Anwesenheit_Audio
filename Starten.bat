@echo off
rem Startet das Programm direkt aus dem Quellcode
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt
start "" pythonw anwesenheit_audio.py
