@echo off
setlocal

REM === CONFIG ===
set PYTHON=python
set "EXCEL=Roadmap.xlsx"
set SHEET=Sheet1
set OUT=data.json
set PORT=8000

REM === Generate data.json ===
%PYTHON% gen_datajson.py "%EXCEL%" --sheet "%SHEET%" -o "%OUT%"
if errorlevel 1 (
  echo Failed to generate %OUT%
  pause
  exit /b 1
)

REM === Start local server (in a new window) ===
start "ELK Server" %PYTHON% -m http.server %PORT%

REM === Open viewer in default browser ===
start "" http://localhost:%PORT%/viewer.html
endlocal