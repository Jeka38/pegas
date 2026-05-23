import os
import shutil
import datetime
import re
import asyncio
import logging
from config import ADMIN_JID, QUOTA_LIMIT_BYTES, MAX_DIR_DEPTH
from utils import (
    get_dir_size, format_size, get_safe_path, get_all_items,
    resolve_items_list, resolve_item, get_unique_path, safe_quote
)
from .base import BasePlugin

class CommandsPlugin(BasePlugin):
    def __init__(self, bot):
        super().__init__(bot)
        self.bot.add_event_handler("message", self.handle_message)
        self._handlers = {
            'help': self.cmd_help,
            '?': self.cmd_help,
            'ping': self.cmd_ping,
            'mkdir': self.cmd_mkdir,
            'rmdir': self.cmd_rmdir,
            'mv': self.cmd_mv,
            'ls': self.cmd_ls,
            'lss': self.cmd_ls,
            'lsl': self.cmd_ls,
            'link': self.cmd_link,
            'lnk': self.cmd_link,
            'rm': self.cmd_rm,
            'priv': self.cmd_priv,
            'pub': self.cmd_pub,
            'album': self.cmd_album,
            # Admin commands
            'add': self.cmd_admin_add,
            'del': self.cmd_admin_del,
            'block': self.cmd_admin_block,
            'unblock': self.cmd_admin_unblock,
            'list': self.cmd_admin_list
        }

    def handle_message(self, msg):
        if msg['type'] not in ('chat', 'normal'):
            return

        if not self.bot.is_allowed(msg['from']):
            # Только если в сообщении что-то есть (текст или OOB), уведомляем об отказе
            if msg['body'] or msg.xml.find('{jabber:x:oob}x') is not None:
                self.reply(msg, f"⚠️ Доступ запрещён. Пожалуйста, обратитесь к администратору для получения доступа: {ADMIN_JID}")
            return

        user_dir, user_hash = self.bot.get_user_info(msg['from'])
        cmd_executed = False
        oob_urls = set()

        # Handle XEP-0066 Out-of-Band Data in messages
        oob = msg.xml.find('{jabber:x:oob}x')
        if oob is not None:
            url_el = oob.find('{jabber:x:oob}url')
            if url_el is not None and url_el.text:
                url = url_el.text.strip()
                cmd_executed = True
                oob_urls.add(url)
                desc = oob.find('{jabber:x:oob}desc')
                fname = desc.text if desc is not None and desc.text else os.path.basename(url)
                asyncio.create_task(self.bot.file_transfer.download_from_url(url, fname, msg['from']))

        if not msg['body']:
            return

        # Detect URLs in message body, avoiding duplicates from OOB and filtering trailing punctuation
        url_regex = r'https?://(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s<>"]*)?'
        raw_urls = re.findall(url_regex, msg['body'])
        clean_urls = []
        for u in raw_urls:
            # Remove trailing punctuation (.,!?;:)
            u = u.rstrip('.,!?;:')
            if u not in oob_urls and u not in clean_urls:
                clean_urls.append(u)

        if clean_urls:
            cmd_executed = True
            for url in clean_urls[:5]: # Limit 5 links at once
                asyncio.create_task(self.bot.file_transfer.download_from_url(url, os.path.basename(url), msg['from']))

        parts = msg['body'].strip().split()
        if not parts: return
        cmd = parts[0].lower()

        handler = self._handlers.get(cmd)
        if handler:
            is_admin_cmd = cmd in ('add', 'del', 'block', 'unblock', 'list')
            is_admin = ADMIN_JID and msg['from'].bare.lower() == ADMIN_JID.lower()

            if is_admin_cmd and not is_admin:
                pass # Fallback to default
            else:
                try:
                    if handler(msg, parts, user_dir, user_hash):
                        cmd_executed = True
                except Exception as e:
                    logging.error(f"Error executing command {cmd}: {e}")
                    self.reply(msg, f"❌ Ошибка при выполнении команды: {e}")
                    cmd_executed = True

        if not cmd_executed:
            is_admin = ADMIN_JID and msg['from'].bare.lower() == ADMIN_JID.lower()
            used = get_dir_size(user_dir)
            self.reply(msg, self.bot.get_help_text(is_admin, user_hash) + f"\n\n📊 Квота: {format_size(used)} / {format_size(QUOTA_LIMIT_BYTES)}")

    def cmd_help(self, msg, parts, user_dir, user_hash):
        if len(parts) != 1: return False
        is_admin = ADMIN_JID and msg['from'].bare.lower() == ADMIN_JID.lower()
        used = get_dir_size(user_dir)
        self.reply(msg, self.bot.get_help_text(is_admin, user_hash) + f"\n\n📊 Квота: {format_size(used)} / {format_size(QUOTA_LIMIT_BYTES)}")
        return True

    def cmd_ping(self, msg, parts, user_dir, user_hash):
        if len(parts) != 1: return False
        self.reply(msg, "pong")
        return True

    def cmd_mkdir(self, msg, parts, user_dir, user_hash):
        if len(parts) < 2: return False
        created = []
        errors = []
        for name in parts[1:]:
            folder_name = name.replace(' ', '_')
            target = get_safe_path(user_dir, folder_name)
            if target:
                rel = os.path.relpath(target, user_dir)
                if rel != "." and rel.count(os.sep) >= MAX_DIR_DEPTH:
                    errors.append(f"❌ {name}: Превышена глубина вложенности")
                else:
                    try:
                        os.makedirs(target, exist_ok=True)
                        created.append(rel)
                    except Exception as e: errors.append(f"❌ {name}: {e}")
            else: errors.append(f"❌ {name}: Недопустимый путь")

        if created:
            self.reply(msg, f"📁 Создано директорий: {len(created)}\n" + "\n".join([f"• {c}" for c in created]))
        if errors:
            self.reply(msg, "\n".join(errors))
        return True

    def cmd_rmdir(self, msg, parts, user_dir, user_hash):
        if len(parts) < 2: return False
        items = get_all_items(user_dir)
        resolved_paths = resolve_items_list(user_dir, parts[1:], items)
        removed_count = 0
        for target in resolved_paths:
            if target and os.path.isdir(target):
                try: os.rmdir(target); removed_count += 1
                except Exception: pass
        if removed_count: self.reply(msg, f"🗑 Удалено директорий: {removed_count}")
        else: self.reply(msg, "❌ Директории не найдены или не пусты")
        return True

    def cmd_mv(self, msg, parts, user_dir, user_hash):
        if len(parts) < 3: return False
        items = get_all_items(user_dir)
        # Sanitize destination name if it's a new path
        dst_arg = parts[-1].replace(' ', '_')
        dst = resolve_item(user_dir, dst_arg, items)
        if not dst:
            self.reply(msg, "❌ Недопустимый путь назначения")
            return True

        resolved_srcs = resolve_items_list(user_dir, parts[1:-1], items)
        if not resolved_srcs:
            self.reply(msg, "❌ Объекты для перемещения не найдены")
            return True

        if len(resolved_srcs) > 1:
            if not os.path.isdir(dst):
                self.reply(msg, "❌ При перемещении нескольких объектов назначение должно быть директорией")
            else:
                moved_count = 0
                for src in resolved_srcs:
                    if os.path.abspath(src) == os.path.abspath(dst):
                        continue
                    new_dst = os.path.join(dst, os.path.basename(src.rstrip('/')))
                    from utils import is_php_file
                    if is_php_file(new_dst):
                        self.reply(msg, f"⚠️ Ошибка: Переименование в PHP-файлы запрещено ({os.path.basename(new_dst)})")
                        continue
                    rel_dst = os.path.relpath(new_dst, user_dir)
                    is_dir = os.path.isdir(src)
                    limit = MAX_DIR_DEPTH if not is_dir else MAX_DIR_DEPTH - 1
                    if rel_dst != "." and rel_dst.count(os.sep) > limit: continue
                    try:
                        new_dst = get_unique_path(new_dst)
                        os.rename(src, new_dst); moved_count += 1
                    except Exception: pass
                self.reply(msg, f"🚚 Перемещено объектов: {moved_count}")
        else:
            src = resolved_srcs[0]
            if src and os.path.exists(src):
                try:
                    final_dst = dst
                    if os.path.isdir(dst):
                        final_dst = os.path.join(dst, os.path.basename(src.rstrip('/')))
                    else:
                        if os.path.isfile(src):
                            _, original_ext = os.path.splitext(src)
                            if not final_dst.lower().endswith(original_ext.lower()):
                                final_dst += original_ext

                    rel_dst = os.path.relpath(final_dst, user_dir)
                    is_dir = os.path.isdir(src)
                    limit = MAX_DIR_DEPTH if not is_dir else MAX_DIR_DEPTH - 1
                    if rel_dst != "." and rel_dst.count(os.sep) > limit:
                        self.reply(msg, "❌ Ошибка: Превышена максимальная глубина вложенности")
                    else:
                        from utils import is_php_file
                        if is_php_file(final_dst):
                            self.reply(msg, f"⚠️ Ошибка: Переименование в PHP-файлы запрещено ({os.path.basename(final_dst)})")
                        else:
                            final_dst = get_unique_path(final_dst)
                            os.rename(src, final_dst)
                            self.reply(msg, f"🚚 Перемещено: {os.path.relpath(src, user_dir)} -> {os.path.relpath(final_dst, user_dir)}")
                except Exception as e: self.reply(msg, f"❌ Ошибка: {e}")
            else: self.reply(msg, "❌ Файл не найден")
        return True

    def cmd_ls(self, msg, parts, user_dir, user_hash):
        cmd = parts[0].lower()
        mode = 'links'
        filter_arg = None
        if cmd == 'lss':
            mode = 'size'
            if len(parts) > 1: filter_arg = parts[1:]
        elif cmd == 'lsl':
            mode = 'long'
            if len(parts) > 1: filter_arg = parts[1:]
        else: # cmd == 'ls'
            if len(parts) > 1:
                if parts[1] == '-s':
                    mode = 'size'
                    if len(parts) > 2: filter_arg = parts[2:]
                elif parts[1] == '-l':
                    mode = 'long'
                    if len(parts) > 2: filter_arg = parts[2:]
                else:
                    filter_arg = parts[1:]

        items = get_all_items(user_dir)
        used = get_dir_size(user_dir)
        footer = f"\n\n📊 Квота: {format_size(used)} / {format_size(QUOTA_LIMIT_BYTES)}"
        footer += f"\n📂 Ваш архив: {self.bot.base_url}/{user_hash}/"

        if not items:
            self.reply(msg, "📁 Папка пуста" + footer)
            return True

        resolved_filter = None
        if filter_arg:
            resolved_filter = set(resolve_items_list(user_dir, filter_arg, items))
            if not resolved_filter:
                self.reply(msg, "🔍 Ничего не найдено" + footer)
                return True

        res = ["Список файлов:"]
        for i, itm in enumerate(items):
            full_path = os.path.join(user_dir, itm)
            if resolved_filter is not None and full_path not in resolved_filter:
                continue

            depth = itm.count('/')
            if itm.endswith('/'): depth -= 1
            name = os.path.basename(itm.rstrip('/'))
            if itm.endswith('/'): name += "/"
            display_itm = ("    " * depth + "└── " + name) if depth > 0 else name

            if mode == 'links': res.append(f"{i+1} - {display_itm}")
            elif mode == 'size':
                if itm.endswith('/'): res.append(f"{i+1} - {display_itm} [директория]")
                else: res.append(f"{i+1} - {display_itm} [{format_size(os.path.getsize(full_path))}]")
            elif mode == 'long':
                st = os.stat(full_path)
                size, mtime = format_size(st.st_size), datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M')
                if itm.endswith('/'):
                    res.append(f"{i+1} - {display_itm} [директория, {mtime}]")
                else:
                    res.append(f"{i+1} - {display_itm} [{size}, загружен {mtime}]")

        if len(res) == 1:
            self.reply(msg, "🔍 Ничего не найдено" + footer)
        else:
            self.reply(msg, "\n".join(res) + footer)
        return True

    def cmd_link(self, msg, parts, user_dir, user_hash):
        if len(parts) < 2: return False
        items = get_all_items(user_dir)
        if not items:
            self.reply(msg, "📁 Папка пуста")
            return True
        if parts[1] == '*':
            res = [f"{i+1} - {self.bot.base_url}/{user_hash}/{safe_quote(itm)}" for i, itm in enumerate(items) if not itm.endswith('/')]
            self.reply(msg, "\n".join(res))
        else:
            resolved_paths = resolve_items_list(user_dir, parts[1:], items)
            res = []
            for path in resolved_paths:
                if not os.path.isdir(path):
                    rel = os.path.relpath(path, user_dir)
                    try: idx = items.index(rel)
                    except ValueError: idx = -1
                    res.append(f"{idx+1 if idx >=0 else '?'} - {self.bot.base_url}/{user_hash}/{safe_quote(rel)}")
            if res: self.reply(msg, "\n".join(res))
        return True

    def cmd_rm(self, msg, parts, user_dir, user_hash):
        if len(parts) < 2: return False
        items = get_all_items(user_dir)
        if not items:
            self.reply(msg, "📁 Папка пуста")
            return True
        if parts[1] == '*':
            if len(parts) == 3 and parts[2].lower() == 'confirm':
                for item in os.listdir(user_dir):
                    item_path = os.path.join(user_dir, item)
                    try:
                        if os.path.isdir(item_path): shutil.rmtree(item_path)
                        else: os.remove(item_path)
                    except Exception: pass
                self.reply(msg, "🗑 Все файлы и папки удалены.")
            else: self.reply(msg, "⚠ Чтобы удалить ВСЕ файлы, напишите: rm * confirm")
        else:
            resolved_paths = resolve_items_list(user_dir, parts[1:], items)
            removed_count = 0
            for path in resolved_paths:
                try:
                    if os.path.isdir(path): shutil.rmtree(path)
                    else: os.remove(path)
                    removed_count += 1
                except Exception: pass
            if removed_count: self.reply(msg, f"🗑 Удалено объектов: {removed_count}")
        return True

    def cmd_priv(self, msg, parts, user_dir, user_hash):
        index_path = os.path.join(user_dir, 'index.html')
        php_path = os.path.join(user_dir, 'index.php')
        if os.path.exists(php_path):
            try: os.remove(php_path)
            except: pass
        if not os.path.exists(index_path):
            with open(index_path, 'w') as f: f.write("<html><body><h1>Private Archive</h1></body></html>")
            self.reply(msg, "🔒 Архив теперь приватный (создан index.html, index.php удалён)")
        else: self.reply(msg, "ℹ Архив уже приватный.")
        return True

    def cmd_pub(self, msg, parts, user_dir, user_hash):
        index_path = os.path.join(user_dir, 'index.html')
        php_path = os.path.join(user_dir, 'index.php')
        removed = []
        for p in (index_path, php_path):
            if os.path.exists(p):
                try:
                    os.remove(p)
                    removed.append(os.path.basename(p))
                except: pass
        if removed: self.reply(msg, f"🔓 Архив теперь публичный (удалено: {', '.join(removed)})")
        else: self.reply(msg, "ℹ Архив уже публичный.")
        return True

    def cmd_album(self, msg, parts, user_dir, user_hash):
        template_path = 'index.php'
        target_path = os.path.join(user_dir, 'index.php')
        index_html = os.path.join(user_dir, 'index.html')
        if os.path.exists(template_path):
            try:
                shutil.copy(template_path, target_path)
                if os.path.exists(index_html): os.remove(index_html)
                self.reply(msg, "🖼 Режим альбома включён (index.php скопирован, index.html удалён)")
            except Exception as e: self.reply(msg, f"❌ Ошибка при создании альбома: {e}")
        else: self.reply(msg, "❌ Ошибка: Шаблон index.php не найден в корне бота")
        return True

    def cmd_admin_add(self, msg, parts, user_dir, user_hash):
        if len(parts) < 2: return False
        entries = [e.strip().lower() for e in parts[1:] if e.strip()]
        # If any entry contains a comma, we should still handle it for backward compatibility if needed,
        # but the prompt asked for batch by names/numbers.
        # Actually, split(',') was there before for "admin add jid1,jid2".
        # Let's keep supporting it but also support space separation.
        final_entries = []
        for e in entries:
            final_entries.extend([i.strip() for i in e.split(',') if i.strip()])
        added = []
        for entry in final_entries:
            if entry == '*' or '@' in entry or '.' in entry:
                self.db.add_to_whitelist(entry); added.append(entry)
        if added:
            if '*' in added: self.reply(msg, "🌟 Доступ разрешён для ВСЕХ пользователей.")
            else: self.reply(msg, f"➕ Добавлено в белый список: {', '.join(added)}")
        else: self.reply(msg, "⚠ Неверный формат. Используйте user@domain, domain или *")
        return True

    def cmd_admin_del(self, msg, parts, user_dir, user_hash):
        if len(parts) < 2: return False
        raw_entries = [e.strip().lower() for e in parts[1:] if e.strip()]
        entries = []
        for e in raw_entries:
            entries.extend([i.strip() for i in e.split(',') if i.strip()])
        whitelist = self.db.get_whitelist()
        removed = [e for e in entries if e in whitelist]
        for e in removed: self.db.remove_from_whitelist(e)
        if removed: self.reply(msg, f"➖ Удалено из белого списка: {', '.join(removed)}")
        else: self.reply(msg, "❓ Ничего не найдено для удаления из белого списка.")
        return True

    def cmd_admin_block(self, msg, parts, user_dir, user_hash):
        if len(parts) < 2: return False
        raw_entries = [e.strip().lower() for e in parts[1:] if e.strip()]
        entries = []
        for e in raw_entries:
            entries.extend([i.strip() for i in e.split(',') if i.strip()])
        added = [e for e in entries if '@' in e or '.' in e]
        for e in added: self.db.add_to_blacklist(e)
        if added: self.reply(msg, f"🚫 Добавлено в чёрный список: {', '.join(added)}")
        else: self.reply(msg, "⚠ Неверный формат. Используйте user@domain или domain")
        return True

    def cmd_admin_unblock(self, msg, parts, user_dir, user_hash):
        if len(parts) < 2: return False
        raw_entries = [e.strip().lower() for e in parts[1:] if e.strip()]
        entries = []
        for e in raw_entries:
            entries.extend([i.strip() for i in e.split(',') if i.strip()])
        blacklist = self.db.get_blacklist()
        removed = [e for e in entries if e in blacklist]
        for e in removed: self.db.remove_from_blacklist(e)
        if removed: self.reply(msg, f"✅ Удалено из чёрного списка: {', '.join(removed)}")
        else: self.reply(msg, "❓ Ничего не найдено для удаления из чёрного списка.")
        return True

    def cmd_admin_list(self, msg, parts, user_dir, user_hash):
        res_w, res_b = "\n".join(sorted(self.db.get_whitelist())), "\n".join(sorted(self.db.get_blacklist()))
        self.reply(msg, f"📄 Белый список:\n{res_w or '(пусто)'}\n\n🚫 Чёрный список:\n{res_b or '(пусто)'}")
        return True
