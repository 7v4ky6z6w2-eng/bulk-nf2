@echo off
REM ── PrimeNF Hub — construction des exécutables Windows ──────────────────────
REM Prérequis : Python 3.11+ avec pyinstaller, fdb, requests, PySide6, openpyxl
REM Lancer depuis la racine du projet : install_windows\build_windows.bat

echo [1/2] Construction de PrimeNFAgent.exe (agent de synchronisation)...
pyinstaller --onefile ^
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
pyinstaller --onefile ^
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
