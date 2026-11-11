@echo off
rem Baut die exe und den Installer neu (installer\AnwesenheitAudio_Setup.exe)
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt pyinstaller
python lizenzen_sammeln.py
python -m PyInstaller --noconfirm --windowed --name AnwesenheitAudio --icon icon.ico --collect-data cv2 --hidden-import pygrabber.dshow_graph --add-data "face_detection_yunet_2023mar.onnx;." anwesenheit_audio.py
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss
pause
