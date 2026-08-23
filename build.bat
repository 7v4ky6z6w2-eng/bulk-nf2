@echo off
setlocal
REM Build the Windows .exe with PyInstaller.
REM Run this from anywhere -- it always operates on the repo this file lives in.

cd /d "%~dp0"

echo ============================================================
echo  1/4  Checking for the Python launcher (py -3.11)
echo ============================================================
py -3.11 -c "import sys; print('Python', sys.version)" 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: "py -3.11" did not run. Make sure Python 3.11 is installed
    echo        via the official installer ^(which registers the "py" launcher^)
    echo        even though "python" itself is not on PATH.
    goto :fail
)

echo.
echo ============================================================
echo  2/4  Installing/updating build dependencies
echo ============================================================
py -3.11 -m pip install --quiet --upgrade -r requirements-dev.txt
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. See the output above.
    goto :fail
)

echo.
echo ============================================================
echo  3/4  Checking for fbclient.dll
echo ============================================================
if not exist "packaging\fbclient\fbclient.dll" (
    echo.
    echo WARNING: packaging\fbclient\fbclient.dll not found.
    echo          The .exe will build, but it will NOT be able to connect to
    echo          Firebird until you copy a 64-bit fbclient.dll there and
    echo          rebuild. It's usually already on this machine -- search for
    echo          fbclient.dll under your NetFact2/Firebird install folder.
    echo.
) else (
    echo Found packaging\fbclient\fbclient.dll
)

echo.
echo ============================================================
echo  4/4  Running PyInstaller
echo ============================================================
py -3.11 -m PyInstaller packaging\build.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. See the output above.
    goto :fail
)

echo.
echo ============================================================
echo  Build succeeded
echo ============================================================
echo  Executable: %~dp0dist\erp-woocommerce-sync.exe
echo.
pause
exit /b 0

:fail
echo.
echo Build FAILED.
pause
exit /b 1
