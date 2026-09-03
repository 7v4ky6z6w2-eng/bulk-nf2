@echo off
REM ── PrimeNF - construction de PrimeNFFournisseurSync.exe SEUL ───────────────
REM Comme build_windows.bat, mais ne construit QUE l'exe du poste fournisseur
REM (pas Agent ni Hub) -- pour itérer vite sans attendre les 3 builds a chaque
REM fois pendant le developpement/debug de fournisseur_sync.py.
REM Prerequis : Python 3.11+ avec pyinstaller, fdb, requests, PySide6.

REM %~dp0 = dossier de CE script (install_windows\) ; ".." = racine du projet
cd /d "%~dp0.."

if not exist "fournisseur_sync.py" (
  echo ERREUR : fournisseur_sync.py introuvable dans %cd%
  echo Ce script doit se trouver dans le dossier install_windows\ A L'INTERIEUR
  echo du projet ^(a cote de fournisseur_sync.py, sync_agent.py, etc.^).
  pause
  exit /b 1
)
echo Dossier de travail : %cd%

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

echo Construction de PrimeNFFournisseurSync.exe (poste du fournisseur)...
REM --windowed : pas de fenetre console a cote de la fenetre/tray Qt (sinon
REM une console noire reste ouverte tant que l'appli tourne en arriere-plan,
REM y compris quand install_fournisseur_sync_task.bat lance l'exe via
REM "cmd /c start ...". Les sorties --once/--backlog-days vont alors dans
REM fournisseur_sync.log (voir main()) plutot que dans une console.
%PYCMD% -m PyInstaller --onefile --windowed ^
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
echo   dist\PrimeNFFournisseurSync.exe  -- a copier sur le poste du fournisseur
echo.
pause
