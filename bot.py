import os
import hashlib
import logging
import asyncio

from slixmpp import ClientXMPP
from slixmpp.xmlstream import ET

from config import (
    ADMIN_JID, VERSION, APP_NAME, BASE_URL
)
from database import Database
from plugins.presence import PresencePlugin
from plugins.commands import CommandsPlugin
from plugins.file_transfer import FileTransferPlugin

class OBBFastBot(ClientXMPP):
    def __init__(self, jid, password, dest_dir):
        super().__init__(jid, password)
        self.dest_dir = dest_dir
        self.base_url = BASE_URL
        self.pending_files = {}
        asyncio.create_task(self.cleanup_pending_files())

        self.db = Database()
        self.migrate_json_to_db()
        self.migrate_filenames()
        self.cleanup_leftover_parts()

        # Initialize core features via plugins
        self.register_plugin('xep_0030')
        self.register_plugin('xep_0047')
        self['xep_0047'].auto_accept = True
        self['xep_0047'].block_size = 32768
        self['xep_0047'].max_block_size = 65536
        self.register_plugin('xep_0199')
        self['xep_0199'].send_keepalive = True
        self['xep_0199'].interval = 60
        self.register_plugin('xep_0092')
        self['xep_0092'].software_name = APP_NAME

        import slixmpp
        import platform
        self['xep_0092'].version = f"{VERSION} on Python {platform.python_version()} + slixmpp {slixmpp.__version__}"

        # Setup custom Ping handler
        from slixmpp.xmlstream import matcher, handler
        self.register_handler(
            handler.Callback('Ping', matcher.MatchXPath('{jabber:client}iq/{urn:xmpp:ping}ping'), self.handle_ping)
        )

        # Load logical modules (plugins)
        self.presence = PresencePlugin(self)
        self.file_transfer = FileTransferPlugin(self)
        self.commands = CommandsPlugin(self)


    def handle_ping(self, iq):
        if iq['type'] in ('error', 'result'): return
        logging.info(f"PING RECV from {iq['from']}")
        reply = iq.reply()
        reply.append(ET.Element('{urn:xmpp:ping}ping'))
        reply.send()
        logging.info(f"PONG SENT to {iq['from']}")

    def make_iq_set(self, ito=None, ifrom=None):
        return self.make_iq(itype='set', ito=ito, ifrom=ifrom)

    async def cleanup_pending_files(self):
        while True:
            try:
                await asyncio.sleep(60)
                now = asyncio.get_event_loop().time()
                to_delete = []
                for sid, info in list(self.pending_files.items()):
                    if isinstance(info, dict):
                        # Timeout for inactive transfers (1 minute)
                        if now - info.get('timestamp', now) > 60:
                            to_delete.append(sid)
                            # Cancel associated task if exists
                            task_key = f"task_{sid}"
                            if task_key in self.pending_files:
                                task = self.pending_files[task_key]
                                if isinstance(task, asyncio.Task) and not task.done():
                                    logging.info(f"CLEANUP: Cancelling inactive task {task_key}")
                                    task.cancel()
                    elif isinstance(info, asyncio.Task):
                        if info.done():
                            to_delete.append(sid)

                for sid in to_delete:
                    if sid in self.pending_files:
                        logging.info(f"CLEANUP: Removing pending item sid={sid}")
                        del self.pending_files[sid]
            except Exception as e:
                logging.error(f"CLEANUP ERROR: {e}")

    def cleanup_leftover_parts(self):
        logging.info("START: Cleanup leftover .part files")
        count = 0
        for root, dirs, files in os.walk(self.dest_dir):
            for f in files:
                if f.endswith('.part'):
                    path = os.path.join(root, f)
                    try:
                        os.remove(path)
                        count += 1
                    except Exception as e:
                        logging.error(f"CLEANUP PART ERROR for {path}: {e}")
        if count > 0:
            logging.info(f"FINISH: Removed {count} .part files during startup")

    def migrate_json_to_db(self):
        whitelist_file = os.getenv('WHITELIST_FILE', 'whitelist.json')
        try:
            if os.path.exists(whitelist_file):
                if os.path.isfile(whitelist_file):
                    import json
                    with open(whitelist_file, 'r') as f:
                        data = json.load(f)
                        for entry in data:
                            self.db.add_to_whitelist(entry)
                    logging.info(f"MIGRATED {len(data)} entries from {whitelist_file} to database")
                    os.remove(whitelist_file)
                elif os.path.isdir(whitelist_file):
                    os.rmdir(whitelist_file)
        except Exception as e:
            logging.error(f"MIGRATION ERROR: {e}")

    def is_allowed(self, jid):
        bare_jid = jid.bare.lower()
        if ADMIN_JID and bare_jid == ADMIN_JID.lower(): return True
        blacklist = self.db.get_blacklist()
        if bare_jid in blacklist or jid.domain.lower() in blacklist: return False
        whitelist = self.db.get_whitelist()
        if '*' in whitelist: return True
        return bare_jid in whitelist or jid.domain.lower() in whitelist

    def migrate_filenames(self):
        logging.info("START: Filename migration (spaces to underscores)")
        count = 0
        for root, dirs, files in os.walk(self.dest_dir):
            for f in files:
                if ' ' in f:
                    old_path = os.path.join(root, f)
                    new_path = os.path.join(root, f.replace(' ', '_'))
                    try:
                        os.rename(old_path, new_path)
                        count += 1
                    except Exception as e:
                        logging.error(f"MIGRATE ERROR for {old_path}: {e}")
        if count > 0:
            logging.info(f"FINISH: Renamed {count} files during migration")

    def get_user_info(self, jid):
        bare_jid = jid.bare.lower()
        user_hash = self.db.get_user_folder(bare_jid)
        is_new = False
        if not user_hash:
            user_hash = hashlib.md5(bare_jid.encode()).hexdigest()
            self.db.set_user_folder(bare_jid, user_hash)
            is_new = True
        user_dir = os.path.join(self.dest_dir, user_hash)
        if not os.path.exists(user_dir): os.makedirs(user_dir); is_new = True
        if is_new:
            from config import ADMIN_NOTIFY_LEVEL
            if ADMIN_JID and ADMIN_NOTIFY_LEVEL in ('all', 'registrations'):
                self.send_message(mto=ADMIN_JID, mbody=f"🆕 Новый пользователь: {bare_jid} ({user_hash})", mtype='chat')
        return user_dir, user_hash

    def get_help_text(self, is_admin=False, user_hash=None):
        text = (
            "команды:\n"
            "ls [фильтр] - список файлов и каталогов в папке пользователя.\n"
            "ls <-s|-l> [фильтр], lss [фильтр], lsl [фильтр] - список файлов (-s: размер, -l: подробно). Примеры: ls -l, lss 1, ls *.jpg\n"
            "mkdir <путь> - создать директорию.\n"
            "rmdir <номер|путь> - удалить пустую директорию.\n"
            "mv <номер|путь> <номер|путь> - переместить/переименовать.\n"
            "rm <номер>[,<номер>],.. - удаление файлов по номеру или rm * - для удаления всех файлов.\n"
            "link <номер>[,<номер>],.. - получение ссылок на файлы или lnk * - для всех файлов.\n"
            "priv - сделать архив приватным (создать index.html).\n"
            "pub - сделать архив публичным (удалить index.html).\n"
            "album - включить режим галереи (создать index.php).\n"
            "ping - проверить доступность бота.\n"
            "help или ? - список команд."
        )
        if user_hash:
            text += f"\n\n📂 Ваш архив: {self.base_url}/{user_hash}/"
            text += "\nЧтобы запретить просмотр списка файлов через браузер, используйте команду priv."
        if is_admin:
            text += (
                "\n\n🔧 Админ-команды:\n"
                "add <jid|domain|*> - разрешить доступ.\n"
                "del <jid|domain|*> - запретить доступ.\n"
                "block <jid|domain> - в чёрный список.\n"
                "unblock <jid|domain> - убрать из чёрного списка.\n"
                "list - показать белый и чёрный списки."
            )
        return text
