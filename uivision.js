const express = require('express');
const app = express();
app.use(express.json());

app.post('/analyze', (req, res) => {
  console.log('📊 Triggering analyze macro...');
  // Trigger analyze macro via command or UI.Vision CLI
  res.sendStatus(200);
});

app.post('/auto', (req, res) => {
  console.log('🟢 Auto-trading triggered...');
  // Trigger auto-trading macro or mode
  res.sendStatus(200);
});

app.listen(3333, () => {
  console.log('🚀 UI.Vision Trigger Server running on port 3333');
});
