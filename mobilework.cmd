@echo off
setlocal
set "MOBILEWORK_DIR=%~dp0"
if not exist "%MOBILEWORK_DIR%.venv\Scripts\python.exe" (
  echo mobilework: missing .venv\Scripts\python.exe 1>&2
  exit /b 2
)
"%MOBILEWORK_DIR%.venv\Scripts\python.exe" -m wiki_maintainer.launcher %* --root "%MOBILEWORK_DIR%."
exit /b %ERRORLEVEL%
