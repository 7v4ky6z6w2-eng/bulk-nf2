@echo off
REM ── Lance le serveur hub PrimeNF (utilise par la tache planifiee) ────────────
REM Detecte python automatiquement puis lance hub_server.py depuis la racine
REM du projet. Peut aussi etre lance a la main pour un test.

cd /d "%~dp0.."

REM Les "pause" ci-dessous attendraient indefiniment une touche qui ne
REM viendra jamais si ce script est lance en arriere-plan sans fenetre (voir
REM run_hub_hidden.vbs / install_hub_task.bat) -- sautes dans ce cas.
if not exist "hub_server.py" (
  echo ERREUR : hub_server.py introuvable dans %cd%
  echo Ce script doit rester dans le dossier install_windows\ du projet.
  if not "%PRIMENF_HIDDEN%"=="1" pause
  exit /b 1
)

set PYCMD=
python --version >nul 2>&1 && set PYCMD=python
if not defined PYCMD (py -3.11 --version >nul 2>&1 && set PYCMD=py -3.11)
if not defined PYCMD (py -3.12 --version >nul 2>&1 && set PYCMD=py -3.12)
if not defined PYCMD (py --version >nul 2>&1 && set PYCMD=py)
if not defined PYCMD (
  echo ERREUR : aucun Python trouve. Installez Python 3.11+ ou ajoutez-le au PATH.
  if not "%PRIMENF_HIDDEN%"=="1" pause
  exit /b 1
)

echo Demarrage du hub PrimeNF (%PYCMD%)... Fermez cette fenetre pour l'arreter.
%PYCMD% hub_server.py --db central.db --port 5000

REM Si le serveur s'arrete tout seul, garder la fenetre ouverte pour lire
REM l'erreur -- SAUF si on est lance en arriere-plan sans fenetre (voir
REM run_hub_hidden.vbs / install_hub_task.bat) : un "pause" attendrait une
REM touche qui ne viendra jamais, bloquant le processus indefiniment au lieu
REM de s'arreter proprement.
echo.
echo Le serveur hub s'est arrete.
if not "%PRIMENF_HIDDEN%"=="1" pause
