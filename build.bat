@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  GUI 模式打包：把 app.py 用 PyInstaller 打成单个 exe
REM  产物：dist\file-transfer.exe（双击打开 GUI 窗口，无 CMD 黑框）
REM ============================================================

echo [1/3] Check PyInstaller...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo     Not installed, installing pyinstaller...
    pip install pyinstaller
    if errorlevel 1 (
        echo Install failed!
        pause
        exit /b 1
    )
)

echo [2/3] Clean old build artifacts...
if exist "build\" (
    rmdir /s /q "build"
)
if exist "dist\" (
    rmdir /s /q "dist"
)
if exist "file-transfer.spec" (
    del /q "file-transfer.spec"
)

echo [3/3] Building exe (windowed, no console)...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name file-transfer ^
    --hidden-import PIL._tkinter_finder ^
    --hidden-import PIL.ImageTk ^
    app.py

if errorlevel 1 (
    echo.
    echo XXX Build failed!
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Build success! exe: dist\file-transfer.exe
echo ========================================
echo   Double-click to open GUI window.
echo   No CMD black box. QR code shown directly.
echo.
pause
