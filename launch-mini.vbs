Option Explicit

' 콘솔 창을 만들지 않고 mini_app.py를 실행하는 사용자 진입점이다.
' 특정 Python 3.14 경로를 우선 사용하고, 없으면 PATH의 pythonw.exe로 폴백한다.

Dim shell, files, root, pythonw, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")

root = files.GetParentFolderName(WScript.ScriptFullName)
pythonw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python314\pythonw.exe"
If Not files.FileExists(pythonw) Then pythonw = "pythonw.exe"

command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & root & "\mini_app.py" & Chr(34)
shell.Run command, 0, False
