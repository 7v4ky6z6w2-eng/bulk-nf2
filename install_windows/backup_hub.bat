@echo off
REM ── Sauvegarde nocturne du hub (magasin 1) ──────────────────────────────────
REM  1. central.db  -> snapshot cohérent via l'API backup de sqlite3 (Python),
REM                    sûr même pendant que le hub tourne (mode WAL).
REM  2. DIFA2.FDB   -> gbak -b -g (sauvegarde à chaud officielle Firebird,
REM                    sûre sur une base en cours d'utilisation).
REM  3. Rétention   -> suppression des sauvegardes de plus de 14 jours.
REM
REM À personnaliser ci-dessous puis planifier via setup_backup_task.bat.

setlocal

REM Ce script tourne AUSSI seul chaque nuit via la tâche planifiée (sans
REM personne devant l'écran) : pas de "pause" sans condition, sinon la
REM sauvegarde nocturne resterait bloquée pour toujours en attendant une
REM touche que personne n'appuiera. setup_backup_task.bat passe le mot
REM "scheduled" en argument ; un lancement manuel (double-clic, ou depuis ce
REM guide) n'a pas cet argument et affiche donc "Appuyez sur une touche..."
REM à la fin pour qu'on puisse lire le résultat.
set MODE=%1

REM ======== PARAMÈTRES À ADAPTER ==============================================
set ROOT=%~dp0..
set CENTRAL_DB=%ROOT%\central.db
set BACKUP_DIR=%ROOT%\backups
set FDB_PATH=C:\Netfact\Data\DIFA2.FDB
set GBAK=C:\Program Files (x86)\Firebird\Firebird_2_5\bin\gbak.exe
set FB_USER=SYSDBA
set FB_PASSWORD=masterkey
set RETENTION_DAYS=14
REM ============================================================================

REM Détection automatique de la commande Python (voir build_windows.bat).
set PYCMD=
python --version >nul 2>&1 && set PYCMD=python
if not defined PYCMD (py -3.11 --version >nul 2>&1 && set PYCMD=py -3.11)
if not defined PYCMD (py -3.12 --version >nul 2>&1 && set PYCMD=py -3.12)
if not defined PYCMD (py --version >nul 2>&1 && set PYCMD=py)
if not defined PYCMD (
  echo ERREUR : aucune commande Python trouvee - la sauvegarde central.db sera ignoree.
)

for /f "tokens=1-3 delims=/-. " %%a in ("%DATE%") do set STAMP=%%c-%%b-%%a
mkdir "%BACKUP_DIR%" 2>nul

echo [1/3] Sauvegarde de central.db...
if not defined PYCMD (
  echo   Python introuvable - sauvegarde central.db ignoree.
) else if exist "%CENTRAL_DB%" (
  %PYCMD% -c "import sqlite3; s=sqlite3.connect(r'%CENTRAL_DB%'); d=sqlite3.connect(r'%BACKUP_DIR%\central_%STAMP%.db'); s.backup(d); d.close(); s.close(); print('  central.db -> central_%STAMP%.db')"
  if errorlevel 1 echo   ERREUR sauvegarde central.db
) else (
  echo   central.db introuvable (%CENTRAL_DB%) - ignore.
)

echo [2/3] Sauvegarde de la base Firebird (gbak)...
if exist "%GBAK%" (
  "%GBAK%" -b -g -user %FB_USER% -password %FB_PASSWORD% "%FDB_PATH%" "%BACKUP_DIR%\DIFA2_%STAMP%.fbk"
  if errorlevel 1 (echo   ERREUR gbak) else (echo   DIFA2.FDB -> DIFA2_%STAMP%.fbk)
) else (
  echo   gbak.exe introuvable (%GBAK%) - adaptez la variable GBAK en tete de script.
)

echo [3/3] Rétention : suppression des sauvegardes de plus de %RETENTION_DAYS% jours...
forfiles /p "%BACKUP_DIR%" /m *.db  /d -%RETENTION_DAYS% /c "cmd /c del @path" 2>nul
forfiles /p "%BACKUP_DIR%" /m *.fbk /d -%RETENTION_DAYS% /c "cmd /c del @path" 2>nul

echo Terminé.
if /I not "%MODE%"=="scheduled" (
  echo.
  pause
)
endlocal
