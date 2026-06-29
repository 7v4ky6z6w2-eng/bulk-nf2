# PrimeNF Agent — enregistrement de la tâche planifiée Windows (PowerShell)
# Alternative à install_agent.bat, plus facile à personnaliser.
#
# Lancer en tant qu'Administrateur :
#   powershell -ExecutionPolicy Bypass -File setup_task.ps1 -StoreId 2
#
# Paramètres :
#   -StoreId  : identifiant du magasin (obligatoire)
#   -Interval : intervalle en minutes (défaut : 15)
#   -ExePath  : chemin complet vers PrimeNFAgent.exe (défaut : C:\PrimeNFAgent\PrimeNFAgent.exe)

param(
    [Parameter(Mandatory=$true)][int]$StoreId,
    [int]$Interval = 15,
    [string]$ExePath = "C:\PrimeNFAgent\PrimeNFAgent.exe"
)

$TaskName = "PrimeNFAgent"
$WorkDir  = Split-Path $ExePath

# Supprimer l'ancienne tâche
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action
$Action = New-ScheduledTaskAction `
    -Execute $ExePath `
    -Argument "--store-id $StoreId --once" `
    -WorkingDirectory $WorkDir

# Déclencheur : toutes les N minutes, départ 1 min après démarrage système
$Trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes $Interval) `
    -Once -At (Get-Date).AddMinutes(1)

# Paramètres
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

Write-Host "✔ Tâche planifiée '$TaskName' créée (magasin $StoreId, toutes les $Interval min)."
Write-Host "  Exécutable : $ExePath"
Write-Host "  Démarrage avec disponibilité réseau : activé (-StartWhenAvailable)"
