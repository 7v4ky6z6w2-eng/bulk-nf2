@echo off
REM ── Demarrage automatique de la synchro fournisseur (tache planifiee) ───────
REM A COPIER SUR LE POSTE DU FOURNISSEUR, dans le MEME dossier que
REM PrimeNFFournisseurSync.exe (ce script cherche l'exe a cote de lui-meme).
REM
REM Cree une tache planifiee qui lance l'exe (fenetre reduite) a CHAQUE
REM ouverture de session Windows : l'appli demarre donc toute seule au
REM demarrage du poste (si la session s'ouvre automatiquement) ou a la
REM connexion, et reste ensuite dans la zone de notification (icone
REM systray) - clic droit dessus pour l'ouvrir/synchroniser/quitter.
REM
REM Ne necessite PAS les droits Administrateur (tache pour l'utilisateur
REM courant uniquement, pas de privilege eleve requis pour cette appli).

setlocal

set EXE=%~dp0PrimeNFFournisseurSync.exe
if not exist "%EXE%" (
  echo ERREUR : PrimeNFFournisseurSync.exe introuvable a cote de ce script.
  echo Copiez ce fichier .bat DANS LE MEME DOSSIER que PrimeNFFournisseurSync.exe.
  pause
  exit /b 1
)

echo Creation de la tache planifiee PrimeNFFournisseurSync (a l'ouverture de session)...
schtasks /create /f /tn "PrimeNFFournisseurSync" /sc onlogon ^
  /tr "cmd /c start \"Synchro fournisseur\" /min \"%EXE%\""
if errorlevel 1 (
  echo ERREUR : impossible de creer la tache.
  pause
  exit /b 1
)

echo Demarrage immediat (sans attendre la prochaine session)...
schtasks /run /tn "PrimeNFFournisseurSync"

echo.
echo *** Tache PrimeNFFournisseurSync installee ***
echo L'appli demarrera automatiquement a chaque ouverture de session Windows,
echo et restera ensuite dans la zone de notification (bas a droite, pres de
echo l'horloge - cliquez sur la fleche ^"^^" si elle est cachee).
echo (Pour verifier/desinstaller : Planificateur de taches Windows, tache
echo  PrimeNFFournisseurSync, ou "schtasks /delete /tn PrimeNFFournisseurSync".)
echo.
pause
endlocal
