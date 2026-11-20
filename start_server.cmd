@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please install Python 3.11+ and follow docs/SERVER_README.md to create .venv first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m server.mock_llm %*
pause
