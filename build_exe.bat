@echo off
echo Building ColdShift.exe ...
echo.

pip install -r requirements.txt

pyinstaller --noconfirm --onefile --windowed ^
  --name ColdShift ^
  --hidden-import=customtkinter ^
  --hidden-import=psutil ^
  ColdShift-GUI.py

echo.
echo Build finished.
echo The executable is in the dist\ folder: dist\ColdShift.exe
echo You can copy that single .exe anywhere and run it.
pause
