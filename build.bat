@echo off
REM Build CaptionBand.exe using PyInstaller.
REM Requires: Python 3.11-3.13 on PATH (or set PYTHON_EXE explicitly).

setlocal

if "%PYTHON_EXE%"=="" (
  if exist C:\Python313\python.exe (
    set "PYTHON_EXE=C:\Python313\python.exe"
  ) else (
    set "PYTHON_EXE=python"
  )
)

echo [build] using Python: %PYTHON_EXE%

REM A DEDICATED venv with only requirements-build.txt. Building from the
REM global interpreter made PyInstaller crawl every package on the machine.
if not exist .venv-build (
  echo [build] creating build venv...
  "%PYTHON_EXE%" -m venv .venv-build
)

call .venv-build\Scripts\activate.bat

echo [build] installing build dependencies...
python -m pip install --upgrade pip
REM Exact pins from the lock when present — reproducible builds. Regenerate
REM the lock with pip-compile whenever requirements-build.txt changes.
if exist requirements-build.lock (
  python -m pip install -r requirements-build.lock
) else (
  python -m pip install -r requirements-build.txt
)
python -m pip install "pyinstaller>=6.0"

echo [build] cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [build] running pyinstaller...
pyinstaller translator.spec --noconfirm

if errorlevel 1 (
  echo [build] FAILED
  exit /b 1
)

echo.
echo [build] OK - app folder at: dist\CaptionBand\
echo.

REM Installer (Inno Setup 6). Per-user install from the Inno installer puts
REM ISCC under %LOCALAPPDATA%; the machine-wide one under Program Files.
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" (
  echo [build] Inno Setup nao encontrado - instale com: winget install JRSoftware.InnoSetup
  echo [build] depois rode: ISCC.exe installer.iss
  exit /b 0
)

echo [build] compiling installer...
"%ISCC%" /Q installer.iss
if errorlevel 1 (
  echo [build] INSTALLER FAILED
  exit /b 1
)
echo [build] OK - installer at: Output\CaptionBandSetup.exe
endlocal