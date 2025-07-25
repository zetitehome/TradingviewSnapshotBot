' run_macro.vbs
Option Explicit

Dim shell, macroPath, uiVisionPath, logPath, command, args
Set shell = CreateObject("WScript.Shell")

' === CONFIG ===
uiVisionPath = "C:\Users\chop\AppData\Local\Programs\UIVision\UIVision.exe"
macroPath = "auto_trade_macro"
logPath = "C:\Users\chop\Documents\macro_log.txt"

' === PARSE ARGUMENTS ===
If WScript.Arguments.Count > 0 Then
    args = WScript.Arguments.Item(0) ' Expect JSON string or query string passed from Node
Else
    args = "{}"
End If

' === BUILD COMMAND ===
command = """" & uiVisionPath & """ -macro=" & macroPath & " -param=" & Chr(34) & args & Chr(34)

' === RUN UI.VISION MACRO ===
Dim result
On Error Resume Next
result = shell.Run(command, 1, True)

Dim fso, file
Set fso = CreateObject("Scripting.FileSystemObject")
Set file = fso.OpenTextFile(logPath, 8, True)

file.WriteLine Now & " | Launched: " & command
If Err.Number = 0 Then
    file.WriteLine Now & " | ✅ Success"
Else
    file.WriteLine Now & " | ❌ Failed: " & Err.Description
End If

file.Close