const express = require('express');
const bodyParser = require('body-parser');
const { exec } = require('child_process');
const app = express();
const port = 3333;

app.use(bodyParser.json());

app.post('/signal', (req, res) => {
  const { pair, action, expiry, amount, winrate } = req.body;

  if (!pair || !action || !expiry || !amount || !winrate) {
    return res.status(400).send('Missing parameters');
  }

  const cmd = `google-chrome "file:///home/YOUR_USER/UIVISION/macros/autotrade.html?pair=${pair}&action=${action}&expiry=${expiry}&amount=${amount}&winrate=${winrate}"`;
  
  exec(cmd, (error, stdout, stderr) => {
    if (error) {
      console.error(`❌ Error: ${error.message}`);
      return res.status(500).send('Failed to run macro');
    }
    console.log(`✅ Trade triggered: ${pair} ${action} $${amount} Exp: ${expiry}min`);
    res.send('Macro executed');
  });
});

app.listen(port, () => {
  console.log(`UI.Vision server running on port ${port}`);
});