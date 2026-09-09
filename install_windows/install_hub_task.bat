@echo off
REM ── Demarrage automatique du hub SANS NSSM (tache planifiee a l'ouverture ───
REM    de session). Alternative a install_hub.bat si vous ne voulez pas
REM    telecharger nssm.exe. A lancer en Administrateur sur le magasin 1.
REM
REM La tache lance run_hub_hidden.vbs, qui demarre start_hub.bat dans une
REM fenetre COMPLETEMENT CACHEE (pas juste minimisee) -- rien ne s'affiche ni
REM ne flashe a l'ecran, meme pas dans la barre des taches. Sur un poste qui
REM ouvre sa session automatiquement au demarrage (cas habituel d'un poste
REM caisse), le hub demarre donc tout seul, entierement en arriere-plan.
REM Logs : logs\hub.log (plus de console pour les lire directement).

setlocal

set VBS=%~dp0run_hub_hidden.vbs
if not exist "%VBS%" (
  echo ERREUR : run_hub_hidden.vbs introuvable a cote de ce script.
  pause
  exit /b 1
)
if not exist "%~dp0start_hub.bat" (
  echo ERREUR : start_hub.bat introuvable a cote de ce script.
  pause
  exit /b 1
)

echo Creation de la tache planifiee PrimeNFHubServer (a l'ouverture de session)...
schtasks /create /f /tn "PrimeNFHubServer" /sc onlogon /rl highest ^
  /tr "wscript.exe \"%VBS%\""
if errorlevel 1 (
  echo ERREUR : impossible de creer la tache. Lancez ce script en Administrateur.
  pause
  exit /b 1
)

echo Demarrage immediat du hub (sans attendre la prochaine session)...
schtasks /run /tn "PrimeNFHubServer"

echo.
echo *** Tache PrimeNFHubServer installee ***
echo Le hub demarrera automatiquement a chaque ouverture de session Windows,
echo entierement en arriere-plan (aucune fenetre, meme pas minimisee).
echo Tableau de bord : http://localhost:5000
echo Logs : %~dp0..\logs\hub.log
echo.
echo Si le tableau de bord ne repond pas apres quelques secondes : ouvrez le
echo Planificateur de taches Windows, tache PrimeNFHubServer, colonne
echo "Dernier resultat d'execution" -- 0x0 = OK. Sinon, verifiez logs\hub.log
echo ^(ou lancez start_hub.bat a la main pour voir l'erreur en clair^).
echo.
pause
endlocal
