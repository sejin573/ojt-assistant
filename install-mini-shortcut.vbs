Option Explicit

' 현재 저장소 위치를 기준으로 바탕화면 바로가기를 만든다.
' 바로가기는 이 스크립트가 아니라 launch-mini.vbs를 호출해 콘솔 없는 실행 경로를 유지한다.

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
