' Start the voice listener hidden (double-click shortcut target).
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "E:\AI\voice"
shell.Run """E:\AI\voice\.venv\Scripts\pythonw.exe"" ""E:\AI\voice\voice.py""", 0, False
