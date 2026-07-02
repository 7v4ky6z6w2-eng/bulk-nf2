@echo off
REM ── Installation du hub PrimeNF sur le poste magasin 1 (toujours allumé) ────
REM Crée le service NSSM qui lance hub_server.py au démarrage.
REM Prérequis : Python installé, pip install -r requirements_hub.txt effectué,
REM             NSSM téléchargé dans install_windows\ ou dans PATH.

setlocal

set ROOT=%~dp0..
set NSSM=%~dp0nssm.exe

REM Vérifier NSSM
if not exist "%NSSM%" (
  echo NSSM introuvable. Téléchargez nssm.exe depuis https://nssm.cc et placez-le
  echo dans install_windows\ puis relancez ce script.
  exit /b 1
)

REM NSSM lance le programme directement (sans passer par cmd.exe), donc il lui
REM faut le CHEMIN COMPLET vers python.exe — pas une commande comme "py -3.11"
REM que seul cmd.exe sait interpreter. On le retrouve automatiquement.
set PYTHON=
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYTHON set PYTHON=%%i
if not defined PYTHON (
  for /f "delims=" %%i in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYTHON set PYTHON=%%i
)
if not defined PYTHON (
  for /f "delims=" %%i in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYTHON set PYTHON=%%i
)
if not defined PYTHON (
  for /f "delims=" %%i in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYTHON set PYTHON=%%i
)
if not defined PYTHON (
  echo ERREUR : impossible de trouver python.exe automatiquement.
  echo Modifiez la ligne "set PYTHON=..." en tete de ce script avec le chemin
  echo complet vers python.exe, par exemple :
  echo   set PYTHON=C:\Users\VOTRE_NOM\AppData\Local\Programs\Python\Python311\python.exe
  echo Puis relancez ce script.
  exit /b 1
)
echo python.exe detecte : %PYTHON%

echo Création du service Windows PrimeNFHub...
"%NSSM%" install PrimeNFHub "%PYTHON%" "%ROOT%\hub_server.py"
"%NSSM%" set PrimeNFHub AppDirectory "%ROOT%"
"%NSSM%" set PrimeNFHub DisplayName "PrimeNF Hub"
"%NSSM%" set PrimeNFHub Description "Serveur central PrimeNF — synchronisation multi-magasins"
"%NSSM%" set PrimeNFHub Start SERVICE_AUTO_START
"%NSSM%" set PrimeNFHub AppStdout "%ROOT%\logs\hub_stdout.log"
"%NSSM%" set PrimeNFHub AppStderr "%ROOT%\logs\hub_stderr.log"
"%NSSM%" set PrimeNFHub AppRotateFiles 1
"%NSSM%" set PrimeNFHub AppRotateBytes 5000000

mkdir "%ROOT%\logs" 2>nul

echo Démarrage du service...
"%NSSM%" start PrimeNFHub

echo.
echo *** Service PrimeNFHub installé et démarré ***
echo Tableau de bord : http://localhost:5000
endlocal
