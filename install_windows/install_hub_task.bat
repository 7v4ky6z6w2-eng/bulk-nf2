@echo off
REM ── Demarrage automatique du hub SANS NSSM (tache planifiee a l'ouverture ───
REM    de session). Alternative a install_hub.bat si vous ne voulez pas
REM    telecharger nssm.exe. A lancer en Administrateur sur le magasin 1.
REM
REM La tache lance start_hub.bat (fenetre reduite) a CHAQUE ouverture de
REM session Windows : sur un poste qui ouvre sa session automatiquement au
REM demarrage (cas habituel d'un poste caisse), le hub demarre donc tout seul.

setlocal

set SCRIPT=%~dp0start_hub.bat
if not exist "%SCRIPT%" (
  echo ERREUR : start_hub.bat introuvable a cote de ce script.
  pause
  exit /b 1
)

echo Creation de la tache planifiee PrimeNFHubServer (a l'ouverture de session)...
schtasks /create /f /tn "PrimeNFHubServer" /sc onlogon /rl highest ^
  /tr "cmd /c start \"PrimeNF Hub\" /min \"%SCRIPT%\""
if errorlevel 1 (
  echo ERREUR : impossible de creer la tache. Lancez ce script en Administrateur.
  pause
  exit /b 1
)

echo Demarrage immediat du hub (sans attendre la prochaine session)...
schtasks /run /tn "PrimeNFHubServer"

echo.
echo *** Tache PrimeNFHubServer installee ***
echo Le hub demarrera automatiquement a chaque ouverture de session Windows.
echo Tableau de bord : http://localhost:5000
echo (Pour verifier : Planificateur de taches Windows, tache PrimeNFHubServer.)
echo.
pause
endlocal
