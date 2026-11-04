@echo off
rem ==========================================================================
rem  ATLAS launcher for Windows. Double-click this file to start ATLAS.
rem
rem   1. finds Python 3.10 or newer - the "py" launcher first, then "python"
rem   2. creates a private virtual environment in .venv on the first run
rem   3. installs requirements.txt only when it changed since the last install
rem   4. tries the optional requirements-extras.txt - ATLAS runs without it
rem   5. starts ATLAS with: python run.py
rem
rem  Written without parenthesised blocks on purpose: folder names that
rem  contain brackets or ampersands cannot break the script this way.
rem ==========================================================================
setlocal EnableExtensions DisableDelayedExpansion
title ATLAS

rem Work from the folder this file is in. pushd also copes with network paths.
pushd "%~dp0" 2>nul || goto :no_folder

if not exist "run.py" goto :not_extracted
if not exist "requirements.txt" goto :not_extracted

echo.
echo   ATLAS launcher
echo   ==============

rem ---- 1. Find Python 3.10 or newer ------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if "%ERRORLEVEL%"=="0" set "PY=py -3"
if defined PY goto :have_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if "%ERRORLEVEL%"=="0" set "PY=python"
if defined PY goto :have_python
goto :no_python

:have_python
set "PYVER="
for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo   Using Python %PYVER%

rem ---- 2. Private virtual environment in .venv -------------------------------
set "VENV_PY=%CD%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :make_venv
"%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto :venv_ready
echo   The .venv folder is broken or out of date - rebuilding it...

:make_venv
if exist ".venv" rmdir /s /q ".venv"
echo   Creating a private Python environment in .venv - one-time setup...
%PY% -m venv ".venv"
if not "%ERRORLEVEL%"=="0" goto :venv_failed
if not exist "%VENV_PY%" goto :venv_failed
echo   Updating pip...
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check --upgrade pip
if not "%ERRORLEVEL%"=="0" echo   Could not update pip - continuing with the bundled version.

:venv_ready
call ".venv\Scripts\activate.bat"

rem ---- 3. Requirements: install only when requirements.txt changed ----------
set "STAMP=.venv\atlas-requirements.txt"
if not exist "%STAMP%" goto :install_reqs
fc /b "requirements.txt" "%STAMP%" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto :reqs_ok

:install_reqs
echo   Installing requirements - this can take a minute the first time...
"%VENV_PY%" -m pip install --disable-pip-version-check -r "requirements.txt"
if not "%ERRORLEVEL%"=="0" goto :pip_failed
copy /y "requirements.txt" "%STAMP%" >nul

:reqs_ok

rem ---- 4. Optional extras: nice to have, never fatal -------------------------
if not exist "requirements-extras.txt" goto :extras_done
set "XSTAMP=.venv\atlas-requirements-extras.txt"
if not exist "%XSTAMP%" goto :install_extras
fc /b "requirements-extras.txt" "%XSTAMP%" >nul 2>&1
if "%ERRORLEVEL%"=="0" goto :extras_done

:install_extras
echo   Installing optional extras - lets ATLAS type and press shortcuts for you...
"%VENV_PY%" -m pip install --disable-pip-version-check -r "requirements-extras.txt"
if "%ERRORLEVEL%"=="0" goto :extras_saved
echo.
echo   Note: the optional extras did not install. ATLAS still works, but it
echo   will not be able to type text or press keyboard shortcuts for you.
echo   To try again later, run:
echo     .venv\Scripts\python -m pip install -r requirements-extras.txt
echo.

:extras_saved
rem Remember this attempt either way, so a failing extra does not slow every start.
copy /y "requirements-extras.txt" "%XSTAMP%" >nul

:extras_done

rem ---- 5. Start ATLAS ---------------------------------------------------------
echo.
echo   Starting ATLAS. Your browser should open by itself - if it does not,
echo   open the address shown below. Keep this window open while you use ATLAS.
echo   To shut it down, press Ctrl+C here or close this window.
echo.
"%VENV_PY%" run.py
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :done
rem These exit codes mean Python was stopped with Ctrl+C - not an error.
if "%RC%"=="-1073741510" goto :done
if "%RC%"=="3221225786" goto :done
echo.
echo   ATLAS stopped because of an error - exit code %RC%.
echo   Scroll up to read the message. README.md has a Troubleshooting section.
goto :fail

rem ---- Problems ---------------------------------------------------------------
:no_python
echo.
echo   ATLAS needs Python 3.10 or newer, and none was found on this PC.
echo.
echo     1. Download Python from  https://www.python.org/downloads/
echo     2. Run the installer and tick "Add python.exe to PATH" on its first screen.
echo     3. Double-click start.bat again.
echo.
echo   If you just installed Python, close this window and try again.
goto :fail

:not_extracted
echo.
echo   start.bat cannot find run.py next to it.
echo   If you opened start.bat from inside a .zip file, extract the whole folder
echo   first: right-click the .zip, choose "Extract All...", then run start.bat
echo   from the extracted folder.
goto :fail

:no_folder
echo.
echo   Could not open the folder that start.bat is in.
goto :fail

:venv_failed
if exist ".venv" rmdir /s /q ".venv"
echo.
echo   Could not create the Python environment in the .venv folder.
echo   Make sure this folder is not read-only and that antivirus software is not
echo   blocking Python, then run start.bat again.
goto :fail

:pip_failed
echo.
echo   Installing the requirements failed - see the messages above.
echo   Check your internet connection and run start.bat again. If it keeps
echo   failing, delete the .venv folder and try once more.
goto :fail

:fail
echo.
pause
popd
exit /b 1

:done
popd
exit /b 0
