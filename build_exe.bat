@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 打包 翻译器.exe ===
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name 翻译器 ^
  --collect-all tkinterdnd2 ^
  --hidden-import translate_pdf ^
  translate_app.py
if errorlevel 1 (
  echo.
  echo [失败] 打包出错，看上面的日志
) else (
  echo.
  echo [完成] 产物：dist\翻译器.exe
)
pause
