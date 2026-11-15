"""Optional local Telegram connectivity test; no trading signals or orders."""
import os
from monitor import send_telegram

if __name__ == '__main__':
    send_telegram('✅ ICT + FVG V3 Telegram connection test. This is NOT a trading signal.',
                  os.environ.get('TELEGRAM_BOT_TOKEN', ''),
                  os.environ.get('TELEGRAM_CHAT_ID', ''))
    print('Test Telegram message accepted.')
