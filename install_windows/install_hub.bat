@echo off
REM ── Installation du hub PrimeNF sur le poste magasin 1 (toujours allumé) ────
REM Crée le service NSSM qui lance hub_server.py au démarrage.
REM Prérequis : Python installé, pip install -r requirements_hub.txt effectué,
REM             NSSM téléchargé dans install_windows\ ou dans PATH.

setlocal

set ROOT=%~dp0..
set PYTHON=python
set NSSM=%~dp0nssm.exe

REM Vérifier NSSM
if not exist "%NSSM%" (
  echo NSSM introuvable. Téléchargez nssm.exe depuis https://nssm.cc et placez-le
  echo dans install_windows\ puis relancez ce script.
  exit /b 1
)

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
