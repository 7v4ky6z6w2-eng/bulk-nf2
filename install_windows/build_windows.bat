@echo off
REM ── PrimeNF Hub — construction des exécutables Windows ──────────────────────
REM Prérequis : Python 3.11+ avec pyinstaller, fdb, requests, PySide6, openpyxl
REM Lancer depuis la racine du projet : install_windows\build_windows.bat

REM Détection automatique de la commande Python : certains PC n'ont que le
REM lanceur "py" sur le PATH (pas "python" directement) — on essaie plusieurs
REM commandes et on garde la première qui répond.
set PYCMD=
python --version >nul 2>&1 && set PYCMD=python
if not defined PYCMD (py -3.11 --version >nul 2>&1 && set PYCMD=py -3.11)
if not defined PYCMD (py -3.12 --version >nul 2>&1 && set PYCMD=py -3.12)
if not defined PYCMD (py --version >nul 2>&1 && set PYCMD=py)
if not defined PYCMD (
  echo ERREUR : aucune commande Python trouvee ^(essaye : python, py -3.11, py -3.12, py^).
  echo Installez Python : https://www.python.org/downloads/windows/
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
  exit /b 1
)

echo [2/2] Construction de PrimeNFHub.exe (application bureau)...
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
  exit /b 1
)

echo.
echo Construction terminee.
echo   dist\PrimeNFAgent.exe  -- a copier sur les postes 2 et 3
echo   dist\PrimeNFHub.exe    -- application bureau (tout poste)
