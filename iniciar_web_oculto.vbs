Set shell = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
command = """" & folder & "\.venv\Scripts\pythonw.exe"" """ & folder & "\app.py"""
shell.Run command, 0, False
