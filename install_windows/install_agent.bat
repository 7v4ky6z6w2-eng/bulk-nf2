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
  echo   Exemple, pour le magasin 2 :   install_agent.bat 2
  echo.
  echo Ce script attend un numero de magasin en argument — il ne fonctionne
  echo pas en simple double-clic. Ouvrez une invite de commandes ^(cmd^) en
  echo Administrateur dans ce dossier et tapez la commande ci-dessus.
  pause
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
  pause
  exit /b 1
)

echo Copie de stores.json...
copy /Y "%CFG%" "%DEST%\stores.json"
if errorlevel 1 (
  echo ERREUR : stores.json introuvable. Copiez stores.json.example et remplissez-le.
  pause
  exit /b 1
)

echo Création de la tâche planifiée (toutes les 15 min, au démarrage)...
REM NOTE : le "/StartWhenAvailable" (rattraper un cycle manque quand le poste
REM etait eteint) n'existe PAS comme option de schtasks.exe en ligne de
REM commande — seule l'API PowerShell l'expose. On delegue donc a
REM setup_task.ps1, qui cree la tache correctement avec cette option.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_task.ps1" ^
  -StoreId %STORE_ID% -ExePath "%DEST%\PrimeNFAgent.exe"

if errorlevel 1 (
  echo ERREUR : Impossible de creer la tache planifiee.
  pause
  exit /b 1
)

echo.
echo *** Installation terminee pour le magasin %STORE_ID% ***
echo L'agent se lancera automatiquement toutes les 15 minutes.
echo Verifiez que stores.json est correct dans %DEST%\
echo.
pause
endlocal
