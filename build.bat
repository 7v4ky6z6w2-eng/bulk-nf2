@echo off
REM ============================================================
REM  build.bat — Compilation de l'exe AffichagePrix
REM  À lancer depuis la RACINE du projet (là où se trouve src/)
REM ============================================================
REM
REM PRÉREQUIS :
REM   py -3.8-32 -m pip install fdb requests Pillow pyinstaller
REM   (l'interface utilise Tkinter, fourni avec Python — aucune dépendance Qt)
REM   (Pillow sert à afficher les photos produits WooCommerce)
REM
REM CLIENT FIREBIRD (fbclient.dll 32 bits) :
REM   Placez fbclient.dll dans le dossier racine du projet (là où
REM   se trouve ce fichier build.bat) AVANT de lancer ce script.
REM   La DLL sera automatiquement embarquée dans l'exe.
REM
REM   Où trouver fbclient.dll 32 bits ?
REM     - Sur le serveur Netfact (si Firebird 32 bits) :
REM         C:\Program Files\Firebird\Firebird_2_5\bin\fbclient.dll
REM     - OU télécharger "Firebird 2.5 Windows 32-bit client" sur firebirdsql.org
REM       et extraire fbclient.dll de l'archive.
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
set "FBCLIENT=fbclient.dll"

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

REM Embarquer fbclient.dll dans l'exe si présente dans le dossier racine
set "FBCLIENT_OPT="
if exist "%FBCLIENT%" (
    set "FBCLIENT_OPT=--add-binary %FBCLIENT%;."
    echo fbclient.dll trouvee — sera embarquee dans l'exe.
) else (
    echo ATTENTION : fbclient.dll introuvable dans le dossier racine.
    echo   L exe ne pourra pas se connecter a Firebird sur le poste kiosque.
    echo   Placez fbclient.dll ici et relancez build.bat.
    echo.
)

echo.
echo === Compilation de %NAME% ===
echo.

py -3.8-32 -m PyInstaller ^
    --onedir ^
    --windowed ^
    --name "%NAME%" ^
    --noconsole ^
    --hidden-import PIL._tkinter_finder ^
    --hidden-import PIL.ImageTk ^
    --collect-all PIL ^
    %ICON_OPT% ^
    %ASSETS_OPT% ^
    %FBCLIENT_OPT% ^
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
echo Dossier    : dist\%NAME%\
echo Executable : dist\%NAME%\%NAME%.exe
echo.

endlocal
pause
