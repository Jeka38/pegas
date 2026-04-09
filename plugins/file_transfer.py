import os
import copy
import socket
import hashlib
import asyncio
import logging
import aiohttp
import base64
import ipaddress
import urllib.parse
from slixmpp.xmlstream import ET, matcher, handler
from config import ADMIN_JID, QUOTA_LIMIT_BYTES
from utils import get_dir_size, safe_quote, get_unique_path
from .base import BasePlugin

class FileTransferPlugin(BasePlugin):
    FT_NAMESPACES = [
        'http://jabber.org/protocol/si',
        'http://jabber.org/protocol/si/profile/file-transfer',
        'http://jabber.org/protocol/bytestreams',
        'http://jabber.org/protocol/ibb',
        'urn:xmpp:jingle:1',
        'urn:xmpp:jingle:apps:file-transfer:1',
        'urn:xmpp:jingle:apps:file-transfer:2',
        'urn:xmpp:jingle:apps:file-transfer:3',
        'urn:xmpp:jingle:apps:file-transfer:4',
        'urn:xmpp:jingle:apps:file-transfer:5',
        'urn:xmpp:jingle:transports:s5b:1',
        'urn:xmpp:jingle:transports:ibb:1',
        'jabber:iq:oob',
        'jabber:x:oob',
        'urn:xmpp:bob',
        'urn:xmpp:thumbs:1'
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self._tracked_ft_ids = set()
        self._ft_ns_prefixes = [f'{{{ns}}}' for ns in self.FT_NAMESPACES]
        self.bot.add_event_handler("xml_in", self.handle_xml_in)
        self.bot.add_event_handler("xml_out", self.handle_xml_out)

        # IQ Handlers
        self.bot.register_handler(handler.Callback('SI', matcher.MatchXPath('{jabber:client}iq/{http://jabber.org/protocol/si}si'), self.handle_raw_si))
        self.bot.register_handler(handler.Callback('S5B', matcher.MatchXPath('{jabber:client}iq/{http://jabber.org/protocol/bytestreams}query'), self.handle_raw_s5b))
        self.bot.register_handler(handler.Callback('Jingle', matcher.MatchXPath('{jabber:client}iq/{urn:xmpp:jingle:1}jingle'), self.handle_jingle))
        self.bot.register_handler(handler.Callback('OOB', matcher.MatchXPath('{jabber:client}iq/{jabber:iq:oob}query'), self.handle_iq_oob))

        self.bot.add_event_handler("ibb_stream_start", self.handle_ibb_stream)

        # SOCKS5 Server
        asyncio.create_task(asyncio.start_server(self._handle_socks5_client, '0.0.0.0', 1080))
        
        # IBB Filter
        self.bot.add_filter('in', self._intercept_ibb_messages)

        # Service Discovery
        for ns in self.FT_NAMESPACES:
            self.bot['xep_0030'].add_feature(ns)

    # --- XML Logging Helpers ---

    def handle_xml_in(self, xml):
        if self._should_log_xml(xml):
            logging.info(f"RECV FT XML from {xml.get('from', 'unknown')}:\n{self._to_log_str(xml)}")
            self._track_id(xml)

    def handle_xml_out(self, xml):
        if self._should_log_xml(xml):
            logging.info(f"SENT FT XML to {xml.get('to', 'unknown')}:\n{self._to_log_str(xml)}")
            self._track_id(xml)

    def _should_log_xml(self, xml):
        has_ft_ns = any(prefix in xml.tag for prefix in self._ft_ns_prefixes)
        if not has_ft_ns:
            has_ft_ns = any(any(prefix in child.tag for prefix in self._ft_ns_prefixes) for child in xml)

        stanza_id = xml.get('id')
        return has_ft_ns or (stanza_id in self._tracked_ft_ids)

    def _track_id(self, xml):
        if not xml.tag.endswith('}iq'): return
        stanza_id = xml.get('id')
        if not stanza_id: return
        if xml.get('type') in ('get', 'set'): self._tracked_ft_ids.add(stanza_id)
        else: self._tracked_ft_ids.discard(stanza_id)

    def _to_log_str(self, xml):
        has_large_data = False
        for tag in ('{http://jabber.org/protocol/ibb}data', '{urn:xmpp:bob}data'):
            el = xml.find(f'.//{tag}')
            if el is not None and el.text and len(el.text) > 200:
                has_large_data = True; break

        if not has_large_data:
            return ET.tostring(xml, encoding='unicode')

        xml_copy = copy.deepcopy(xml)
        for tag in ('{http://jabber.org/protocol/ibb}data', '{urn:xmpp:bob}data'):
            for data in xml_copy.findall(f'.//{tag}'):
                if data.text and len(data.text) > 100:
                    data.text = data.text[:50] + f"...[TRUNCATED {len(data.text)} bytes]..." + data.text[-10:]
        return ET.tostring(xml_copy, encoding='unicode')

    # --- SOCKS5 Server and Client ---

    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except: return '127.0.0.1'

    async def _handle_socks5_client(self, reader, writer):
        try:
            # Handshake
            ver_nmethods = await reader.readexactly(2)
            if ver_nmethods[0] != 0x05: return
            methods = await reader.readexactly(ver_nmethods[1])
            if 0x00 not in methods:
                writer.write(b"\x05\xFF"); await writer.drain(); return
            writer.write(b"\x05\x00"); await writer.drain()

            # Request
            req = await reader.readexactly(4)
            if req != b"\x05\x01\x00\x03": return
            addr_len = (await reader.readexactly(1))[0]
            dst_addr = (await reader.readexactly(addr_len)).decode()
            port = await reader.readexactly(2)

            for sid, info in list(self.bot.pending_files.items()):
                if not isinstance(info, dict): continue
                t_sid = info.get('transport_sid', sid)
                expected = hashlib.sha1(f"{t_sid}{info['peer_jid'].full}{self.bot.boundjid.full}".encode()).hexdigest()
                if dst_addr == expected:
                    if info.get('downloading'):
                        writer.write(b"\x05\x01\x00\x03" + bytes([len(dst_addr)]) + dst_addr.encode() + port)
                        await writer.drain(); return

                    info['downloading'] = True
                    writer.write(b"\x05\x00\x00\x03" + bytes([len(dst_addr)]) + dst_addr.encode() + port)
                    await writer.drain()
                    await self.download_file_task(reader, info, info['peer_jid'], sid)
                    return

            writer.write(b"\x05\x01\x00\x03" + bytes([len(dst_addr)]) + dst_addr.encode() + port)
            await writer.drain()
        except Exception as e: logging.error(f"SOCKS5 server error: {e}")
        finally:
            try: writer.close()
            except: pass

    # --- OOB / URL Download ---

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
        if not await self._validate_oob_url(url, peer_jid): return

        parsed_url = urllib.parse.urlparse(url)
        if not fname or fname == os.path.basename(url):
            fname = os.path.basename(parsed_url.path) if parsed_url.path.strip('/') else "downloaded_file"

        from utils import is_php_file
        if is_php_file(fname):
            self.bot.send_message(mto=peer_jid, mbody=f"⚠️ Ошибка: Загрузка PHP-файлов запрещена ({fname})", mtype='chat')
            return

        user_dir, user_hash = self.bot.get_user_info(peer_jid)
        path = get_unique_path(os.path.join(user_dir, os.path.basename(fname).replace(' ', '_')))
        part_path = path + ".part"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=300) as resp:
                    if resp.status != 200:
                        self.bot.send_message(mto=peer_jid, mbody=f"⚠️ Ошибка: HTTP {resp.status}", mtype='chat'); return

                    fsize = int(resp.headers.get('Content-Length', 0))
                    if not self._check_quota_and_limit(user_dir, fsize, peer_jid): return

                    await self._stream_to_file(resp.content, part_path, fsize, peer_jid)
                    os.rename(part_path, path)
                    self.bot.send_message(mto=peer_jid, mbody=f"✅ Готово!\n{self.bot.base_url}/{user_hash}/{safe_quote(os.path.basename(path))}", mtype='chat')
        except Exception as e:
            logging.error(f"OOB error: {e}")
            if os.path.exists(part_path): os.remove(part_path)

    async def _validate_oob_url(self, url, peer_jid):
        try:
            parsed = urllib.parse.urlparse(url)
            if not parsed.hostname: return False
            addr_info = await asyncio.get_event_loop().getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
            for _, _, _, _, sockaddr in addr_info:
                ip = ipaddress.ip_address(sockaddr[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Доступ запрещён.", mtype='chat'); return False
            return True
        except: return False

    def _check_quota_and_limit(self, user_dir, fsize, peer_jid):
        MAX_OOB_SIZE = 500 * 1024 * 1024
        if fsize > MAX_OOB_SIZE:
            self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Превышен лимит 500 МБ.", mtype='chat'); return False
        if fsize > 0 and get_dir_size(user_dir) + fsize > QUOTA_LIMIT_BYTES:
            self.bot.send_message(mto=peer_jid, mbody="⚠ Квота превышена!", mtype='chat'); return False
        return True

    async def _stream_to_file(self, stream, path, expected_size, peer_jid):
        received, loop = 0, asyncio.get_event_loop()
        with open(path, 'wb') as f:
            while True:
                chunk = await asyncio.wait_for(stream.read(1048576), timeout=60)
                if not chunk: break
                await loop.run_in_executor(None, f.write, chunk)
                received += len(chunk)
                if received > 500 * 1024 * 1024: raise Exception("Size limit exceeded")
            await loop.run_in_executor(None, f.flush)
        if expected_size > 0 and received != expected_size:
            raise Exception(f"Incomplete download: {received}/{expected_size}")

    # --- Jingle Handling ---

    def handle_jingle(self, iq):
        if iq['type'] in ('error', 'result'): return
        jingle = iq.xml.find('{urn:xmpp:jingle:1}jingle')
        if jingle is None: return iq.reply().send()

        action = jingle.get('action')
        sid = jingle.get('sid')

        handlers = {
            'session-initiate': self._jingle_initiate,
            'transport-info': self._jingle_transport_info,
            'transport-replace': self._jingle_transport_replace,
            'transport-accept': lambda i, j, s: i.reply().send(),
            'session-terminate': self._jingle_terminate,
        }

        if action in handlers: handlers[action](iq, jingle, sid)
        else: iq.reply().send()

    def _jingle_initiate(self, iq, jingle, sid):
        if not self.bot.is_allowed(iq['from']):
            self.bot.send_message(mto=iq['from'], mbody=f"⚠️ Доступ запрещён: {ADMIN_JID}", mtype='chat')
            iq.error('not-allowed').send(); return

        content = jingle.find('{urn:xmpp:jingle:1}content')
        if content is None: return iq.reply().send()

        # Find file description
        ft_ns, description = None, None
        for ns in ['urn:xmpp:jingle:apps:file-transfer:5', 'urn:xmpp:jingle:apps:file-transfer:4']:
            description = content.find(f'{{{ns}}}description')
            if description is not None: ft_ns = ns; break
        if description is None: return iq.reply().send()

        file_tag = description.find(f'{{{ft_ns}}}file')
        name_tag, size_tag = file_tag.find(f'{{{ft_ns}}}name'), file_tag.find(f'{{{ft_ns}}}size')
        if name_tag is None or size_tag is None: return iq.reply().send()

        fname = os.path.basename(name_tag.text or "file").replace(' ', '_')
        from utils import is_php_file
        if is_php_file(fname):
            self.bot.send_message(mto=iq['from'], mbody=f"⚠️ Ошибка: Загрузка PHP запрещена.", mtype='chat')
            iq.error('not-acceptable').send(); return

        fsize = int(size_tag.text or 0)
        user_dir, _ = self.bot.get_user_info(iq['from'])
        if get_dir_size(user_dir) + fsize > QUOTA_LIMIT_BYTES:
            iq.error('not-acceptable').send(); return

        ibb_t = content.find('{urn:xmpp:jingle:transports:ibb:1}transport')
        s5b_t = content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
        transport_sid = s5b_t.get('sid') if s5b_t is not None else (ibb_t.get('sid') if ibb_t is not None else sid)

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
        self._jingle_accept(iq, sid, ft_ns, fname, fsize, content, s5b_t, ibb_t, transport_sid)

    def _jingle_accept(self, iq, sid, ft_ns, fname, fsize, content, s5b_t, ibb_t, transport_sid):
        accept_iq = self.bot.make_iq_set(ito=iq['from'])
        res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-accept', 'sid': sid, 'initiator': iq['from'].full})
        res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {'creator': content.get('creator'), 'name': content.get('name')})
        res_d = ET.SubElement(res_c, f'{{{ft_ns}}}description')
        res_f = ET.SubElement(res_d, f'{{{ft_ns}}}file')
        ET.SubElement(res_f, f'{{{ft_ns}}}name').text = fname
        ET.SubElement(res_f, f'{{{ft_ns}}}size').text = str(fsize)

        if s5b_t is not None:
            res_t = ET.SubElement(res_c, '{urn:xmpp:jingle:transports:s5b:1}transport', {'sid': transport_sid, 'mode': 'tcp'})
            self._add_s5b_candidates(res_t)
        else:
            block_size = int(ibb_t.get('block-size', '32768')) if ibb_t is not None else 32768
            stanzas = ibb_t.get('stanzas', 'message') if ibb_t is not None else 'message'
            ET.SubElement(res_c, '{urn:xmpp:jingle:transports:ibb:1}transport', {'block-size': str(block_size), 'sid': transport_sid, 'stanzas': stanzas})
            self._setup_ibb_stream(transport_sid, iq['from'], block_size, stanzas == 'message')

        accept_iq.append(res_j); accept_iq.send()

        if s5b_t is not None and s5b_t.findall('{urn:xmpp:jingle:transports:s5b:1}candidate'):
            self.bot.pending_files[sid]['s5b_connecting'] = True
            asyncio.create_task(self._socks5_connect_and_save(iq, jingle_sid=sid))

    def _add_s5b_candidates(self, res_t):
        local_ip = self.get_local_ip()
        ET.SubElement(res_t, '{urn:xmpp:jingle:transports:s5b:1}candidate', host=local_ip, port='1080', jid=self.bot.boundjid.full, cid='local', priority='8253074', type='host')
        for p in ['proxy.eu.jabber.network', 'proxy.jabber.ru']:
            ET.SubElement(res_t, '{urn:xmpp:jingle:transports:s5b:1}candidate', host=p, port='1080', jid=p, cid=p, priority='65536', type='proxy')

    def _jingle_transport_info(self, iq, jingle, sid):
        content = jingle.find('{urn:xmpp:jingle:1}content')
        if content is not None:
            s5b = content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
            if s5b is not None and not self.bot.pending_files.get(sid, {}).get('s5b_connecting'):
                self.bot.pending_files[sid]['s5b_connecting'] = True
                asyncio.create_task(self._socks5_connect_and_save(iq, jingle_sid=sid))
        iq.reply().send()

    def _jingle_transport_replace(self, iq, jingle, sid):
        content = jingle.find('{urn:xmpp:jingle:1}content')
        ibb_t = content.find('{urn:xmpp:jingle:transports:ibb:1}transport') if content is not None else None
        if ibb_t is not None and sid in self.bot.pending_files:
            ibb_sid = ibb_t.get('sid')
            self.bot.pending_files[sid].update({'transport_sid': ibb_sid, 'ibb_stanzas': ibb_t.get('stanzas')})
            self.bot.pending_files[ibb_sid] = self.bot.pending_files[sid]

            reply = self.bot.make_iq_set(ito=iq['from'])
            res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-accept', 'sid': sid, 'initiator': iq['from'].full})
            res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {'creator': content.get('creator'), 'name': content.get('name')})
            ET.SubElement(res_c, '{urn:xmpp:jingle:transports:ibb:1}transport', {'sid': ibb_sid, 'block-size': '32768', 'stanzas': 'message'})
            self._setup_ibb_stream(ibb_sid, iq['from'], 32768, True)
            reply.append(res_j); reply.send()
        iq.reply().send()

    def _jingle_terminate(self, iq, jingle, sid):
        task = self.bot.pending_files.get(f"task_{sid}")
        if isinstance(task, asyncio.Task) and not task.done(): task.cancel()
        self.bot.pending_files.pop(sid, None)
        iq.reply().send()

    # --- SI Handling ---

    def handle_raw_si(self, iq):
        if iq['type'] in ('error', 'result'): return
        if not self.bot.is_allowed(iq['from']):
            iq.error('not-allowed').send(); return

        si = iq.xml.find('{http://jabber.org/protocol/si}si')
        sid = si.get('id')
        file_tag = si.find('{http://jabber.org/protocol/si/profile/file-transfer}file')
        fname, fsize = os.path.basename(file_tag.get('name') or "file").replace(' ', '_'), int(file_tag.get('size', 0))

        from utils import is_php_file
        if is_php_file(fname): iq.error('not-acceptable').send(); return

        user_dir, _ = self.bot.get_user_info(iq['from'])
        if get_dir_size(user_dir) + fsize > QUOTA_LIMIT_BYTES:
            iq.error('not-acceptable').send(); return

        methods = self._get_si_methods(si)
        chosen = next((m for m in ['http://jabber.org/protocol/bytestreams', 'http://jabber.org/protocol/ibb'] if m in methods), None)
        if not chosen: iq.error('bad-request').send(); return

        self.bot.pending_files[sid] = {
            'name': fname, 'size': fsize, 'timestamp': asyncio.get_event_loop().time(),
            'ibb_allowed': 'http://jabber.org/protocol/ibb' in methods,
            'peer_jid': iq['from'], 'transport_sid': sid, 'downloading': False
        }
        self._send_si_reply(iq, sid, chosen)

    def _get_si_methods(self, si):
        neg = si.find('{http://jabber.org/protocol/feature-neg}feature')
        if neg is None: return []
        x = neg.find('{jabber:x:data}x')
        if x is None: return []
        field = next((f for f in x.findall('{jabber:x:data}field') if f.get('var') == 'stream-method'), None)
        if field is None: return []
        return [v.text for v in field.findall('{jabber:x:data}value')] + [v.text for v in field.findall('{jabber:x:data}option/{jabber:x:data}value')]

    def _send_si_reply(self, iq, sid, method):
        reply = iq.reply()
        si = ET.Element('{http://jabber.org/protocol/si}si', {'id': sid})
        feature = ET.SubElement(si, '{http://jabber.org/protocol/feature-neg}feature')
        x = ET.SubElement(feature, '{jabber:x:data}x', type='submit')
        field = ET.SubElement(x, '{jabber:x:data}field', var='stream-method')
        ET.SubElement(field, '{jabber:x:data}value').text = method
        reply.append(si); reply.send()

    # --- SOCKS5 Bytestreams Handlers ---

    def handle_raw_s5b(self, iq):
        if iq['type'] in ('error', 'result'): return
        query = iq.xml.find('{http://jabber.org/protocol/bytestreams}query')
        if query is not None and query.find('{http://jabber.org/protocol/bytestreams}streamhost-used') is not None:
             asyncio.create_task(self._socks5_connect_and_save(iq))
        else:
             asyncio.create_task(self._socks5_connect_and_save(iq))

    async def _socks5_connect_and_save(self, iq, jingle_sid=None):
        try:
            sid, peer_full, hosts, used_jid = self._parse_s5b_iq(iq, jingle_sid)
            if hosts is None: return # Handled or error

            file_info = self.bot.pending_files.get(sid)
            if not file_info: return

            t_sid = file_info.get('transport_sid', sid)
            dst_addr = hashlib.sha1(f"{t_sid}{peer_full}{self.bot.boundjid.full}".encode()).hexdigest()

            for host in hosts:
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(host.get('host'), int(host.get('port', 1080))), 5)
                    if not await self._socks5_handshake(reader, writer, dst_addr): continue

                    self._confirm_s5b_candidate(iq, jingle_sid, sid, host, file_info)
                    file_info['downloading'] = True
                    await self.download_file_task(reader, file_info, iq['from'], sid)
                    writer.close(); return
                except: continue

            await self._handle_s5b_failure(iq, jingle_sid, sid, file_info)
        except Exception as e: logging.error(f"S5B ERROR: {e}")

    def _parse_s5b_iq(self, iq, jingle_sid):
        if jingle_sid:
            jingle = iq.xml.find('{urn:xmpp:jingle:1}jingle')
            content = jingle.find('{urn:xmpp:jingle:1}content')
            query = content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
            return jingle_sid, iq['from'].full, query.findall('{urn:xmpp:jingle:transports:s5b:1}candidate'), None

        query = iq.xml.find('{http://jabber.org/protocol/bytestreams}query')
        sid, peer_full = query.get('sid'), iq['from'].full
        used = query.find('{http://jabber.org/protocol/bytestreams}streamhost-used')

        if used is not None:
            jid = used.get('jid')
            # For simplicity, we only handle our own proxies or known ones
            # In a full impl, we'd have a list of discovered proxies
            proxies = {'proxy.eu.jabber.network': 'proxy.eu.jabber.network', 'proxy.jabber.ru': 'proxy.jabber.ru'}
            host = proxies.get(jid, jid)
            return sid, peer_full, [ET.Element('streamhost', host=host, port='1080', jid=jid)], jid

        hosts = query.findall('{http://jabber.org/protocol/bytestreams}streamhost')
        if not hosts:
            self._send_s5b_streamhosts(iq, sid)
            return sid, peer_full, None, None
        return sid, peer_full, hosts, None

    async def _socks5_handshake(self, reader, writer, dst_addr):
        writer.write(b"\x05\x01\x00"); await writer.drain()
        if await reader.read(2) != b"\x05\x00": writer.close(); return False
        writer.write(b"\x05\x01\x00\x03" + bytes([len(dst_addr)]) + dst_addr.encode() + b"\x00\x00"); await writer.drain()
        resp = await reader.read(4)
        if not resp or resp[1] != 0x00: writer.close(); return False
        atyp = resp[3]
        if atyp == 0x01: await reader.read(6)
        elif atyp == 0x03: addr_len = await reader.read(1); await reader.read(addr_len[0] + 2)
        elif atyp == 0x04: await reader.read(18)
        return True

    def _confirm_s5b_candidate(self, iq, jingle_sid, sid, host, file_info):
        if jingle_sid:
            reply = self.bot.make_iq_set(ito=iq['from'])
            res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-info', 'sid': jingle_sid, 'initiator': iq['from'].full})
            res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {'creator': file_info.get('content_creator', 'initiator'), 'name': file_info.get('content_name', 'file')})
            res_t = ET.SubElement(res_c, '{urn:xmpp:jingle:transports:s5b:1}transport', {'sid': sid})
            ET.SubElement(res_t, '{urn:xmpp:jingle:transports:s5b:1}candidate-used', cid=host.get('cid'))
            reply.append(res_j); reply.send()
        else:
            reply = iq.reply()
            if iq.xml.find('.//{http://jabber.org/protocol/bytestreams}streamhost-used') is None:
                query = ET.Element('{http://jabber.org/protocol/bytestreams}query', {'sid': sid})
                ET.SubElement(query, '{http://jabber.org/protocol/bytestreams}streamhost-used', jid=host.get('jid'))
                reply.append(query)
            reply.send()

    def _send_s5b_streamhosts(self, iq, sid):
        reply = iq.reply()
        query = ET.Element('{http://jabber.org/protocol/bytestreams}query', {'sid': sid})
        local_ip = self.get_local_ip()
        ET.SubElement(query, '{http://jabber.org/protocol/bytestreams}streamhost', host=local_ip, port='1080', jid=self.bot.boundjid.full)
        for p in ['proxy.eu.jabber.network', 'proxy.jabber.ru']:
            ET.SubElement(query, '{http://jabber.org/protocol/bytestreams}streamhost', host=p, port='1080', jid=p)
        reply.append(query); reply.send()

    async def _handle_s5b_failure(self, iq, jingle_sid, sid, file_info):
        if not jingle_sid: iq.error('service-unavailable').send()
        elif file_info.get('ibb_allowed'):
            new_sid = f"fallback_{sid}"
            file_info.update({'transport_sid': new_sid, 'ibb_stanzas': 'message'})
            self.bot.pending_files[new_sid] = file_info

            reply = self.bot.make_iq_set(ito=iq['from'])
            res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-replace', 'sid': sid, 'initiator': iq['from'].full})
            res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {'creator': file_info.get('content_creator', 'initiator'), 'name': file_info.get('content_name', 'file')})
            ET.SubElement(res_c, '{urn:xmpp:jingle:transports:ibb:1}transport', {'sid': new_sid, 'block-size': '32768', 'stanzas': 'message'})
            self._setup_ibb_stream(new_sid, iq['from'], 32768, True)
            reply.append(res_j); reply.send()
        else: self.bot.pending_files.pop(sid, None)

    # --- IBB Handling ---

    def handle_ibb_stream(self, stream):
        file_info = self.bot.pending_files.get(stream.sid)
        if file_info and file_info['peer_jid'].bare == stream.peer_jid.bare:
            file_info['stream'] = stream
            self.bot.pending_files[f"task_{stream.sid}"] = asyncio.create_task(self.download_file_task(stream, file_info, stream.peer_jid, stream.sid))
        else: stream.close()

    def _setup_ibb_stream(self, sid, peer_jid, block_size, use_msg):
        from slixmpp.plugins.xep_0047 import IBBytestream
        stream = IBBytestream(self.bot, sid, block_size, self.bot.boundjid, peer_jid, use_msg)
        self.bot['xep_0047'].api['set_stream'](self.bot.boundjid, sid, peer_jid, stream)
        self.bot.event('ibb_stream_start', stream)

    def _intercept_ibb_messages(self, stanza):
        if not (hasattr(stanza, 'xml') and stanza.xml.tag.endswith('message')): return stanza
        data_el = stanza.xml.find('{http://jabber.org/protocol/ibb}data')
        close_el = stanza.xml.find('{http://jabber.org/protocol/ibb}close')
        if data_el is not None:
            info = self.bot.pending_files.get(data_el.get('sid'))
            if info and 'stream' in info:
                info['timestamp'] = asyncio.get_event_loop().time()
                if data_el.text: info['stream'].recv_queue.put_nowait(base64.b64decode(data_el.text))
                return None
        elif close_el is not None:
            info = self.bot.pending_files.get(close_el.get('sid'))
            if info and 'stream' in info:
                info['timestamp'] = asyncio.get_event_loop().time()
                info['stream'].recv_queue.put_nowait(None); return None
        return stanza

    # --- Data Reception Task ---

    async def download_file_task(self, reader, file_info, peer_jid, sid):
        user_dir, user_hash = self.bot.get_user_info(peer_jid)
        path = get_unique_path(os.path.join(user_dir, os.path.basename(file_info['name'])))
        part_path = path + ".part"
        received, loop = 0, asyncio.get_event_loop()
        try:
            with open(part_path, 'wb') as f:
                while received < file_info['size']:
                    if hasattr(reader, 'recv_queue'): chunk = await asyncio.wait_for(reader.recv_queue.get(), timeout=60)
                    else: chunk = await asyncio.wait_for(reader.read(min(file_info['size'] - received, 1048576)), timeout=60)
                    if not chunk: break
                    await loop.run_in_executor(None, f.write, chunk)
                    received += len(chunk)
                    if sid in self.bot.pending_files: self.bot.pending_files[sid]['timestamp'] = loop.time()
                await loop.run_in_executor(None, f.flush)

            if received == file_info['size']:
                os.rename(part_path, path)
                self.bot.send_message(mto=peer_jid, mbody=f"✅ Готово!\n{self.bot.base_url}/{user_hash}/{safe_quote(os.path.basename(path))}", mtype='chat')
                self._send_jingle_completion(peer_jid, file_info)
            else: raise Exception("Incomplete")
        except Exception as e:
            logging.error(f"Download error sid={sid}: {e}")
            if os.path.exists(part_path): os.remove(part_path)
            self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Файл получен не полностью.", mtype='chat')
        finally:
            self._cleanup_session(sid)

    def _send_jingle_completion(self, peer_jid, file_info):
        sid, ns = file_info.get('session_sid'), file_info.get('ft_ns')
        if not (sid and ns): return

        info = self.bot.make_iq_set(ito=peer_jid)
        res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-info', 'sid': sid, 'initiator': peer_jid.full})
        ET.SubElement(res_j, f'{{{ns}}}received')
        info.append(res_j); info.send()

        term = self.bot.make_iq_set(ito=peer_jid)
        res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-terminate', 'sid': sid, 'initiator': peer_jid.full})
        ET.SubElement(ET.SubElement(res_j, '{urn:xmpp:jingle:1}reason'), '{urn:xmpp:jingle:1}success')
        term.append(res_j); term.send()

    def _cleanup_session(self, sid):
        info = self.bot.pending_files.pop(sid, None)
        if info: self.bot.pending_files.pop(info.get('transport_sid'), None)
