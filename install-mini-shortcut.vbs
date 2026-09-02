Option Explicit

Dim shell, files, root, desktop, shortcut, pythonw, shortcutName
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")

root = files.GetParentFolderName(WScript.ScriptFullName)
desktop = shell.SpecialFolders("Desktop")
pythonw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python314\pythonw.exe"
shortcutName = "OJT " & ChrW(&HBBF8) & ChrW(&HB2C8) & " " & ChrW(&HB3C4) & ChrW(&HC6B0) & ChrW(&HBBF8)

Set shortcut = shell.CreateShortcut(desktop & "\" & shortcutName & ".lnk")
shortcut.TargetPath = shell.ExpandEnvironmentStrings("%WINDIR%") & "\System32\wscript.exe"
shortcut.Arguments = Chr(34) & root & "\launch-mini.vbs" & Chr(34)
shortcut.WorkingDirectory = root
shortcut.Description = "Compact OJT assistant"
If files.FileExists(pythonw) Then shortcut.IconLocation = pythonw & ",0"
shortcut.Save
