' run_macro.vbs
Dim args, pair, action, expiry, amount, winrate
Set args = WScript.Arguments

pair = args(0)
action = args(1)
expiry = args(2)
amount = args(3)
winrate = args(4)

Set shell = CreateObject("WScript.Shell")
macroPath = "C:\Users\gwappobot94\Documents\UI.Vision\macros\pocket_trade_macro.json"

cmd = "ui.vision.html -macro=" & macroPath & " -cmdvar1=" & pair & " -cmdvar2=" & action & " -cmdvar3=" & expiry & " -cmdvar4=" & amount & " -cmdvar5=" & winrate

shell.Run cmd, 1, false
