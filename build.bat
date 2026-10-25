@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Deadlock Profile Analyzer - Build v0.5.8

echo =======================================================
echo       DEADLOCK PROFILE ANALYZER v0.5.8 BUILD
echo =======================================================
echo.

set "SCRIPT=deadlock_profile_analyzer_0_5_8.py"
if not exist "%SCRIPT%" (
  echo [ERROR] Missing %SCRIPT%
  echo Copy all package files into this folder before building.
  goto :failed
)
if not exist "deadlock_analyzer.ico" (
  echo [ERROR] Missing deadlock_analyzer.ico
  goto :failed
)
if not exist "version_info.txt" (
  echo [ERROR] Missing version_info.txt
  goto :failed
)

where py >nul 2>&1
if not errorlevel 1 (
  set "PY=py"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Python is not installed on this BUILD computer.
    echo Install Python for Windows and make sure the Python launcher or python command is available.
    goto :failed
  )
  set "PY=python"
)

echo [1/3] Checking PyInstaller...
%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller into your Python environment...
  %PY% -m pip install --upgrade pyinstaller
  if errorlevel 1 goto :failed
)

echo.
echo [2/3] Building standalone Windows executable...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name "Deadlock Profile Analyzer" --icon "deadlock_analyzer.ico" --version-file "version_info.txt" --add-data "deadlock_analyzer.ico;." "%SCRIPT%"
if errorlevel 1 goto :failed

if not exist "dist\Deadlock Profile Analyzer.exe" (
  echo [ERROR] PyInstaller reported success but the EXE is missing.
  goto :failed
)

echo.
echo [3/3] Build completed.
echo.
echo EXE: "%CD%\dist\Deadlock Profile Analyzer.exe"
echo.
echo Python is NOT required on computers running this EXE.
echo The application needs internet access for API lookups.
echo User data: %%LOCALAPPDATA%%\DeadlockProfileAnalyzer
pause
exit /b 0

:failed
echo.
echo BUILD FAILED. Read the error above for details.
pause
exit /b 1
