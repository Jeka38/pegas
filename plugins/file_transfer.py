import os
import copy
import socket
import hashlib
import asyncio
import logging
import aiohttp
import base64
from slixmpp.xmlstream import ET, matcher, handler
from config import ADMIN_JID, ADMIN_NOTIFY_LEVEL, QUOTA_LIMIT_BYTES, SOCKS5_PORT, SOCKS5_IP
from utils import get_dir_size, safe_quote, get_unique_path
from .base import BasePlugin

class FileTransferPlugin(BasePlugin):
    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except: return '127.0.0.1'

    KNOWN_PROXIES = {
        'proxy.eu.jabber.network': {'host': 'proxy.eu.jabber.network', 'port': 1080},
        'proxy.jabber.ru': {'host': 'proxy.jabber.ru', 'port': 1080},
        'proxy.jabbim.cz': {'host': 'proxy.jabbim.cz', 'port': 1080},
        'proxy.yax.im': {'host': 'proxy.yax.im', 'port': 1080},
    }

    FT_NAMESPACES = {
        'urn:xmpp:jingle:1',
        'urn:xmpp:jingle:apps:file-transfer:1',
        'urn:xmpp:jingle:apps:file-transfer:2',
        'urn:xmpp:jingle:apps:file-transfer:3',
        'urn:xmpp:jingle:apps:file-transfer:4',
        'urn:xmpp:jingle:apps:file-transfer:5',
        'urn:xmpp:jingle:transports:s5b:1',
        'urn:xmpp:jingle:transports:ibb:1',
        'http://jabber.org/protocol/si',
        'http://jabber.org/protocol/si/profile/file-transfer',
        'http://jabber.org/protocol/bytestreams',
        'http://jabber.org/protocol/ibb',
        'jabber:iq:oob',
        'jabber:x:oob',
        'urn:xmpp:bob',
        'urn:xmpp:thumbs:1'
    }

    def __init__(self, bot):
        super().__init__(bot)
        self._tracked_ft_ids = set()
        self._ft_ns_prefixes = [f'{{{ns}}}' for ns in self.FT_NAMESPACES]
        self.bot.add_event_handler("xml_in", self.handle_xml_in)
        self.bot.add_event_handler("xml_out", self.handle_xml_out)

        # Регистрация обработчиков IQ
        self.bot.register_handler(
            handler.Callback('SI', matcher.MatchXPath('{jabber:client}iq/{http://jabber.org/protocol/si}si'), self.handle_raw_si)
        )
        self.bot.register_handler(
            handler.Callback('S5B', matcher.MatchXPath('{jabber:client}iq/{http://jabber.org/protocol/bytestreams}query'), self.handle_raw_s5b)
        )
        self.bot.register_handler(
            handler.Callback('Jingle', matcher.MatchXPath('{jabber:client}iq/{urn:xmpp:jingle:1}jingle'), self.handle_jingle)
        )
        self.bot.register_handler(
            handler.Callback('OOB', matcher.MatchXPath('{jabber:client}iq/{jabber:iq:oob}query'), self.handle_iq_oob)
        )

        self.bot.add_event_handler("ibb_stream_start", self.handle_ibb_stream)

        # Запускаем собственный SOCKS5 сервер
        asyncio.create_task(asyncio.start_server(self._handle_socks5_client, '0.0.0.0', SOCKS5_PORT))
        
        # Перехватчик входящего XML для IBB <message>
        self.bot.add_filter('in', self._intercept_ibb_messages)

        # Регистрация фич в Service Discovery (XEP-0030)
        for ns in self.FT_NAMESPACES:
            self.bot['xep_0030'].add_feature(ns)

    def _intercept_ibb_messages(self, stanza):
        try:
            if hasattr(stanza, 'xml') and stanza.xml.tag.endswith('message'):
                data_el = stanza.xml.find('{http://jabber.org/protocol/ibb}data')
                close_el = stanza.xml.find('{http://jabber.org/protocol/ibb}close')
                if data_el is not None:
                    sid = data_el.get('sid')
                    file_info = self.bot.pending_files.get(sid)
                    if file_info and 'stream' in file_info:
                        file_info['timestamp'] = asyncio.get_event_loop().time()
                        if data_el.text:
                            chunk = base64.b64decode(data_el.text)
                            file_info['stream'].recv_queue.put_nowait(chunk)
                        return None
                elif close_el is not None:
                    sid = close_el.get('sid')
                    file_info = self.bot.pending_files.get(sid)
                    if file_info and 'stream' in file_info:
                        file_info['timestamp'] = asyncio.get_event_loop().time()
                        file_info['stream'].recv_queue.put_nowait(None)
                        return None
        except Exception as e:
            logging.error(f"IBB filter error: {e}")
        return stanza

    def _should_log_xml(self, xml):
        has_ft_ns = False
        for prefix in self._ft_ns_prefixes:
            if prefix in xml.tag:
                has_ft_ns = True
                break
        if not has_ft_ns:
            for child in xml:
                for prefix in self._ft_ns_prefixes:
                    if prefix in child.tag:
                        has_ft_ns = True
                        break
                if has_ft_ns:
                    break

        stanza_id = xml.get('id')
        if stanza_id in self._tracked_ft_ids:
            return True

        return has_ft_ns

    def _to_log_str(self, xml):
        xml_copy = copy.deepcopy(xml)
        for data in xml_copy.findall('.//{http://jabber.org/protocol/ibb}data'):
            if data.text and len(data.text) > 100:
                data.text = data.text[:50] + f"...[TRUNCATED {len(data.text)} bytes]..." + data.text[-10:]
        for data in xml_copy.findall('.//{urn:xmpp:bob}data'):
            if data.text and len(data.text) > 100:
                data.text = data.text[:50] + f"...[TRUNCATED {len(data.text)} bytes]..." + data.text[-10:]
        if xml_copy.tag.endswith('}data') and ('http://jabber.org/protocol/ibb' in xml_copy.tag or 'urn:xmpp:bob' in xml_copy.tag):
            if xml_copy.text and len(xml_copy.text) > 100:
                xml_copy.text = xml_copy.text[:50] + f"...[TRUNCATED {len(xml_copy.text)} bytes]..." + xml_copy.text[-10:]
        return ET.tostring(xml_copy, encoding='unicode')

    def handle_xml_in(self, xml):
        if self._should_log_xml(xml):
            logging.info(f"RECV FT XML from {xml.get('from', 'unknown')}:\n{self._to_log_str(xml)}")
            if xml.tag.endswith('}iq'):
                stanza_id = xml.get('id')
                if stanza_id:
                    if xml.get('type') in ('get', 'set'):
                        self._tracked_ft_ids.add(stanza_id)
                    else:
                        self._tracked_ft_ids.discard(stanza_id)
            elif xml.tag.endswith('}message') and xml.get('type') == 'error':
                stanza_id = xml.get('id')
                if stanza_id: self._tracked_ft_ids.discard(stanza_id)

    def handle_xml_out(self, xml):
        if self._should_log_xml(xml):
            logging.info(f"SENT FT XML to {xml.get('to', 'unknown')}:\n{self._to_log_str(xml)}")
            if xml.tag.endswith('}iq'):
                stanza_id = xml.get('id')
                if stanza_id:
                    if xml.get('type') in ('get', 'set'):
                        self._tracked_ft_ids.add(stanza_id)
                    else:
                        self._tracked_ft_ids.discard(stanza_id)
            elif xml.tag.endswith('}message') and xml.get('type') == 'error':
                stanza_id = xml.get('id')
                if stanza_id: self._tracked_ft_ids.discard(stanza_id)

    async def _handle_socks5_client(self, reader, writer):
        try:
            ver_nmethods = await reader.readexactly(2)
            if ver_nmethods[0] != 0x05:
                writer.close(); return
            nmethods = ver_nmethods[1]
            methods = await reader.readexactly(nmethods)
            if 0x00 not in methods:
                writer.write(b"\x05\xFF")
                await writer.drain(); writer.close(); return
            writer.write(b"\x05\x00"); await writer.drain()

            req = await reader.readexactly(4)
            if req != b"\x05\x01\x00\x03":
                writer.close(); return
            addr_len = (await reader.readexactly(1))[0]
            dst_addr = (await reader.readexactly(addr_len)).decode()
            port = await reader.readexactly(2)

            match_found = False
            for sid, info in list(self.bot.pending_files.items()):
                if isinstance(info, dict):
                    t_sid = info.get('transport_sid', sid)
                    peer_full = info['peer_jid'].full
                    expected = hashlib.sha1(f"{t_sid}{peer_full}{self.bot.boundjid.full}".encode()).hexdigest()
                    if dst_addr == expected:
                        if info.get('downloading'):
                            writer.write(b"\x05\x01\x00\x03" + bytes([len(dst_addr)]) + dst_addr.encode() + port)
                            await writer.drain(); writer.close(); return

                        info['downloading'] = True
                        writer.write(b"\x05\x00\x00\x03" + bytes([len(dst_addr)]) + dst_addr.encode() + port)
                        await writer.drain()
                        logging.info(f"SOCKS5: Recognized incoming connection for sid={sid}, dst_addr={dst_addr}")
                        await self.download_file_task(reader, info, info['peer_jid'], sid)
                        match_found = True
                        break

            if not match_found:
                writer.write(b"\x05\x01\x00\x03" + bytes([len(dst_addr)]) + dst_addr.encode() + port)
                await writer.drain()
        except Exception as e:
            logging.error(f"SOCKS5 server error: {e}")
        finally:
            try: writer.close()
            except: pass

    async def request_bob_data(self, to_jid, cid_uri, fname):
        cid = cid_uri.replace('cid:', '')
        iq = self.bot.make_iq(itype='get', ito=to_jid)
        ET.SubElement(iq.xml, '{urn:xmpp:bob}data', cid=cid)
        try:
            resp = await iq.send()
            bob_data = resp.xml.find('{urn:xmpp:bob}data')
            if bob_data is not None and bob_data.text:
                data = base64.b64decode(bob_data.text)
                await self._save_thumb(to_jid, fname, data)
        except Exception as e:
            logging.error(f"BoB request error for {cid}: {e}")

    async def _save_thumb(self, peer_jid, fname, data):
        user_dir, _ = self.bot.get_user_info(peer_jid)
        thumb_dir = os.path.join(user_dir, '_sfpg_data', 'thumb')
        loop = asyncio.get_event_loop()

        def _do_save():
            os.makedirs(thumb_dir, exist_ok=True)
            safe_fname = os.path.basename(fname)
            thumb_path = os.path.join(thumb_dir, safe_fname + ".jpg")
            with open(thumb_path, 'wb') as f:
                f.write(data)
            return thumb_path

        try:
            thumb_path = await loop.run_in_executor(None, _do_save)
            logging.info(f"Thumbnail saved to {thumb_path}")
        except Exception as e:
            logging.error(f"Error saving thumbnail for {fname}: {e}")

    def handle_iq_oob(self, iq):
        if iq['type'] in ('error', 'result'): return
        query = iq.xml.find('{jabber:iq:oob}query')
        if query is None: return iq.reply().send()
        url_tag = query.find('{jabber:iq:oob}url')
        if url_tag is None or not url_tag.text: return iq.reply().send()
        url = url_tag.text
        desc = query.find('{jabber:iq:oob}desc')
        fname = desc.text if desc is not None and desc.text else os.path.basename(url)
        self.bot.pending_files[f"oob_{url}"] = asyncio.create_task(self.download_from_url(url, fname, iq['from']))
        iq.reply().send()

    async def download_from_url(self, url, fname, peer_jid):
        logging.info(f"Downloading OOB from {url}")
        from utils import is_php_file
        if is_php_file(fname):
            self.bot.send_message(mto=peer_jid, mbody=f"⚠️ Ошибка: Загрузка PHP-файлов запрещена ({fname})", mtype='chat')
            return
        user_dir, user_hash = self.bot.get_user_info(peer_jid)
        fname = os.path.basename(fname).replace(' ', '_')
        path = get_unique_path(os.path.join(user_dir, fname))
        part_path = path + ".part"
        loop = asyncio.get_event_loop()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=300) as resp:
                    if resp.status == 200:
                        fsize = int(resp.headers.get('Content-Length', 0))
                        if fsize > 0 and get_dir_size(user_dir) + fsize > QUOTA_LIMIT_BYTES:
                             self.bot.send_message(mto=peer_jid, mbody="⚠ Квота превышена!", mtype='chat')
                             return
                        received = 0
                        with open(part_path, 'wb') as f:
                            while True:
                                try:
                                    chunk = await asyncio.wait_for(resp.content.read(1048576), timeout=60)
                                    if not chunk:
                                        break
                                    await loop.run_in_executor(None, f.write, chunk)
                                    received += len(chunk)
                                except asyncio.TimeoutError:
                                    logging.error(f"OOB TIMEOUT: {url}, no data for 60s")
                                    self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Файл получен не полностью. Пожалуйста, попробуйте отправить снова.", mtype='chat')
                                    raise
                            await loop.run_in_executor(None, f.flush)
                            await loop.run_in_executor(None, os.fsync, f.fileno())

                        if fsize > 0 and received != fsize:
                             logging.error(f"OOB INCOMPLETE: {url}, received {received}/{fsize}")
                             self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Файл получен не полностью. Пожалуйста, попробуйте отправить снова.", mtype='chat')
                             if os.path.exists(part_path):
                                 os.remove(part_path)
                             return

                        os.rename(part_path, path)
                        real_fname = os.path.basename(path)
                        self.bot.send_message(mto=peer_jid, mbody=f"✅ Готово!\n{self.bot.base_url}/{user_hash}/{safe_quote(real_fname)}", mtype='chat')
                    else: logging.error(f"OOB download failed: HTTP {resp.status}")
        except Exception as e:
            logging.error(f"OOB download error: {e}")
            if os.path.exists(part_path): os.remove(part_path)

    def handle_jingle(self, iq):
        try:
            if iq['type'] in ('error', 'result'): return
            jingle = iq.xml.find('{urn:xmpp:jingle:1}jingle')
            if jingle is None:
                return iq.reply().send()
            action, sid = jingle.get('action'), jingle.get('sid')
            logging.info(f"JINGLE EVENT: action={action}, sid={sid}, from={iq['from']}")
            if action == 'session-initiate':
                if not self.bot.is_allowed(iq['from']):
                    logging.warning(f"JINGLE access denied for {iq['from']}")
                    self.bot.send_message(mto=iq['from'], mbody=f"⚠️ Доступ запрещён. Пожалуйста, обратитесь к администратору для получения доступа: {ADMIN_JID}", mtype='chat')
                    iq.error('not-allowed').send()
                    return
                content = jingle.find('{urn:xmpp:jingle:1}content')
                if content is None: return iq.reply().send()
                ft_ns = 'urn:xmpp:jingle:apps:file-transfer:5'
                description = content.find(f'{{{ft_ns}}}description')
                if description is None:
                    ft_ns = 'urn:xmpp:jingle:apps:file-transfer:4'; description = content.find(f'{{{ft_ns}}}description')
                if description is None: return iq.reply().send()
                file_tag = description.find(f'{{{ft_ns}}}file')
                if file_tag is None: return iq.reply().send()
                name_tag, size_tag = file_tag.find(f'{{{ft_ns}}}name'), file_tag.find(f'{{{ft_ns}}}size')
                if name_tag is None or size_tag is None: return iq.reply().send()

                fname = os.path.basename(name_tag.text or "file").replace(' ', '_')
                from utils import is_php_file
                if is_php_file(fname):
                    self.bot.send_message(mto=iq['from'], mbody=f"⚠️ Ошибка: Загрузка PHP-файлов запрещена ({fname})", mtype='chat')
                    iq.error('not-acceptable').send()
                    return
                try: fsize = int(size_tag.text or 0)
                except: fsize = 0
                user_dir, _ = self.bot.get_user_info(iq['from'])
                if get_dir_size(user_dir) + fsize > QUOTA_LIMIT_BYTES:
                    iq.error('not-acceptable').send()
                    return

                ibb_t, s5b_t = content.find('{urn:xmpp:jingle:transports:ibb:1}transport'), content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
                if s5b_t is not None and s5b_t.get('sid'): transport_sid = s5b_t.get('sid')
                elif ibb_t is not None and ibb_t.get('sid'): transport_sid = ibb_t.get('sid')
                else: transport_sid = sid

                self.bot.pending_files[sid] = {
                    'name': fname, 'size': fsize, 'timestamp': asyncio.get_event_loop().time(),
                    'peer_jid': iq['from'], 'ibb_allowed': True,
                    'content_name': content.get('name'), 'content_creator': content.get('creator'),
                    'ft_ns': ft_ns, 'transport_sid': transport_sid, 's5b_connecting': False,
                    'ibb_stanzas': ibb_t.get('stanzas') if ibb_t is not None else None,
                    'session_sid': sid, 'downloading': False
                }
                if transport_sid != sid: self.bot.pending_files[transport_sid] = self.bot.pending_files[sid]
                iq.reply().send()

                thumb_tag = file_tag.find('{urn:xmpp:thumbs:1}thumbnail')
                if thumb_tag is not None:
                    uri = thumb_tag.get('uri')
                    if uri and uri.startswith('cid:'):
                        asyncio.create_task(self.request_bob_data(iq['from'], uri, fname))

                try:
                    accept_iq = self.bot.make_iq_set(ito=iq['from'])
                    res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-accept', 'sid': sid, 'initiator': iq['from'].full})
                    res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {'creator': content.get('creator'), 'name': content.get('name')})
                    res_d = ET.SubElement(res_c, f'{{{ft_ns}}}description')
                    res_f = ET.SubElement(res_d, f'{{{ft_ns}}}file')
                    ET.SubElement(res_f, f'{{{ft_ns}}}name').text = fname
                    ET.SubElement(res_f, f'{{{ft_ns}}}size').text = str(fsize)

                    if s5b_t is not None:
                        res_t = ET.SubElement(res_c, '{urn:xmpp:jingle:transports:s5b:1}transport', {'sid': transport_sid, 'mode': 'tcp'})
                        local_ip = self.get_local_ip()
                        ET.SubElement(res_t, '{urn:xmpp:jingle:transports:s5b:1}candidate', host=local_ip, port=str(SOCKS5_PORT), jid=self.bot.boundjid.full, cid='direct-host-local', priority='8253074', type='host')
                        if SOCKS5_IP and SOCKS5_IP != local_ip:
                            ET.SubElement(res_t, '{urn:xmpp:jingle:transports:s5b:1}candidate', host=SOCKS5_IP, port=str(SOCKS5_PORT), jid=self.bot.boundjid.full, cid='direct-host-public', priority='8252818', type='host')
                        for p_host, p_jid in [('proxy.eu.jabber.network', 'proxy.eu.jabber.network'), ('proxy.jabber.ru', 'proxy.jabber.ru')]:
                            ET.SubElement(res_t, '{urn:xmpp:jingle:transports:s5b:1}candidate', host=p_host, port='1080', jid=p_jid, cid=hashlib.md5(p_jid.encode()).hexdigest(), priority='65536', type='proxy')
                    elif ibb_t is not None:
                        b_size = int(ibb_t.get('block-size', '8192'))
                        use_msg = ibb_t.get('stanzas') == 'message'
                        ibb_attrs = {'block-size': str(b_size), 'sid': transport_sid}
                        if use_msg: ibb_attrs['stanzas'] = 'message'
                        ET.SubElement(res_c, '{urn:xmpp:jingle:transports:ibb:1}transport', ibb_attrs)
                        from slixmpp.plugins.xep_0047 import IBBytestream
                        stream = IBBytestream(self.bot, transport_sid, b_size, self.bot.boundjid, iq['from'], use_msg)
                        asyncio.create_task(self.bot['xep_0047'].api['set_stream'](self.bot.boundjid, transport_sid, iq['from'], stream))
                        self.bot.event('ibb_stream_start', stream)
                    else:
                        ET.SubElement(res_c, '{urn:xmpp:jingle:transports:ibb:1}transport', {'block-size': '8192', 'sid': sid})
                        from slixmpp.plugins.xep_0047 import IBBytestream
                        stream = IBBytestream(self.bot, sid, 8192, self.bot.boundjid, iq['from'], False)
                        asyncio.create_task(self.bot['xep_0047'].api['set_stream'](self.bot.boundjid, sid, iq['from'], stream))
                        self.bot.event('ibb_stream_start', stream)

                    accept_iq.append(res_j)
                    accept_iq.send()
                    if s5b_t is not None and s5b_t.findall('{urn:xmpp:jingle:transports:s5b:1}candidate'):
                        self.bot.pending_files[sid]['s5b_connecting'] = True
                        self.bot.pending_files[f"jingle_s5b_{sid}"] = asyncio.create_task(self._socks5_connect_and_save(iq, jingle_sid=sid))
                except Exception as e: logging.error(f"JINGLE ACCEPT ERROR: {e}")
            elif action == 'transport-info':
                content = jingle.find('{urn:xmpp:jingle:1}content')
                if content is not None:
                    transport = content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
                    if transport is not None and not self.bot.pending_files.get(sid, {}).get('s5b_connecting'):
                        self.bot.pending_files[sid]['s5b_connecting'] = True
                        self.bot.pending_files[f"jingle_s5b_info_{sid}"] = asyncio.create_task(self._socks5_connect_and_save(iq, jingle_sid=sid))
                iq.reply().send()
            elif action == 'transport-replace':
                content = jingle.find('{urn:xmpp:jingle:1}content')
                if content is not None:
                    ibb_t = content.find('{urn:xmpp:jingle:transports:ibb:1}transport')
                    if ibb_t is not None:
                        if sid in self.bot.pending_files:
                            ibb_sid = ibb_t.get('sid')
                            self.bot.pending_files[sid]['transport_sid'] = ibb_sid
                            self.bot.pending_files[sid]['ibb_stanzas'] = ibb_t.get('stanzas')
                            self.bot.pending_files[ibb_sid] = self.bot.pending_files[sid]
                            reply = self.bot.make_iq_set(ito=iq['from'])
                            res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-accept', 'sid': sid, 'initiator': iq['from'].full})
                            res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {'creator': content.get('creator'), 'name': content.get('name')})
                            use_msg = ibb_t.get('stanzas') == 'message'
                            ibb_attrs = {'sid': ibb_sid}
                            if use_msg: ibb_attrs['stanzas'] = 'message'
                            ET.SubElement(res_c, '{urn:xmpp:jingle:transports:ibb:1}transport', ibb_attrs)
                            from slixmpp.plugins.xep_0047 import IBBytestream
                            stream = IBBytestream(self.bot, ibb_sid, 8192, self.bot.boundjid, iq['from'], use_msg)
                            asyncio.create_task(self.bot['xep_0047'].api['set_stream'](self.bot.boundjid, ibb_sid, iq['from'], stream))
                            self.bot.event('ibb_stream_start', stream)
                            reply.append(res_j); reply.send()
                iq.reply().send()
            elif action == 'transport-accept':
                iq.reply().send()
            elif action == 'session-terminate':
                task_key = f"task_{sid}"
                if task_key in self.bot.pending_files:
                    task = self.bot.pending_files[task_key]
                    if isinstance(task, asyncio.Task) and not task.done(): task.cancel()
                if sid in self.bot.pending_files: del self.bot.pending_files[sid]
                iq.reply().send()
            else:
                iq.reply().send()
        except Exception as e:
            logging.error(f"JINGLE IQ ERROR: {e}")
            try: iq.error('internal-server-error').send()
            except: pass

    def handle_raw_si(self, iq):
        if iq['type'] in ('error', 'result'): return
        if not self.bot.is_allowed(iq['from']):
            logging.warning(f"SI access denied for {iq['from']}")
            self.bot.send_message(mto=iq['from'], mbody=f"⚠️ Доступ запрещён. Пожалуйста, обратитесь к администратору для получения доступа: {ADMIN_JID}", mtype='chat')
            iq.error('not-allowed').send()
            return
        try:
            si = iq.xml.find('{http://jabber.org/protocol/si}si')
            sid, tag = si.get('id'), si.find('{http://jabber.org/protocol/si/profile/file-transfer}file')
            logging.info(f"SI REQUEST: sid={sid}, from={iq['from']}, file={tag.get('name')}, size={tag.get('size')}")
            fname, fsize = os.path.basename(tag.get('name') or "file").replace(' ', '_'), int(tag.get('size', 0))
            from utils import is_php_file
            if is_php_file(fname):
                self.bot.send_message(mto=iq['from'], mbody=f"⚠️ Ошибка: Загрузка PHP-файлов запрещена ({fname})", mtype='chat')
                iq.error('not-acceptable').send()
                return
            user_dir, _ = self.bot.get_user_info(iq['from'])
            if get_dir_size(user_dir) + fsize > QUOTA_LIMIT_BYTES:
                iq.error('not-acceptable').send()
                return
            feature_neg = si.find('{http://jabber.org/protocol/feature-neg}feature')
            offered_methods = []
            if feature_neg is not None:
                x_data = feature_neg.find('{jabber:x:data}x')
                if x_data is not None:
                    field = next((f for f in x_data.findall('{jabber:x:data}field') if f.get('var') == 'stream-method'), None)
                    if field is not None:
                        offered_methods = [v.text for v in field.findall('{jabber:x:data}value')]
                        offered_methods.extend([v.text for v in field.findall('{jabber:x:data}option/{jabber:x:data}value')])
            chosen_method = next((m for m in ['jabber:iq:oob', 'http://jabber.org/protocol/bytestreams', 'http://jabber.org/protocol/ibb'] if m in offered_methods), None)
            if not chosen_method:
                iq.error('bad-request').send()
                return
            self.bot.pending_files[sid] = {
                'name': fname, 'size': fsize, 'timestamp': asyncio.get_event_loop().time(),
                'ibb_allowed': 'http://jabber.org/protocol/ibb' in offered_methods,
                'peer_jid': iq['from'], 'transport_sid': sid, 'downloading': False
            }
            reply = iq.reply()
            res_si = ET.Element('{http://jabber.org/protocol/si}si', {'id': sid})
            feature = ET.SubElement(res_si, '{http://jabber.org/protocol/feature-neg}feature')
            x = ET.SubElement(feature, '{jabber:x:data}x', type='submit')
            field = ET.SubElement(x, '{jabber:x:data}field', var='stream-method')
            ET.SubElement(field, '{jabber:x:data}value').text = chosen_method
            reply.append(res_si)
            reply.send()
        except Exception as e:
            logging.error(f"SI ERROR: {e}")
            try: iq.error('internal-server-error').send()
            except: pass

    def handle_raw_s5b(self, iq):
        if iq['type'] in ('error', 'result'): return
        query = iq.xml.find('{http://jabber.org/protocol/bytestreams}query')
        if query is not None and query.find('{http://jabber.org/protocol/bytestreams}streamhost-used') is not None:
             asyncio.create_task(self._socks5_connect_and_save(iq))
        else:
             self.bot.pending_files[f"s5b_{iq['id']}"] = asyncio.create_task(self._socks5_connect_and_save(iq))

    async def _socks5_connect_and_save(self, iq, jingle_sid=None):
        sid = None
        try:
            if jingle_sid:
                sid = jingle_sid; jingle = iq.xml.find('{urn:xmpp:jingle:1}jingle')
                if jingle is None: return
                content = jingle.find('{urn:xmpp:jingle:1}content')
                if content is None: return
                query = content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
                if query is None: return
                hosts = query.findall('{urn:xmpp:jingle:transports:s5b:1}candidate')
                peer_full = iq['from'].full
            else:
                query = iq.xml.find('{http://jabber.org/protocol/bytestreams}query')
                if query is None: return
                sid, peer_full = query.get('sid'), iq['from'].full
                used = query.find('{http://jabber.org/protocol/bytestreams}streamhost-used')
                if used is not None:
                    jid = used.get('jid'); proxy = self.KNOWN_PROXIES.get(jid)
                    if proxy: hosts = [ET.Element('streamhost', host=proxy['host'], port=str(proxy['port']), jid=jid)]
                    else: iq.error('item-not-found').send(); return
                else:
                    hosts = query.findall('{http://jabber.org/protocol/bytestreams}streamhost')
                if not hosts and used is None:
                    reply = iq.reply()
                    res_q = ET.Element('{http://jabber.org/protocol/bytestreams}query', {'sid': sid})
                    local_ip = self.get_local_ip()
                    ET.SubElement(res_q, '{http://jabber.org/protocol/bytestreams}streamhost', host=local_ip, port=str(SOCKS5_PORT), jid=self.bot.boundjid.full)
                    if SOCKS5_IP and SOCKS5_IP != local_ip:
                         ET.SubElement(res_q, '{http://jabber.org/protocol/bytestreams}streamhost', host=SOCKS5_IP, port=str(SOCKS5_PORT), jid=self.bot.boundjid.full)
                    for p_jid, p_info in self.KNOWN_PROXIES.items():
                        ET.SubElement(res_q, '{http://jabber.org/protocol/bytestreams}streamhost', host=p_info['host'], port=str(p_info['port']), jid=p_jid)
                    reply.append(res_q); reply.send(); return

            file_info = self.bot.pending_files.get(sid)
            if not file_info: return
            t_sid = file_info.get('transport_sid', sid)
            dst_addr = hashlib.sha1(f"{t_sid}{peer_full}{self.bot.boundjid.full}".encode()).hexdigest()

            if jingle_sid and not hosts:
                jingle = iq.xml.find('{urn:xmpp:jingle:1}jingle')
                if jingle is not None and jingle.get('action') == 'session-initiate':
                    self.bot.pending_files[sid]['s5b_connecting'] = False
                    return

            for host in hosts:
                try:
                    logging.info(f"S5B: Connecting to {host.get('host')}:{host.get('port', 1080)} for sid={sid}")
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(host.get('host'), int(host.get('port', 1080))), 5)
                    writer.write(b"\x05\x01\x00"); await writer.drain()
                    if await reader.read(2) != b"\x05\x00": writer.close(); continue
                    writer.write(b"\x05\x01\x00\x03" + bytes([len(dst_addr)]) + dst_addr.encode() + b"\x00\x00"); await writer.drain()
                    resp = await reader.read(4)
                    if not resp or resp[1] != 0x00: writer.close(); continue
                    atyp = resp[3]
                    if atyp == 0x01: await reader.read(6)
                    elif atyp == 0x03: addr_len = await reader.read(1); await reader.read(addr_len[0] + 2)
                    elif atyp == 0x04: await reader.read(18)
                    if jingle_sid:
                        reply = self.bot.make_iq_set(ito=iq['from'])
                        res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-info', 'sid': jingle_sid, 'initiator': iq['from'].full})
                        res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {'creator': file_info.get('content_creator', 'initiator'), 'name': file_info.get('content_name', 'file')})
                        res_t = ET.SubElement(res_c, '{urn:xmpp:jingle:transports:s5b:1}transport', {'sid': sid})
                        ET.SubElement(res_t, '{urn:xmpp:jingle:transports:s5b:1}candidate-used', cid=host.get('cid'))
                        reply.append(res_j); reply.send()
                    else:
                        reply = iq.reply()
                        if used is None:
                            res_q = ET.Element('{http://jabber.org/protocol/bytestreams}query', {'sid': sid})
                            ET.SubElement(res_q, '{http://jabber.org/protocol/bytestreams}streamhost-used', jid=host.get('jid'))
                            reply.append(res_q)
                        reply.send()
                    logging.info(f"S5B: SUCCESS connect to {host.get('host')}:{host.get('port')} for sid={sid}")
                    file_info['downloading'] = True
                    await self.download_file_task(reader, file_info, iq['from'], sid)
                    writer.close(); await writer.wait_closed(); return
                except Exception as e:
                    logging.info(f"S5B: Failed connect to {host.get('host')} for sid={sid}: {e}")
                    continue
            if not jingle_sid: iq.error('service-unavailable').send()
            elif file_info.get('ibb_allowed'):
                logging.info(f"SOCKS5 failed for Jingle sid={sid}, falling back to IBB")
                new_ibb_sid = f"fallback_{sid}"
                self.bot.pending_files[sid]['transport_sid'] = new_ibb_sid
                self.bot.pending_files[new_ibb_sid] = self.bot.pending_files[sid]
                reply = self.bot.make_iq_set(ito=iq['from'])
                res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-replace', 'sid': sid, 'initiator': iq['from'].full})
                res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {'creator': file_info.get('content_creator', 'initiator'), 'name': file_info.get('content_name', 'file')})
                use_msg = self.bot.pending_files.get(sid, {}).get('ibb_stanzas') == 'message'
                ibb_attrs = {'sid': new_ibb_sid, 'block-size': '8192'}
                if use_msg: ibb_attrs['stanzas'] = 'message'
                ET.SubElement(res_c, '{urn:xmpp:jingle:transports:ibb:1}transport', ibb_attrs)
                from slixmpp.plugins.xep_0047 import IBBytestream
                stream = IBBytestream(self.bot, new_ibb_sid, 8192, self.bot.boundjid, iq['from'], use_msg)
                asyncio.create_task(self.bot['xep_0047'].api['set_stream'](self.bot.boundjid, new_ibb_sid, iq['from'], stream))
                self.bot.event('ibb_stream_start', stream)
                reply.append(res_j); reply.send()
            else:
                if sid in self.bot.pending_files: del self.bot.pending_files[sid]
        except Exception as e: logging.error(f"SOCKS5 ERROR: {e}")

    def handle_ibb_stream(self, stream):
        sid = stream.sid
        file_info = self.bot.pending_files.get(sid)
        if file_info:
            if file_info['peer_jid'].bare != stream.peer_jid.bare:
                stream.close(); return
            logging.info(f"IBB stream started for sid={sid}, peer={stream.peer_jid}")
            self.bot.pending_files[sid]['stream'] = stream
            task = asyncio.create_task(self.download_file_task(stream, file_info, stream.peer_jid, sid))
            self.bot.pending_files[f"task_{sid}"] = task
        else: stream.close()

    async def download_file_task(self, reader, file_info, peer_jid, sid):
        logging.info(f"DOWNLOAD START: sid={sid}, peer={peer_jid}, file={file_info['name']}, size={file_info['size']}")
        user_dir, user_hash = self.bot.get_user_info(peer_jid)
        path = get_unique_path(os.path.join(user_dir, os.path.basename(file_info['name'])))
        part_path = path + ".part"
        received, loop = 0, asyncio.get_event_loop()
        try:
            with open(part_path, 'wb') as f:
                while received < file_info['size']:
                    try:
                        if hasattr(reader, 'recv_queue'): chunk = await asyncio.wait_for(reader.recv_queue.get(), timeout=60)
                        else: chunk = await asyncio.wait_for(reader.read(min(file_info['size'] - received, 1048576)), timeout=60)
                        if not chunk: break
                        await loop.run_in_executor(None, f.write, chunk)
                        received += len(chunk)
                        if sid in self.bot.pending_files: self.bot.pending_files[sid]['timestamp'] = loop.time()
                    except asyncio.TimeoutError:
                        logging.error(f"DOWNLOAD TIMEOUT: sid={sid}, no data for 60s")
                        self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Файл получен не полностью. Пожалуйста, попробуйте отправить снова.", mtype='chat')
                        raise
                await loop.run_in_executor(None, f.flush); await loop.run_in_executor(None, os.fsync, f.fileno())

            if received == file_info['size']:
                os.rename(part_path, path)
                logging.info(f"DOWNLOAD COMPLETE: sid={sid}, path={path}")
                self.bot.send_message(mto=peer_jid, mbody=f"✅ Готово!\n{self.bot.base_url}/{user_hash}/{safe_quote(os.path.basename(path))}", mtype='chat')
                session_sid, ft_ns = file_info.get('session_sid'), file_info.get('ft_ns')
                if session_sid and ft_ns:
                    logging.info(f"JINGLE COMPLETE: Sending session-info (received) and session-terminate (success) for sid={session_sid}")
                    info_iq = self.bot.make_iq_set(ito=peer_jid)
                    res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-info', 'sid': session_sid, 'initiator': peer_jid.full})
                    ET.SubElement(res_j, f'{{{ft_ns}}}received')
                    info_iq.append(res_j); info_iq.send()
                    term_iq = self.bot.make_iq_set(ito=peer_jid)
                    res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-terminate', 'sid': session_sid, 'initiator': peer_jid.full})
                    reason = ET.SubElement(res_j, '{urn:xmpp:jingle:1}reason')
                    ET.SubElement(reason, '{urn:xmpp:jingle:1}success')
                    term_iq.append(res_j); term_iq.send()
            else:
                logging.error(f"DOWNLOAD INCOMPLETE: sid={sid}, received {received}/{file_info['size']}")
                self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Файл получен не полностью. Пожалуйста, попробуйте отправить снова.", mtype='chat')
                if os.path.exists(part_path): os.remove(part_path)
        except (asyncio.CancelledError, Exception) as e:
            logging.error(f"DOWNLOAD {'CANCELLED' if isinstance(e, asyncio.CancelledError) else 'ERROR'}: sid={sid}, error={e}")
            if os.path.exists(part_path): os.remove(part_path)
        finally:
            if hasattr(reader, 'close'):
                try:
                    if asyncio.iscoroutinefunction(reader.close): await reader.close()
                    else: reader.close()
                except: pass
            info = self.bot.pending_files.get(sid)
            if info:
                t_sid = info.get('transport_sid')
                if t_sid and t_sid in self.bot.pending_files: del self.bot.pending_files[t_sid]
            if sid in self.bot.pending_files: del self.bot.pending_files[sid]
