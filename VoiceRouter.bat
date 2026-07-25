@echo off
rem Voice Router launcher - finds Python automatically (no hardcoded paths)
setlocal

set "SCRIPT=%~dp0voice_router.py"

rem 1) py launcher (bundled with python.org installer)
where pyw >nul 2>&1
if %errorlevel%==0 (
  start "" pyw "%SCRIPT%"
  goto :eof
)

rem 2) pythonw on PATH
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%SCRIPT%"
  goto :eof
)

rem 3) common install locations
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
  "%USERPROFILE%\anaconda3\pythonw.exe"
  "%USERPROFILE%\miniconda3\pythonw.exe"
  "%LOCALAPPDATA%\anaconda3\pythonw.exe"
  "%PROGRAMDATA%\anaconda3\pythonw.exe"
) do (
  if exist %%P (
    start "" %%P "%SCRIPT%"
    goto :eof
  )
)

echo Python was not found.
echo Please install Python 3.10+ from https://www.python.org/downloads/
echo and make sure "Add Python to PATH" is checked.
pause
