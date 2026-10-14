Option Explicit
Dim oShell, scriptDir, result
Set oShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
oShell.CurrentDirectory = scriptDir
result = oShell.Run(Chr(34) & scriptDir & "\start_silent_inner.bat" & Chr(34), 0, True)
If result <> 0 Then
    MsgBox "System Monitor could not start. Please run start_console.bat for details, or check logs\launcher.log and logs\app.log.", 48, "System Monitor"
End If
