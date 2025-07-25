' run_macro.vbs
Set args = WScript.Arguments

Dim pair, action, expiry, amount, winrate
pair = args(0)
action = args(1)
expiry = args(2)
amount = args(3)
winrate = args(4)

macroParams = "-macro=TradeMacro" & " -param pair=" & pair & " -param action=" & action & " -param expiry=" & expiry & " -param amount=" & amount & " -param winrate=" & winrate

Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Program Files\UI.Vision RPA\uivision.exe"" " & macroParams, 1, false