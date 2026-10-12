import logging
import os
from typing import Optional, Dict, Any
import httpx

logger = logging.getLogger(__name__)


class TelegramAlertService:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send_alert(self, title: str, message: str, level: str = "INFO", details: Optional[Dict[str, Any]] = None) -> bool:
        emoji_map = {
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "ERROR": "🚨",
            "CRITICAL": "💥",
        }
        emoji = emoji_map.get(level.upper(), "📢")
        
        text = f"<b>{emoji} [{level.upper()}] {title}</b>\n\n{message}"
        if details:
            text += "\n\n<b>Details:</b>"
            for k, v in details.items():
                text += f"\n• <code>{k}</code>: {v}"

        if not self.is_configured:
            logger.info(f"[TelegramAlert mock dispatch] {level}: {title} - {message}")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info(f"Telegram alert sent successfully: {title}")
                    return True
                else:
                    logger.error(f"Failed to send Telegram alert: status {resp.status_code}, response: {resp.text}")
                    return False
        except Exception as exc:
            logger.error(f"Error sending Telegram alert: {exc}")
            return False
