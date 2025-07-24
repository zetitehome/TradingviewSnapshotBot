const fetch = require('node-fetch');

async function triggerUIVisionMacro(macroName, variables = {}) {
  const uivisionWebhookURL = 'http://localhost:18000/uivision-webhook'; // <-- Replace with your actual UI.Vision webhook URL

  const body = {
    macro: macroName,
    variables
  };

  try {
    const response = await fetch(uivisionWebhookURL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      throw new Error(`UI.Vision webhook failed: ${response.statusText}`);
    }
    return await response.json();
  } catch (err) {
    console.error('Error triggering UI.Vision macro:', err);
    throw err;
  }
}

module.exports = {
  triggerUIVisionMacro
};
