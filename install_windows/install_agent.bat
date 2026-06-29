@echo off
REM ── Installation de l'agent PrimeNF sur un poste magasin (sans Python) ───────
REM Copie PrimeNFAgent.exe et stores.json dans C:\PrimeNFAgent\
REM Enregistre une tâche planifiée qui tourne toutes les 15 min au démarrage.
REM
REM Usage : install_agent.bat <STORE_ID>
REM   Exemple : install_agent.bat 2

setlocal

set STORE_ID=%1
if "%STORE_ID%"=="" (
  echo Usage : install_agent.bat ^<STORE_ID^>
  exit /b 1
)

set DEST=C:\PrimeNFAgent
set EXE=%~dp0..\dist\PrimeNFAgent.exe
set CFG=%~dp0..\stores.json

echo Dossier d'installation : %DEST%
mkdir "%DEST%" 2>nul

echo Copie de PrimeNFAgent.exe...
copy /Y "%EXE%" "%DEST%\PrimeNFAgent.exe"
if errorlevel 1 (
  echo ERREUR : PrimeNFAgent.exe introuvable dans dist\
  exit /b 1
)

echo Copie de stores.json...
copy /Y "%CFG%" "%DEST%\stores.json"
if errorlevel 1 (
  echo ERREUR : stores.json introuvable. Copiez stores.json.example et remplissez-le.
  exit /b 1
)

echo Suppression de l'ancienne tâche planifiée (si existante)...
schtasks /delete /tn "PrimeNFAgent" /f 2>nul

echo Création de la tâche planifiée (toutes les 15 min, au démarrage)...
schtasks /create ^
  /tn "PrimeNFAgent" ^
  /tr "\"%DEST%\PrimeNFAgent.exe\" --store-id %STORE_ID% --once" ^
  /sc MINUTE /mo 15 ^
  /ru SYSTEM ^
  /f ^
  /RL HIGHEST ^
  /StartWhenAvailable

if errorlevel 1 (
  echo ERREUR : Impossible de creer la tache planifiee.
  exit /b 1
)

echo.
echo *** Installation terminee pour le magasin %STORE_ID% ***
echo L'agent se lancera automatiquement toutes les 15 minutes.
echo Verifiez que stores.json est correct dans %DEST%\
endlocal
