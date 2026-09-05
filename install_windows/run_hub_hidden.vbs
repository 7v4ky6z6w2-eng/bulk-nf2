' Lance start_hub.bat SANS AUCUNE fenetre visible (ni console, ni entree
' minimisee dans la barre des taches) -- utilise par la tache planifiee
' PrimeNFHubServer (voir install_hub_task.bat).
'
' WshShell.Run avec le style de fenetre 0 (SW_HIDE) masque completement la
' fenetre du cmd.exe lance, contrairement a "start /min" qui la minimise
' mais la laisse visible/cliquable dans la barre des taches a chaque
' ouverture de session.
'
' Reutilise start_hub.bat tel quel (detection automatique de python, chemin
' vers hub_server.py...) plutot que de redetecter python.exe/pythonw.exe ici
' : c'est le chemin deja confirme fonctionner manuellement, on ne fait que
' le rendre invisible.
Dim fso, shell, scriptDir, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")

' PRIMENF_HIDDEN=1 : dit a start_hub.bat de ne pas attendre une touche a la
' fin (le "pause" de secours en cas de plantage) -- personne n'est devant
' cette fenetre cachee pour l'appuyer.
cmd = "cmd /c set PRIMENF_HIDDEN=1&& """ & scriptDir & "\start_hub.bat"""
shell.Run cmd, 0, False
