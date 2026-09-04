@echo off
REM ── Demarrage automatique du hub SANS NSSM (tache planifiee a l'ouverture ───
REM    de session). Alternative a install_hub.bat si vous ne voulez pas
REM    telecharger nssm.exe. A lancer en Administrateur sur le magasin 1.
REM
REM La tache lance pythonw.exe DIRECTEMENT (pas de cmd.exe, pas de fenetre
REM "start /min") a CHAQUE ouverture de session Windows : pythonw n'a AUCUNE
REM console, donc rien ne s'affiche ni ne flashe a l'ecran -- contrairement a
REM python.exe (meme lance minimise, la fenetre existe et apparait dans la
REM barre des taches). Sur un poste qui ouvre sa session automatiquement au
REM demarrage (cas habituel d'un poste caisse), le hub demarre donc tout seul,
REM entierement en arriere-plan. Les logs vont dans logs\hub.log (plus de
REM console pour les lire).

setlocal

for %%i in ("%~dp0..") do set ROOT=%%~fi

if not exist "%ROOT%\hub_server.py" (
  echo ERREUR : hub_server.py introuvable dans %ROOT%
  pause
  exit /b 1
)

REM pythonw.exe est TOUJOURS installe a cote de python.exe (meme dossier) --
REM on retrouve d'abord python.exe puis on en deduit pythonw.exe, comme
REM start_hub.bat le fait pour python.exe.
set PYTHONW=
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYTHONW (
  if exist "%%~dpi\pythonw.exe" set PYTHONW=%%~dpipythonw.exe
)
if not defined PYTHONW (
  for /f "delims=" %%i in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYTHONW (
    if exist "%%~dpi\pythonw.exe" set PYTHONW=%%~dpipythonw.exe
  )
)
if not defined PYTHONW (
  for /f "delims=" %%i in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYTHONW (
    if exist "%%~dpi\pythonw.exe" set PYTHONW=%%~dpipythonw.exe
  )
)
if not defined PYTHONW (
  for /f "delims=" %%i in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYTHONW (
    if exist "%%~dpi\pythonw.exe" set PYTHONW=%%~dpipythonw.exe
  )
)
if not defined PYTHONW (
  echo ERREUR : pythonw.exe introuvable a cote de python.exe.
  echo Verifiez votre installation Python ^(pythonw.exe doit etre dans le meme
  echo dossier que python.exe^), ou utilisez install_hub.bat ^(service NSSM^) a la place.
  pause
  exit /b 1
)
echo pythonw.exe detecte : %PYTHONW%

echo Creation de la tache planifiee PrimeNFHubServer (a l'ouverture de session)...
schtasks /create /f /tn "PrimeNFHubServer" /sc onlogon /rl highest ^
  /tr "\"%PYTHONW%\" \"%ROOT%\hub_server.py\" --db \"%ROOT%\central.db\" --port 5000"
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
echo Logs : %ROOT%\logs\hub.log
echo (Pour verifier : Planificateur de taches Windows, tache PrimeNFHubServer.)
echo.
pause
endlocal
