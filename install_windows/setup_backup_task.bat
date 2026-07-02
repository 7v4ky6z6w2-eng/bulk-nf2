@echo off
REM ── Planifie la sauvegarde nocturne du hub (tous les jours à 02:00) ─────────
REM À lancer en Administrateur sur le poste du magasin 1.

setlocal
set SCRIPT=%~dp0backup_hub.bat

schtasks /delete /tn "PrimeNFBackup" /f 2>nul
REM Le "scheduled" en argument dit a backup_hub.bat de NE PAS attendre une
REM touche a la fin (personne n'est devant l'ecran a 2h du matin).
schtasks /create ^
  /tn "PrimeNFBackup" ^
  /tr "\"%SCRIPT%\" scheduled" ^
  /sc DAILY /st 02:00 ^
  /ru SYSTEM ^
  /RL HIGHEST ^
  /f

if errorlevel 1 (
  echo ERREUR : impossible de creer la tache planifiee.
  pause
  exit /b 1
)
echo *** Tache "PrimeNFBackup" creee : sauvegarde chaque nuit a 02:00. ***
echo Testez maintenant avec :  "%SCRIPT%"
echo.
pause
endlocal
