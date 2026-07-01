@echo off
REM ── Planifie la sauvegarde nocturne du hub (tous les jours à 02:00) ─────────
REM À lancer en Administrateur sur le poste du magasin 1.

setlocal
set SCRIPT=%~dp0backup_hub.bat

schtasks /delete /tn "PrimeNFBackup" /f 2>nul
schtasks /create ^
  /tn "PrimeNFBackup" ^
  /tr "\"%SCRIPT%\"" ^
  /sc DAILY /st 02:00 ^
  /ru SYSTEM ^
  /RL HIGHEST ^
  /f

if errorlevel 1 (
  echo ERREUR : impossible de creer la tache planifiee.
  exit /b 1
)
echo *** Tache "PrimeNFBackup" creee : sauvegarde chaque nuit a 02:00. ***
echo Testez maintenant avec :  "%SCRIPT%"
endlocal
