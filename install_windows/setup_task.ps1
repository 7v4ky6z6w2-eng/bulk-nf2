# PrimeNF Agent - enregistrement de la tache planifiee Windows (PowerShell)
# Alternative a install_agent.bat, plus facile a personnaliser.
#
# Lancer en tant qu'Administrateur :
#   powershell -ExecutionPolicy Bypass -File setup_task.ps1 -StoreId 2
#
# Parametres :
#   -StoreId  : identifiant du magasin (obligatoire)
#   -Interval : intervalle en minutes (defaut : 15)
#   -ExePath  : chemin complet vers PrimeNFAgent.exe (defaut : C:\PrimeNFAgent\PrimeNFAgent.exe)
#
# NOTE ENCODAGE : ce fichier ne doit contenir AUCUN caractere accentue. Sur les
# PC dont la page de code active n'est pas UTF-8 (courant sur Windows en
# configuration arabe/algerienne), PowerShell 5.1 lit les fichiers .ps1 sans
# BOM avec la page de code ANSI du systeme : un accent encode en UTF-8 se
# retrouve alors mal decode et peut casser une chaine de caracteres, faisant
# planter TOUT le script avec une erreur de parsing (deja vu en pratique).

param(
    [Parameter(Mandatory=$true)][int]$StoreId,
    [int]$Interval = 15,
    [string]$ExePath = "C:\PrimeNFAgent\PrimeNFAgent.exe"
)

$TaskName = "PrimeNFAgent"
$WorkDir  = Split-Path $ExePath

# Supprimer l'ancienne tache
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action
$Action = New-ScheduledTaskAction `
    -Execute $ExePath `
    -Argument "--store-id $StoreId --once" `
    -WorkingDirectory $WorkDir

# Declencheur : toutes les N minutes, depart 1 min apres demarrage systeme
$Trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes $Interval) `
    -Once -At (Get-Date).AddMinutes(1)

# Parametres
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# Enregistrement
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -RunLevel Highest `
    -Force | Out-Null

Write-Host "OK - Tache planifiee '$TaskName' creee (magasin $StoreId, toutes les $Interval min)."
Write-Host "  Executable : $ExePath"
Write-Host "  Demarrage rattrape si le poste etait eteint : active (-StartWhenAvailable)"
