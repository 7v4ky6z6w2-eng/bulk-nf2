@echo off
REM ── PrimeNF Hub - construction des executables Windows ──────────────────────
REM Prerequis : Python 3.11+ avec pyinstaller, fdb, requests, PySide6, openpyxl
REM Peut etre lance de n'importe ou (double-clic depuis install_windows\, ou
REM depuis la racine) : ce script se place TOUJOURS lui-meme dans le dossier
REM racine du projet avant de continuer, ci-dessous.

REM %~dp0 = dossier de CE script (install_windows\) ; ".." = racine du projet
REM (la ou se trouvent sync_agent.py, prime_hub.py, src\, templates\...).
cd /d "%~dp0.."

if not exist "sync_agent.py" (
  echo ERREUR : sync_agent.py introuvable dans %cd%
  echo Ce script doit se trouver dans le dossier install_windows\ A L'INTERIEUR
  echo du projet ^(a cote de sync_agent.py, prime_hub.py, etc.^). Verifiez que
  echo vous n'avez pas deplace/copie ce fichier seul en dehors du projet.
  pause
  exit /b 1
)
echo Dossier de travail : %cd%

REM Detection automatique de la commande Python : certains PC n'ont que le
REM lanceur "py" sur le PATH (pas "python" directement) - on essaie plusieurs
REM commandes et on garde la premiere qui repond.
set PYCMD=
python --version >nul 2>&1 && set PYCMD=python
if not defined PYCMD (py -3.11 --version >nul 2>&1 && set PYCMD=py -3.11)
if not defined PYCMD (py -3.12 --version >nul 2>&1 && set PYCMD=py -3.12)
if not defined PYCMD (py --version >nul 2>&1 && set PYCMD=py)
if not defined PYCMD (
  echo ERREUR : aucune commande Python trouvee ^(essaye : python, py -3.11, py -3.12, py^).
  echo Installez Python : https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
echo Python detecte : %PYCMD%

echo Verification de PyInstaller...
%PYCMD% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERREUR : PyInstaller n'est pas installe pour ce Python.
  echo Installez-le avec :   %PYCMD% -m pip install pyinstaller
  echo Puis relancez ce script.
  pause
  exit /b 1
)

echo [1/2] Construction de PrimeNFAgent.exe (agent de synchronisation)...
%PYCMD% -m PyInstaller --onefile ^
  --name PrimeNFAgent ^
  --add-data "src;src" ^
  --add-data "stores.json.example;." ^
  --hidden-import fdb ^
  --hidden-import requests ^
  --hidden-import pikepdf ^
  --hidden-import pdfplumber ^
  --hidden-import fontTools ^
  sync_agent.py

if errorlevel 1 (
  echo ERREUR : pyinstaller a echoue pour PrimeNFAgent
  pause
  exit /b 1
)

echo [2/3] Construction de PrimeNFHub.exe (application bureau)...
%PYCMD% -m PyInstaller --onefile ^
  --name PrimeNFHub ^
  --add-data "src;src" ^
  --add-data "stores.json.example;." ^
  --add-data "templates;templates" ^
  --hidden-import fdb ^
  --hidden-import requests ^
  --hidden-import PySide6 ^
  --hidden-import flask ^
  prime_hub.py

if errorlevel 1 (
  echo ERREUR : pyinstaller a echoue pour PrimeNFHub
  pause
  exit /b 1
)

echo [3/3] Construction de PrimeNFFournisseurSync.exe (poste du fournisseur)...
%PYCMD% -m PyInstaller --onefile ^
  --name PrimeNFFournisseurSync ^
  --add-data "src;src" ^
  --hidden-import fdb ^
  --hidden-import requests ^
  --hidden-import PySide6 ^
  fournisseur_sync.py

if errorlevel 1 (
  echo ERREUR : pyinstaller a echoue pour PrimeNFFournisseurSync
  pause
  exit /b 1
)

echo.
echo Construction terminee.
echo   dist\PrimeNFAgent.exe            -- a copier sur les postes 2 et 3
echo   dist\PrimeNFHub.exe              -- application bureau (tout poste)
echo   dist\PrimeNFFournisseurSync.exe  -- a copier sur le poste du fournisseur
echo.
pause
