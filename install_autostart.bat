@echo off
REM ============================================================
REM  install_autostart.bat — Demarrage automatique du kiosque
REM  Cree un raccourci vers AffichagePrix.exe dans le dossier
REM  Demarrage de l'utilisateur courant (shell:startup).
REM ============================================================
REM  A lancer depuis le dossier contenant ce script OU depuis dist\.
REM  L'exe doit exister dans le sous-dossier dist\ par rapport a ce script.
REM ============================================================

setlocal

REM Chemin de l'exe (dans dist\ par rapport a la racine du projet)
set "EXE_DIR=%~dp0dist"
set "EXE=%EXE_DIR%\AffichagePrix.exe"

REM Verifier que l'exe existe
if not exist "%EXE%" (
    echo ERREUR : Fichier introuvable : %EXE%
    echo Compilez d'abord l'application avec build.bat.
    pause
    exit /b 1
)

REM Creer le raccourci via PowerShell (WScript.Shell)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([System.IO.Path]::Combine([System.Environment]::GetFolderPath('Startup'), 'AffichagePrix.lnk')); $s.TargetPath = '%EXE%'; $s.WorkingDirectory = '%EXE_DIR%'; $s.Description = 'Affichage Prix Netfact - Kiosque code-barres'; $s.Save()"

if errorlevel 1 (
    echo ERREUR : Creation du raccourci echouee.
    pause
    exit /b 1
)

echo.
echo Demarrage automatique configure avec succes !
echo Le kiosque se lancera automatiquement a la prochaine ouverture de session Windows.
echo.
echo Raccourci cree dans : %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
echo.

endlocal
pause
