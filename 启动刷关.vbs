Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
py = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python310\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw.exe"
sh.CurrentDirectory = base
sh.Run """" & py & """ """" & base & "nte_gui.py""", 0, False
