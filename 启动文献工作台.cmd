@echo off
chcp 65001 >nul
setlocal
rem ============================================================================
rem  Personal Library - local literature workbench (double-click to start)
rem  * loopback only (127.0.0.1); data lives in %~dp0..\library (outside source)
rem  * THIS window IS the service: closing it stops background OCR tasks
rem  * Chinese startup text is printed by the Python entry point (UTF-8 safe)
rem ============================================================================

set "LIT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%LIT_PYTHON%" set "LIT_PYTHON=%~dp0..\work\litworkbench-venv\Scripts\python.exe"
if not exist "%LIT_PYTHON%" (
  echo [ERROR] Python environment not found.
  echo Install it with: uv sync --python 3.12 --extra ocr --extra web
  echo or: uv pip install --python ^<python^> -e ".[ocr,web]"
  pause
  exit /b 1
)

rem Default data root: ..\library (sibling of the source tree, never inside it)
set "LIT_WORKSPACE=%~dp0..\library"
if not exist "%LIT_WORKSPACE%" mkdir "%LIT_WORKSPACE%"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem Run the service in THIS window: no extra background console, no hidden process
"%LIT_PYTHON%" -m litworkbench.web --workspace "%LIT_WORKSPACE%" --port 8765
set "LIT_EXIT=%errorlevel%"

echo.
if not "%LIT_EXIT%"=="0" (
  echo [INFO] Service exited with code %LIT_EXIT%; see the message above.
) else (
  echo Service stopped.
)
pause
exit /b %LIT_EXIT%