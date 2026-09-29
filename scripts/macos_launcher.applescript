-- Generic source. The builder replaces the placeholder only in a temporary,
-- untracked copy before compiling the local application bundle.
property projectRoot : "__PROJECT_ROOT__"

on managerCommand(argumentsText)
	set pythonPath to projectRoot & "/.venv/bin/python"
	set managerPath to projectRoot & "/scripts/lead_finder_manager.py"
	return quoted form of pythonPath & space & quoted form of managerPath & space & argumentsText
end managerCommand

on showStartFailure()
	set logPath to projectRoot & "/data/runtime/backend.log"
	set choice to button returned of (display alert "Lead Opportunity Finder n’a pas pu démarrer." message "Vérifiez les prérequis et le journal local :" & return & logPath buttons {"Annuler", "Réessayer"} default button "Réessayer" cancel button "Annuler")
	return choice
end showStartFailure

on startAndOpen()
	repeat
		try
			do shell script my managerCommand("start")
			open location "http://127.0.0.1:8000"
			return
		on error
			if my showStartFailure() is not "Réessayer" then return
		end try
	end repeat
end startAndOpen

on run
	set pythonPath to projectRoot & "/.venv/bin/python"
	try
		-- A direct file access lets macOS request the narrow Files & Folders
		-- permission when the project lives in Desktop/Documents/Downloads.
		info for POSIX file pythonPath
	on error
		display alert "Lead Opportunity Finder ne peut pas accéder au projet." message "Si le projet est dans Bureau, Documents ou Téléchargements, autorisez uniquement l’accès à ce dossier lorsque macOS le demande. Sinon, vérifiez que .venv existe puis consultez docs/local-operations.md." as critical
		return
	end try
	set isRunning to false
	try
		do shell script my managerCommand("status --quiet")
		set isRunning to true
	end try
	if isRunning then
		set choice to button returned of (display dialog "Lead Opportunity Finder est déjà lancé." buttons {"Annuler", "Arrêter", "Ouvrir"} default button "Ouvrir" cancel button "Annuler")
		if choice is "Ouvrir" then
			open location "http://127.0.0.1:8000"
		else if choice is "Arrêter" then
			try
				do shell script my managerCommand("stop")
				display notification "Application arrêtée proprement." with title "Lead Opportunity Finder"
			on error
				display alert "Lead Opportunity Finder n’a pas pu être arrêté." message "Aucun processus non identifié n’a été interrompu. Consultez data/runtime/manager.log." as critical
			end try
		end if
	else
		my startAndOpen()
	end if
end run
