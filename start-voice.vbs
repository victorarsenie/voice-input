' Start the OpenCode push-to-talk listener without a console window.
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "E:\AI\voice"
shell.Run """E:\AI\voice\.venv\Scripts\pythonw.exe"" ""E:\AI\voice\voice.py""", 0, False
