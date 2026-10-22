@echo off
setlocal
set "LIT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%LIT_PYTHON%" set "LIT_PYTHON=%~dp0..\work\litworkbench-venv\Scripts\python.exe"
if not exist "%LIT_PYTHON%" (
  echo Python environment missing. Run: uv sync --python 3.12 --extra ocr 1>&2
  exit /b 1
)
"%LIT_PYTHON%" -m litworkbench %*
exit /b %errorlevel%
