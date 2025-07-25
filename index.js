const express = require("express");
const bodyParser = require("body-parser");
const { exec } = require("child_process");

const app = express();
app.use(bodyParser.json());

app.post("/trade", (req, res) => {
  const { pair, amount, direction, expiry } = req.body;

  if (!pair || !amount || !direction) {
    return res.status(400).json({ success: false, message: "Missing trade parameters." });
  }

  const macroPath = "C:\\path\\to\\ui.vision\\macro\\pocket-trade.json"; // Update this
  const command = `"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" "file:///C:/path/to/UI.Vision.html?macro=${macroPath}&direct=1&closeBrowser=1&savelog=log.txt&storage=hard&cmd_var1=${pair}&cmd_var2=${amount}&cmd_var3=${direction}&cmd_var4=${expiry}"`;

  exec(command, (err, stdout, stderr) => {
    if (err) {
      console.error("Macro exec error:", stderr);
      return res.status(500).json({ success: false, message: "Macro execution failed." });
    }

    console.log("Macro triggered:", stdout);
    return res.json({ success: true, message: "Trade macro triggered." });
  });
});

const PORT = 3333;
app.listen(PORT, () => console.log(`UI.Vision Trigger Server running on port ${PORT}`));
