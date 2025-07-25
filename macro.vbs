' run_macro.vbs
' Usage: cscript run_macro.vbs "EURUSD" "BUY" "5" "10" "75"

Dim shell, fso, logFile, pair, action, expiry, amount, winrate, timestamp, cmd, returnCode

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Signal Data
pair = WScript.Arguments(0)
action = WScript.Arguments(1)
expiry = WScript.Arguments(2)
amount = WScript.Arguments(3)
winrate = WScript.Arguments(4)
timestamp = Now

' Command to run the macro
cmd = "cmd /c start """" ""C:\Program Files\UI.Vision RPA\UI.Vision RPA.exe"" -macro=TradeFromSignal -cmdline 1 -savelog -storage=hard -param pair=" & pair & " -param action=" & action & " -param expiry=" & expiry & " -param amount=" & amount & " -param winrate=" & winrate

' Run UI.Vision macro
returnCode = shell.Run(cmd, 1, True)

' Logging
Set logFile = fso.OpenTextFile("trade_log.txt", 8, True)
If returnCode = 0 Then
  logFile.WriteLine timestamp & " | SUCCESS | " & pair & " " & action & " | Expiry: " & expiry & " | $" & amount & " | Winrate: " & winrate & "%"
Else
  logFile.WriteLine timestamp & " | FAILED  | " & pair & " " & action & " | Expiry: " & expiry & " | $" & amount & " | Winrate: " & winrate & "%"
End If
logFile.Close