@echo off
REM ============================================================
REM  build.bat — Compilation de l'exe AffichagePrix
REM  À lancer depuis la RACINE du projet (là où se trouve src/)
REM ============================================================
REM
REM PRÉREQUIS :
REM   pip install pyinstaller
REM   (les autres dépendances doivent déjà être installées via requirements.txt)
REM
REM CLIENT FIREBIRD :
REM   La DLL cliente Firebird 2.5 (fbclient.dll, version 32 bits si Python est
REM   32 bits, 64 bits sinon) DOIT être disponible au moment de l'exécution.
REM   Option A : installer le client Firebird 2.5 sur le poste kiosque.
REM   Option B : copier fbclient.dll dans le même dossier que AffichagePrix.exe
REM              (généralement C:\Program Files\Firebird\Firebird_2_5\bin\fbclient.dll
REM               sur le serveur, à récupérer et placer dans dist\).
REM
REM ICÔNE :
REM   Placez votre icône dans assets\app.ico avant de lancer ce script.
REM   Si le fichier est absent, PyInstaller utilisera l'icône par défaut.
REM ============================================================

setlocal

set "SCRIPT=src\main.py"
set "NAME=AffichagePrix"
set "ASSETS=assets"
set "ICON=assets\app.ico"

REM Construire les options de l'icône seulement si le fichier existe
set "ICON_OPT="
if exist "%ICON%" (
    set "ICON_OPT=--icon %ICON%"
)

REM Construire les options d'ajout des assets seulement si le dossier existe
set "ASSETS_OPT="
if exist "%ASSETS%" (
    set "ASSETS_OPT=--add-data %ASSETS%;assets"
)

echo.
echo === Compilation de %NAME% ===
echo.

pyinstaller ^
    --onefile ^
    --windowed ^
    --name "%NAME%" ^
    --noconsole ^
    %ICON_OPT% ^
    %ASSETS_OPT% ^
    --paths src ^
    "%SCRIPT%"

if errorlevel 1 (
    echo.
    echo ERREUR : la compilation a echoue.
    pause
    exit /b 1
)

echo.
echo === Compilation terminee ===
echo Executable : dist\%NAME%.exe
echo.
echo N'oubliez pas de copier fbclient.dll dans dist\ si le client Firebird
echo n'est pas installe sur le poste kiosque.
echo.

endlocal
pause
