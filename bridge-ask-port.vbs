' Ask for a COM port, then launch the RC2014 bridge on it.
' Double-click / taskbar friendly: no console window (pythonw), and the
' port last used is prefilled from rc2014bridge.ini next to this script.

Option Explicit

Dim fso, sh, dir, ini, line, eqPos
Dim port, answer

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

port = "COM10"
ini = dir & "\rc2014bridge.ini"
If fso.FileExists(ini) Then
    Dim f
    Set f = fso.OpenTextFile(ini, 1)
    Do While Not f.AtEndOfStream
        line = Trim(f.ReadLine)
        If LCase(Left(line, 4)) = "port" Then
            eqPos = InStr(line, "=")
            If eqPos > 0 Then port = Trim(Mid(line, eqPos + 1))
        End If
    Loop
    f.Close
End If

answer = InputBox("Which COM port is the machine on?" & vbCrLf & _
                  "(e.g. COM4, COM10)", "RC2014 Bridge", port)
If answer = "" Then WScript.Quit   ' Cancel pressed

answer = UCase(Trim(answer))
If Left(answer, 3) <> "COM" Or Not IsNumeric(Mid(answer, 4)) Then
    MsgBox """" & answer & """ doesn't look like a COM port (expected COM<number>).", _
           vbExclamation, "RC2014 Bridge"
    WScript.Quit
End If

sh.CurrentDirectory = dir
sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" -m rc2014bridge.app --port " & _
       answer & " --mcp-host 127.0.0.1 --log-file """ & dir & "\bridge.log""", 1, False
