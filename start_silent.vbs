Set oShell = CreateObject("WScript.Shell")
oShell.CurrentDirectory = "E:\hermes_space\system-monitor-prototype"
oShell.Run "start_silent_inner.bat", 0, False
