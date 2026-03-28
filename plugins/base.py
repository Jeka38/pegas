import asyncio
import logging

class BasePlugin:
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    def reply(self, msg, text):
        self.bot.send_message(mto=msg['from'], mbody=text, mtype='chat')

    def send_iq(self, iq):
        """Send an IQ and log any error, preventing 'Future exception never retrieved'."""
        async def _send():
            try:
                if iq['type'] in ('get', 'set'):
                    await iq.send()
                else:
                    iq.send()
            except Exception as e:
                logging.debug(f"IQ error ({iq['id']}): {e}")
        asyncio.create_task(_send())
