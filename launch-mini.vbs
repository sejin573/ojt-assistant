Option Explicit

Dim shell, files, root, pythonw, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")

root = files.GetParentFolderName(WScript.ScriptFullName)
pythonw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python314\pythonw.exe"
If Not files.FileExists(pythonw) Then pythonw = "pythonw.exe"

command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & root & "\mini_app.py" & Chr(34)
shell.Run command, 0, False
