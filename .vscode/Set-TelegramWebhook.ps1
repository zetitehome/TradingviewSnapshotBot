# ==========================
# Set Telegram Bot Webhook
# ==========================

# --- CONFIG ---
$BotToken = "8009536179:AAGb8atyBIotWcITtzx4cDuchc_xXXH-9cA"  # Your Telegram bot token
$WebhookURL = "https://tradingviewsnapshotbot.onrender.com/webhook"  # Your webhook endpoint

# --- SET WEBHOOK ---
$uri = "https://api.telegram.org/bot$BotToken/setWebhook"

Write-Host "Setting Telegram webhook to $WebhookURL..."
try {
    $response = Invoke-WebRequest -Uri $uri -Method Post -Body @{url=$WebhookURL}
    Write-Host "Response: $($response.Content)"
} catch {
    Write-Host "❌ Error setting webhook: $($_.Exception.Message)"
}
