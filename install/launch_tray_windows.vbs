' Start the ASC Remote Hub tray without a visible PowerShell window.
Option Explicit
Dim shell, fs, ps1, command
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
ps1 = fs.BuildPath(fs.GetParentFolderName(WScript.ScriptFullName), "tray_windows.ps1")
command = "powershell.exe -NoProfile -NonInteractive -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & ps1 & Chr(34)
shell.Run command, 0, True
