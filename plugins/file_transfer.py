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
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
    from aiortc.contrib.signaling import BYE
    HAS_WEBRTC = True
except ImportError:
    HAS_WEBRTC = False

from slixmpp.xmlstream import ET, matcher, handler
from config import ADMIN_JID, ADMIN_NOTIFY_LEVEL, QUOTA_LIMIT_BYTES, SOCKS5_PORT, SOCKS5_IP, STUN_SERVERS, TURN_SERVERS
from utils import get_dir_size, safe_quote, get_unique_path
from .base import BasePlugin

class JingleSession:
    def __init__(self, sid, peer_jid, bot_jid, is_initiator=False):
        self.sid = sid
        self.peer_jid = peer_jid
        self.bot_jid = bot_jid
        self.is_initiator = is_initiator
        self.state = 'starting'
        self.file_info = {}
        self.transport_type = 's5b' # default
        self.transport_sid = sid
        self.candidates = []
        self.used_candidate = None
        self.timestamp = asyncio.get_event_loop().time()
        self.task = None
        self.webrtc_pc = None # For ICE-UDP
        self.webrtc_dc = None # For ICE-UDP
        self.ufrag = None
        self.pwd = None
        self.ufrag_remote = None
        self.pwd_remote = None

    def get_dst_addr(self):
        initiator = self.bot_jid.full if self.is_initiator else self.peer_jid.full
        target = self.peer_jid.full if self.is_initiator else self.bot_jid.full
        return hashlib.sha1(f"{self.transport_sid}{initiator}{target}".encode()).hexdigest()

class FileTransferPlugin(BasePlugin):
    def _get_rtc_config(self):
        if not HAS_WEBRTC: return None
        from aiortc import RTCConfiguration, RTCIceServer
        ice_servers = []
        for s in STUN_SERVERS:
            if s: ice_servers.append(RTCIceServer(urls=s.strip()))
        for s in TURN_SERVERS:
            if not s: continue
            # Format: turn:user:pass@host:port
            if '@' in s:
                prefix, host_port = s.split('@', 1)
                proto, auth = prefix.split(':', 1)
                user, pwd = auth.split(':', 1)
                ice_servers.append(RTCIceServer(urls=f"{proto}:{host_port}", username=user, credential=pwd))
            else:
                ice_servers.append(RTCIceServer(urls=s.strip()))
        return RTCConfiguration(iceServers=ice_servers)

    def _rtc_to_jingle_candidate(self, candidate):
        return {
            'component': '1',
            'foundation': candidate.foundation,
            'id': str(id(candidate)),
            'ip': candidate.ip,
            'port': str(candidate.port),
            'priority': str(candidate.priority),
            'protocol': candidate.protocol,
            'type': candidate.type,
            'generation': '0',
            'network': '0'
        }

    def _jingle_to_rtc_candidate(self, c):
        return RTCIceCandidate(
            component=int(c.get('component', '1')),
            foundation=c.get('foundation'),
            ip=c.get('ip'),
            port=int(c.get('port')),
            priority=int(c.get('priority')),
            protocol=c.get('protocol'),
            type=c.get('type'),
            sdpMid='0',
            sdpMLineIndex=0
        )

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

    FT_NAMESPACES = [
        'urn:xmpp:jingle:1',
        'urn:xmpp:jingle:apps:file-transfer:5',
        'urn:xmpp:jingle:transports:s5b:1',
        'urn:xmpp:jingle:transports:ice-udp:1',
        'http://jabber.org/protocol/bytestreams', # Needed for S5B candidates
        'jabber:iq:oob',
        'jabber:x:oob',
        'urn:xmpp:bob',
        'urn:xmpp:thumbs:1'
    ]

    def __init__(self, bot):
        super().__init__(bot)
        # Disable default Slixmpp S5B handler if it exists to avoid double responses
        self.bot.remove_handler('SOCKS5 Bytestreams')
        self.jingle_sessions = {}
        self._tracked_ft_ids = set()
        self._ft_ns_prefixes = [f'{{{ns}}}' for ns in self.FT_NAMESPACES]
        self.proxies = copy.deepcopy(self.KNOWN_PROXIES)
        self.bot.add_event_handler("session_start", self.discover_proxies)
        self.bot.add_event_handler("xml_in", self.handle_xml_in)
        self.bot.add_event_handler("xml_out", self.handle_xml_out)
        self.bot.add_event_handler("jingle_done", self.handle_jingle_done)

        # Регистрация обработчиков IQ
        self.bot.register_handler(
            handler.Callback('Jingle', matcher.MatchXPath('{jabber:client}iq/{urn:xmpp:jingle:1}jingle'), self.handle_jingle)
        )
        self.bot.register_handler(
            handler.Callback('OOB', matcher.MatchXPath('{jabber:client}iq/{jabber:iq:oob}query'), self.handle_iq_oob)
        )

        # Запускаем собственный SOCKS5 сервер
        asyncio.create_task(asyncio.start_server(self._handle_socks5_client, '0.0.0.0', SOCKS5_PORT))

        # Регистрация фич в Service Discovery (XEP-0030)
        for ns in self.FT_NAMESPACES:
            self.bot['xep_0030'].add_feature(ns)

    def handle_jingle_done(self, data):
        sid, peer_jid, file_info = data['sid'], data['peer_jid'], data['file_info']
        ft_ns = file_info.get('ft_ns', 'urn:xmpp:jingle:apps:file-transfer:5')
        logging.info(f"JINGLE COMPLETE: Sending session-info (received) and session-terminate (success) for sid={sid}")

        info_iq = self.bot.make_iq_set(ito=peer_jid)
        res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-info', 'sid': sid, 'initiator': peer_jid.full})
        ET.SubElement(res_j, f'{{{ft_ns}}}received')
        info_iq.append(res_j); self.send_iq(info_iq)

        term_iq = self.bot.make_iq_set(ito=peer_jid)
        res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-terminate', 'sid': sid, 'initiator': peer_jid.full})
        reason = ET.SubElement(res_j, '{urn:xmpp:jingle:1}reason')
        ET.SubElement(reason, '{urn:xmpp:jingle:1}success')
        term_iq.append(res_j); self.send_iq(term_iq)

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
        # Оптимизация: не делаем deepcopy для данных BoB, если они огромные
        has_large_data = False
        for tag in ('{urn:xmpp:bob}data',):
            el = xml.find(f'.//{tag}')
            if el is not None and el.text and len(el.text) > 200:
                has_large_data = True; break

        if not has_large_data:
            return ET.tostring(xml, encoding='unicode')

        xml_copy = copy.deepcopy(xml)
        for tag in ('{urn:xmpp:bob}data',):
            for data in xml_copy.findall(f'.//{tag}'):
                if data.text and len(data.text) > 100:
                    data.text = data.text[:50] + f"...[TRUNCATED {len(data.text)} bytes]..." + data.text[-10:]

        if xml_copy.tag.endswith('}data') and ('urn:xmpp:bob' in xml_copy.tag):
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
                        logging.debug(f"SOCKS5: Match found for sid={sid}, dst_addr={dst_addr}. Starting download task.")
                        logging.info(f"SOCKS5: Recognized incoming connection for sid={sid}, dst_addr={dst_addr}")
                        await self.download_file_task(reader, info, info['peer_jid'], sid, writer=writer)
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
        if query is None: return self.send_iq(iq.reply())
        url_tag = query.find('{jabber:iq:oob}url')
        if url_tag is None or not url_tag.text: return self.send_iq(iq.reply())
        url = url_tag.text
        desc = query.find('{jabber:iq:oob}desc')
        fname = desc.text if desc is not None and desc.text else os.path.basename(url)
        self.bot.pending_files[f"oob_{url}"] = asyncio.create_task(self.download_from_url(url, fname, iq['from']))
        self.send_iq(iq.reply())

    async def download_from_url(self, url, fname, peer_jid):
        logging.info(f"Downloading OOB from {url}")

        # SSRF Protection: Resolve and validate IP address
        try:
            parsed_url = urllib.parse.urlparse(url)
            host = parsed_url.hostname
            if not host:
                logging.error(f"OOB: Invalid URL host in {url}")
                return
            addr_info = await asyncio.get_event_loop().getaddrinfo(host, parsed_url.port or (443 if parsed_url.scheme == 'https' else 80))
            for family, _, _, _, sockaddr in addr_info:
                ip_addr = ipaddress.ip_address(sockaddr[0])
                if ip_addr.is_private or ip_addr.is_loopback or ip_addr.is_link_local:
                    logging.error(f"OOB: SSRF attempt blocked for {url} (IP: {ip_addr})")
                    self.bot.send_message(mto=peer_jid, mbody=f"⚠️ Ошибка: Доступ к этому адресу запрещён.", mtype='chat')
                    return
        except Exception as e:
            logging.error(f"OOB: SSRF check error for {url}: {e}")
            return

        # Improved filename extraction
        if not fname or fname == os.path.basename(url):
            path_part = parsed_url.path
            fname = os.path.basename(path_part) if path_part.strip('/') else "downloaded_file"

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
                        # Limit arbitrary URL downloads to 500MB
                        MAX_OOB_SIZE = 500 * 1024 * 1024
                        if fsize > MAX_OOB_SIZE:
                            self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Размер файла превышает лимит (500 МБ).", mtype='chat')
                            return
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
                                    if received > MAX_OOB_SIZE:
                                        logging.error(f"OOB: File exceeded 500MB limit during download: {url}")
                                        self.bot.send_message(mto=peer_jid, mbody="⚠️ Ошибка: Размер файла превышает лимит (500 МБ).", mtype='chat')
                                        if os.path.exists(part_path): os.remove(part_path)
                                        return
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
                    else:
                        logging.error(f"OOB download failed: HTTP {resp.status}")
                        self.bot.send_message(mto=peer_jid, mbody=f"⚠️ Ошибка: Не удалось загрузить файл (HTTP {resp.status})", mtype='chat')
        except Exception as e:
            logging.error(f"OOB download error: {e}")
            if os.path.exists(part_path): os.remove(part_path)

    def handle_jingle(self, iq):
        try:
            if iq['type'] in ('error', 'result'): return
            jingle = iq.xml.find('{urn:xmpp:jingle:1}jingle')
            if jingle is None:
                return self.send_iq(iq.reply())
            action, sid = jingle.get('action'), jingle.get('sid')
            logging.info(f"JINGLE EVENT: action={action}, sid={sid}, from={iq['from']}")

            if action == 'session-initiate':
                if not self.bot.is_allowed(iq['from']):
                    logging.warning(f"JINGLE access denied for {iq['from']}")
                    self.bot.send_message(mto=iq['from'], mbody=f"⚠️ Доступ запрещён. Пожалуйста, обратитесь к администратору для получения доступа: {ADMIN_JID}", mtype='chat')
                    self.send_iq(iq.error('not-allowed'))
                    return

                content = jingle.find('{urn:xmpp:jingle:1}content')
                if content is None: return self.send_iq(iq.reply())

                ft_ns = 'urn:xmpp:jingle:apps:file-transfer:5'
                description = content.find(f'{{{ft_ns}}}description')
                if description is None:
                    ft_ns = 'urn:xmpp:jingle:apps:file-transfer:4'
                    description = content.find(f'{{{ft_ns}}}description')
                if description is None: return self.send_iq(iq.reply())

                file_tag = description.find(f'{{{ft_ns}}}file')
                if file_tag is None: return self.send_iq(iq.reply())

                name_tag = file_tag.find(f'{{{ft_ns}}}name')
                size_tag = file_tag.find(f'{{{ft_ns}}}size')
                if name_tag is None or size_tag is None: return self.send_iq(iq.reply())

                fname = os.path.basename(name_tag.text or "file").replace(' ', '_')
                from utils import is_php_file
                if is_php_file(fname):
                    self.bot.send_message(mto=iq['from'], mbody=f"⚠️ Ошибка: Загрузка PHP-файлов запрещена ({fname})", mtype='chat')
                    self.send_iq(iq.error('not-acceptable'))
                    return

                try: fsize = int(size_tag.text or 0)
                except: fsize = 0
                user_dir, _ = self.bot.get_user_info(iq['from'])
                if get_dir_size(user_dir) + fsize > QUOTA_LIMIT_BYTES:
                    self.send_iq(iq.error('not-acceptable'))
                    return

                # Transport Selection Logic
                s5b_ns = 'urn:xmpp:jingle:transports:s5b:1'
                ice_ns = 'urn:xmpp:jingle:transports:ice-udp:1'
                ibb_ns = 'urn:xmpp:jingle:transports:ibb:1'

                s5b_t = content.find(f'{{{s5b_ns}}}transport')
                ice_t = content.find(f'{{{ice_ns}}}transport')
                ibb_t = content.find(f'{{{ibb_ns}}}transport')

                chosen_transport_el = None
                transport_type = None

                # Priority: ICE-UDP > S5B > IBB
                if ice_t is not None and HAS_WEBRTC:
                    chosen_transport_el = ice_t
                    transport_type = 'ice-udp'
                elif s5b_t is not None:
                    chosen_transport_el = s5b_t
                    transport_type = 's5b'
                elif ibb_t is not None:
                    chosen_transport_el = ibb_t
                    transport_type = 'ibb'

                if chosen_transport_el is None:
                    logging.warning(f"JINGLE: No supported transport offered by {iq['from']}")
                    self.send_iq(iq.error('feature-not-implemented'))
                    return

                transport_sid = chosen_transport_el.get('sid') or sid
                session = JingleSession(sid, iq['from'], self.bot.boundjid)
                session.file_info = {'name': fname, 'size': fsize}
                session.transport_sid = transport_sid
                session.transport_type = transport_type
                self.jingle_sessions[sid] = session

                self.bot.pending_files[sid] = {
                    'name': fname, 'size': fsize, 'timestamp': asyncio.get_event_loop().time(),
                    'peer_jid': iq['from'],
                    'content_name': content.get('name'), 'content_creator': content.get('creator'),
                    'ft_ns': ft_ns, 'transport_sid': transport_sid, 's5b_connecting': False,
                    'session_sid': sid, 'downloading': False
                }
                if transport_sid != sid:
                    self.bot.pending_files[transport_sid] = self.bot.pending_files[sid]

                # Acknowledge the initiate IQ
                self.send_iq(iq.reply())

                # Request BoB thumbnails if present
                thumb_tag = file_tag.find('{urn:xmpp:thumbs:1}thumbnail')
                if thumb_tag is not None:
                    uri = thumb_tag.get('uri')
                    if uri and uri.startswith('cid:'):
                        asyncio.create_task(self.request_bob_data(iq['from'], uri, fname))

                try:
                    accept_iq = self.bot.make_iq_set(ito=iq['from'])
                    res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {
                        'action': 'session-accept',
                        'sid': sid,
                        'initiator': iq['from'].full
                    })
                    res_c = ET.SubElement(res_j, '{urn:xmpp:jingle:1}content', {
                        'creator': content.get('creator'),
                        'name': content.get('name')
                    })
                    res_d = ET.SubElement(res_c, f'{{{ft_ns}}}description')
                    res_f = ET.SubElement(res_d, f'{{{ft_ns}}}file')
                    ET.SubElement(res_f, f'{{{ft_ns}}}name').text = fname
                    ET.SubElement(res_f, f'{{{ft_ns}}}size').text = str(fsize)

                    # Requirement 1 & 2: Mirror the transport element exactly
                    res_t = copy.deepcopy(chosen_transport_el)
                    res_c.append(res_t)

                    if transport_type == 'ice-udp':
                        asyncio.create_task(self._setup_webrtc_responder(sid, iq['from'], chosen_transport_el))
                    elif transport_type == 's5b':
                        # Requirement 3: Append additional candidates to the copied transport
                        local_ip = self.get_local_ip()
                        ET.SubElement(res_t, f'{{{s5b_ns}}}candidate', {
                            'host': local_ip,
                            'port': str(SOCKS5_PORT),
                            'jid': self.bot.boundjid.full,
                            'cid': 'direct-host-local',
                            'priority': '8253074',
                            'type': 'host'
                        })
                        if SOCKS5_IP and SOCKS5_IP != local_ip:
                            ET.SubElement(res_t, f'{{{s5b_ns}}}candidate', {
                                'host': SOCKS5_IP,
                                'port': str(SOCKS5_PORT),
                                'jid': self.bot.boundjid.full,
                                'cid': 'direct-host-public',
                                'priority': '8252818',
                                'type': 'host'
                            })
                        for p_jid, p_info in self.proxies.items():
                            ET.SubElement(res_t, f'{{{s5b_ns}}}candidate', {
                                'host': p_info['host'],
                                'port': str(p_info['port']),
                                'jid': p_jid,
                                'cid': hashlib.md5(p_jid.encode()).hexdigest(),
                                'priority': '65536',
                                'type': 'proxy'
                            })
                    elif transport_type == 'ibb':
                        pass

                    accept_iq.append(res_j)
                    self.send_iq(accept_iq)

                    if transport_type == 's5b' and chosen_transport_el.findall(f'{{{s5b_ns}}}candidate'):
                        self.bot.pending_files[sid]['s5b_connecting'] = True
                        self.bot.pending_files[f"jingle_s5b_{sid}"] = asyncio.create_task(
                            self._socks5_connect_and_save(iq, jingle_sid=sid)
                        )
                except Exception as e:
                    logging.error(f"JINGLE ACCEPT ERROR: {e}")
            elif action == 'transport-info':
                session = self.jingle_sessions.get(sid)
                content = jingle.find('{urn:xmpp:jingle:1}content')
                if content is not None:
                    s5b_t = content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
                    ice_t = content.find('{urn:xmpp:jingle:transports:ice-udp:1}transport')

                    if s5b_t is not None and not self.bot.pending_files.get(sid, {}).get('s5b_connecting'):
                        self.bot.pending_files[sid]['s5b_connecting'] = True
                        self.bot.pending_files[f"jingle_s5b_info_{sid}"] = asyncio.create_task(self._socks5_connect_and_save(iq, jingle_sid=sid))

                    if ice_t is not None and session and session.webrtc_pc:
                        ufrag, pwd = ice_t.get('ufrag'), ice_t.get('pwd')
                        if ufrag: session.ufrag_remote = ufrag
                        if pwd: session.pwd_remote = pwd

                        candidates = ice_t.findall('{urn:xmpp:jingle:transports:ice-udp:1}candidate')
                        for c in candidates:
                            rtc_c = self._jingle_to_rtc_candidate(c)
                            asyncio.create_task(session.webrtc_pc.addIceCandidate(rtc_c))

                self.send_iq(iq.reply())
            elif action == 'transport-replace':
                content = jingle.find('{urn:xmpp:jingle:1}content')
                if content is not None:
                    s5b_t = content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
                    if s5b_t is not None:
                        # Handle S5B transport replace (e.g. from ICE-UDP or other)
                        pass
                self.send_iq(iq.reply())
            elif action == 'transport-accept':
                self.send_iq(iq.reply())
            elif action == 'session-terminate':
                if sid in self.jingle_sessions:
                    session = self.jingle_sessions[sid]
                    if session.task and not session.task.done(): session.task.cancel()
                    del self.jingle_sessions[sid]

                if sid in self.bot.pending_files: del self.bot.pending_files[sid]
                self.send_iq(iq.reply())
            elif action == 'session-accept':
                if sid in self.jingle_sessions:
                    session = self.jingle_sessions[sid]
                    logging.info(f"JINGLE ACCEPTED: sid={sid}")
                    content = jingle.find('{urn:xmpp:jingle:1}content')
                    if content is not None:
                         s5b_t = content.find('{urn:xmpp:jingle:transports:s5b:1}transport')
                         if s5b_t is not None:
                              # Peer accepted our S5B transport
                              asyncio.create_task(self._socks5_connect_and_save(iq, jingle_sid=sid))
                self.send_iq(iq.reply())
            else:
                self.send_iq(iq.reply())
        except Exception as e:
            logging.error(f"JINGLE IQ ERROR: {e}")
            try: self.send_iq(iq.error('internal-server-error'))
            except: pass


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

                session = self.jingle_sessions.get(sid)
                if session:
                    dst_addr = session.get_dst_addr()
                else:
                    return
            else:
                query = iq.xml.find('{http://jabber.org/protocol/bytestreams}query')
                if query is None: return
                sid, peer_full = query.get('sid'), iq['from'].full
                used = query.find('{http://jabber.org/protocol/bytestreams}streamhost-used')
                if used is not None:
                    jid = used.get('jid'); proxy = self.proxies.get(jid)
                    if proxy: hosts = [ET.Element('streamhost', host=proxy['host'], port=str(proxy['port']), jid=jid)]
                    else: self.send_iq(iq.error('item-not-found')); return
                else:
                    hosts = query.findall('{http://jabber.org/protocol/bytestreams}streamhost')
                if not hosts and used is None:
                    reply = iq.reply()
                    res_q = ET.Element('{http://jabber.org/protocol/bytestreams}query', {'sid': sid})
                    local_ip = self.get_local_ip()
                    ET.SubElement(res_q, '{http://jabber.org/protocol/bytestreams}streamhost', host=local_ip, port=str(SOCKS5_PORT), jid=self.bot.boundjid.full)
                    if SOCKS5_IP and SOCKS5_IP != local_ip:
                         ET.SubElement(res_q, '{http://jabber.org/protocol/bytestreams}streamhost', host=SOCKS5_IP, port=str(SOCKS5_PORT), jid=self.bot.boundjid.full)
                    for p_jid, p_info in self.proxies.items():
                        ET.SubElement(res_q, '{http://jabber.org/protocol/bytestreams}streamhost', host=p_info['host'], port=str(p_info['port']), jid=p_jid)
                        reply.append(res_q); self.send_iq(reply); return

            file_info = self.bot.pending_files.get(sid)
            if not file_info: return

            if not jingle_sid:
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
                        reply.append(res_j); self.send_iq(reply)
                    else:
                        reply = iq.reply()
                        if used is None:
                            res_q = ET.Element('{http://jabber.org/protocol/bytestreams}query', {'sid': sid})
                            ET.SubElement(res_q, '{http://jabber.org/protocol/bytestreams}streamhost-used', jid=host.get('jid'))
                            reply.append(res_q)
                        self.send_iq(reply)
                    logging.info(f"S5B: SUCCESS connect to {host.get('host')}:{host.get('port')} for sid={sid}")
                    file_info['downloading'] = True
                    await self.download_file_task(reader, file_info, iq['from'], sid, writer=writer)
                    writer.close(); await writer.wait_closed(); return
                except Exception as e:
                    logging.info(f"S5B: Failed connect to {host.get('host')} for sid={sid}: {e}")
                    continue
            if not jingle_sid: self.send_iq(iq.error('service-unavailable'))
            else:
                if sid in self.bot.pending_files: del self.bot.pending_files[sid]
                # Jingle: we should abort session if no candidates work and no replacement
                term_iq = self.bot.make_iq_set(ito=iq['from'])
                res_j = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-terminate', 'sid': jingle_sid, 'initiator': iq['from'].full})
                reason = ET.SubElement(res_j, '{urn:xmpp:jingle:1}reason')
                ET.SubElement(reason, '{urn:xmpp:jingle:1}connectivity-error')
                term_iq.append(res_j); self.send_iq(term_iq)
        except Exception as e: logging.error(f"SOCKS5 ERROR: {e}")

    async def discover_proxies(self, event):
        logging.info("S5B: Starting manual proxy discovery via XEP-0030...")
        try:
            # Step 1: Query server for its features
            server = self.bot.boundjid.domain
            info = await self.bot['xep_0030'].get_info(jid=server)
            if 'http://jabber.org/protocol/bytestreams' in info['features']:
                 # Server itself might be a proxy
                 await self._check_and_add_proxy(server)

            # Step 2: Query server for its items (components/proxies)
            items = await self.bot['xep_0030'].get_items(jid=server)
            for item in items['disco_items']['items']:
                jid = item[0]
                try:
                    info = await self.bot['xep_0030'].get_info(jid=jid)
                    if 'http://jabber.org/protocol/bytestreams' in info['features']:
                         await self._check_and_add_proxy(jid)
                except Exception as e:
                    logging.debug(f"S5B: Failed disco info for {jid}: {e}")
        except Exception as e:
            logging.error(f"S5B: Manual proxy discovery error: {e}")

    async def _check_and_add_proxy(self, jid):
        try:
            iq = self.bot.make_iq_get(ito=jid)
            ET.SubElement(iq.xml, '{http://jabber.org/protocol/bytestreams}query')
            resp = await iq.send()
            query = resp.xml.find('{http://jabber.org/protocol/bytestreams}query')
            if query is not None:
                sh = query.find('{http://jabber.org/protocol/bytestreams}streamhost')
                if sh is not None and sh.get('host') and sh.get('port'):
                    self.proxies[jid] = {'host': sh.get('host'), 'port': int(sh.get('port'))}
                    logging.info(f"S5B: Manually discovered proxy {jid} at {sh.get('host')}:{sh.get('port')}")
        except Exception as e:
            logging.debug(f"S5B: Failed to get streamhost info from {jid}: {e}")

    async def send_file(self, peer_jid, filepath):
        if not os.path.exists(filepath):
            logging.error(f"Send file: {filepath} not found")
            return

        sid = hashlib.md5(f"{peer_jid}{filepath}{asyncio.get_event_loop().time()}".encode()).hexdigest()
        fname = os.path.basename(filepath)
        fsize = os.path.getsize(filepath)

        session = JingleSession(sid, peer_jid, self.bot.boundjid, is_initiator=True)
        session.file_info = {'name': fname, 'size': fsize, 'path': filepath}
        self.jingle_sessions[sid] = session
        self.bot.pending_files[sid] = {'name': fname, 'size': fsize, 'peer_jid': peer_jid, 'is_sending': True}

        iq = self.bot.make_iq_set(ito=peer_jid)
        jingle = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-initiate', 'sid': sid, 'initiator': self.bot.boundjid.full})
        content = ET.SubElement(jingle, '{urn:xmpp:jingle:1}content', {'creator': 'initiator', 'name': 'file-transfer'})

        description = ET.SubElement(content, '{urn:xmpp:jingle:apps:file-transfer:5}description')
        file_tag = ET.SubElement(description, '{urn:xmpp:jingle:apps:file-transfer:5}file')
        ET.SubElement(file_tag, '{urn:xmpp:jingle:apps:file-transfer:5}name').text = fname
        ET.SubElement(file_tag, '{urn:xmpp:jingle:apps:file-transfer:5}size').text = str(fsize)

        if HAS_WEBRTC:
            session.transport_type = 'ice-udp'
            # Offer ICE-UDP transport
            transport = ET.SubElement(content, '{urn:xmpp:jingle:transports:ice-udp:1}transport')
            await self._setup_webrtc_initiator(sid, peer_jid, transport)
        else:
            transport = ET.SubElement(content, '{urn:xmpp:jingle:transports:s5b:1}transport', {'sid': sid, 'mode': 'tcp'})
            local_ip = self.get_local_ip()
            ET.SubElement(transport, '{urn:xmpp:jingle:transports:s5b:1}candidate', cid='local', host=local_ip, port=str(SOCKS5_PORT), jid=self.bot.boundjid.full, priority='8253074', type='host')
            if SOCKS5_IP and SOCKS5_IP != local_ip:
                ET.SubElement(transport, '{urn:xmpp:jingle:transports:s5b:1}candidate', cid='public', host=SOCKS5_IP, port=str(SOCKS5_PORT), jid=self.bot.boundjid.full, priority='8252818', type='host')

            for p_jid, p_info in self.proxies.items():
                ET.SubElement(transport, '{urn:xmpp:jingle:transports:s5b:1}candidate', cid=hashlib.md5(p_jid.encode()).hexdigest(), host=p_info['host'], port=str(p_info['port']), jid=p_jid, priority='65536', type='proxy')

        iq.append(jingle)
        self.send_iq(iq)
        logging.info(f"JINGLE INITIATE: sid={sid}, to={peer_jid}, file={fname}")

    async def download_file_task(self, reader, file_info, peer_jid, sid, writer=None):
        if file_info.get('is_sending'):
            await self._upload_file_task(writer, file_info, peer_jid, sid)
            return

        session = self.jingle_sessions.get(sid)
        if session and session.transport_type == 'ice-udp':
             await self._webrtc_download_task(session, file_info, peer_jid, sid)
             return

        logging.info(f"DOWNLOAD START: sid={sid}, peer={peer_jid}, file={file_info['name']}, size={file_info['size']}")
        user_dir, user_hash = self.bot.get_user_info(peer_jid)
        path = get_unique_path(os.path.join(user_dir, os.path.basename(file_info['name'])))
        part_path = path + ".part"
        received, loop = 0, asyncio.get_event_loop()
        try:
            with open(part_path, 'wb') as f:
                while received < file_info['size']:
                    try:
                        chunk = await asyncio.wait_for(reader.read(min(file_info['size'] - received, 1048576)), timeout=60)
                        if not chunk: break
                        await loop.run_in_executor(None, f.write, chunk)
                        received += len(chunk)
                        logging.debug(f"DOWNLOAD sid={sid}: Received {len(chunk)} bytes. Total: {received}/{file_info['size']}")
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
                self.bot.event('jingle_done', {'sid': sid, 'peer_jid': peer_jid, 'file_info': file_info})
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

    async def _setup_webrtc_initiator(self, sid, peer_jid, transport_xml):
        if not HAS_WEBRTC: return
        session = self.jingle_sessions.get(sid)
        if not session: return

        config = self._get_rtc_config()
        pc = RTCPeerConnection(configuration=config)
        session.webrtc_pc = pc

        dc = pc.createDataChannel("file-transfer")
        session.webrtc_dc = dc

        # Handle local candidates
        @pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                iq = self.bot.make_iq_set(ito=peer_jid)
                j_xml = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-info', 'sid': sid, 'initiator': self.bot.boundjid.full})
                c_xml = ET.SubElement(j_xml, '{urn:xmpp:jingle:1}content', {'creator': 'initiator', 'name': 'file-transfer'})
                t_xml = ET.SubElement(c_xml, '{urn:xmpp:jingle:transports:ice-udp:1}transport', {'ufrag': session.ufrag, 'pwd': session.pwd})
                ET.SubElement(t_xml, '{urn:xmpp:jingle:transports:ice-udp:1}candidate', self._rtc_to_jingle_candidate(candidate))
                iq.append(j_xml); self.send_iq(iq)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Extract local ufrag/pwd
        sdp_str = pc.localDescription.sdp
        for line in sdp_str.split('\n'):
            line = line.strip()
            if line.startswith('a=ice-ufrag:'): session.ufrag = line.split(':', 1)[1]
            elif line.startswith('a=ice-pwd:'): session.pwd = line.split(':', 1)[1]

        transport_xml.set('ufrag', session.ufrag)
        transport_xml.set('pwd', session.pwd)

    async def _setup_webrtc_responder(self, sid, peer_jid, ice_t):
        if not HAS_WEBRTC: return
        session = self.jingle_sessions.get(sid)
        if not session: return

        config = self._get_rtc_config()
        pc = RTCPeerConnection(configuration=config)
        session.webrtc_pc = pc

        @pc.on("datachannel")
        def on_datachannel(channel):
            logging.info(f"WebRTC: DataChannel received: {channel.label}")
            session.webrtc_dc = channel
            @channel.on("message")
            def on_message(message):
                if not hasattr(session, 'dc_buffer'): session.dc_buffer = asyncio.Queue()
                session.dc_buffer.put_nowait(message)

        @pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                iq = self.bot.make_iq_set(ito=peer_jid)
                j_xml = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-info', 'sid': sid, 'initiator': peer_jid.full})
                c_xml = ET.SubElement(j_xml, '{urn:xmpp:jingle:1}content', {'creator': 'initiator', 'name': 'file-transfer'})
                t_xml = ET.SubElement(c_xml, '{urn:xmpp:jingle:transports:ice-udp:1}transport', {'ufrag': session.ufrag, 'pwd': session.pwd})
                ET.SubElement(t_xml, '{urn:xmpp:jingle:transports:ice-udp:1}candidate', self._rtc_to_jingle_candidate(candidate))
                iq.append(j_xml); self.send_iq(iq)

        # Trigger ICE gathering
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        sdp_str = pc.localDescription.sdp
        for line in sdp_str.split('\n'):
            line = line.strip()
            if line.startswith('a=ice-ufrag:'): session.ufrag = line.split(':', 1)[1]
            elif line.startswith('a=ice-pwd:'): session.pwd = line.split(':', 1)[1]

        logging.info(f"WebRTC: Initialized responder for sid={sid}, ufrag={session.ufrag}")

    async def _webrtc_download_task(self, session, file_info, peer_jid, sid):
        logging.info(f"WebRTC DOWNLOAD START: sid={sid}, peer={peer_jid}")
        user_dir, user_hash = self.bot.get_user_info(peer_jid)
        path = get_unique_path(os.path.join(user_dir, os.path.basename(file_info['name'])))
        part_path = path + ".part"
        received, loop = 0, asyncio.get_event_loop()

        # Wait for data channel to be ready and receive data
        if not hasattr(session, 'dc_buffer'): session.dc_buffer = asyncio.Queue()

        try:
            with open(part_path, 'wb') as f:
                while received < file_info['size']:
                    try:
                        chunk = await asyncio.wait_for(session.dc_buffer.get(), timeout=60)
                        if not chunk: break
                        await loop.run_in_executor(None, f.write, chunk)
                        received += len(chunk)
                        logging.debug(f"WebRTC DOWNLOAD sid={sid}: Received {len(chunk)} bytes. Total: {received}/{file_info['size']}")
                    except asyncio.TimeoutError:
                        logging.error(f"WebRTC TIMEOUT: sid={sid}")
                        raise
                await loop.run_in_executor(None, f.flush); await loop.run_in_executor(None, os.fsync, f.fileno())

            if received == file_info['size']:
                os.rename(part_path, path)
                self.bot.send_message(mto=peer_jid, mbody=f"✅ Готово!\n{self.bot.base_url}/{user_hash}/{safe_quote(os.path.basename(path))}", mtype='chat')
                # signaling success
                self.bot.event('jingle_done', {'sid': sid, 'peer_jid': peer_jid, 'file_info': file_info})
            else:
                if os.path.exists(part_path): os.remove(part_path)
        except Exception as e:
            logging.error(f"WebRTC DOWNLOAD ERROR: {e}")
            if os.path.exists(part_path): os.remove(part_path)
        finally:
             if session.webrtc_pc: await session.webrtc_pc.close()
             if sid in self.bot.pending_files: del self.bot.pending_files[sid]
             if sid in self.jingle_sessions: del self.jingle_sessions[sid]

    async def _upload_file_task(self, writer, file_info, peer_jid, sid):
        logging.info(f"UPLOAD START: sid={sid}, peer={peer_jid}, file={file_info['name']}, size={file_info['size']}")

        session = self.jingle_sessions.get(sid)
        if session and session.transport_type == 'ice-udp':
             await self._webrtc_upload_task(session, file_info, peer_jid, sid)
             return

        filepath = file_info.get('path')
        if not filepath or not os.path.exists(filepath):
            logging.error(f"Upload: {filepath} not found")
            return

        loop = asyncio.get_event_loop()
        try:
            with open(filepath, 'rb') as f:
                sent = 0
                while sent < file_info['size']:
                    chunk = await loop.run_in_executor(None, f.read, 65536)
                    if not chunk: break
                    writer.write(chunk)
                    await writer.drain()
                    sent += len(chunk)
                    if sid in self.bot.pending_files: self.bot.pending_files[sid]['timestamp'] = loop.time()

            logging.info(f"UPLOAD COMPLETE: sid={sid}, sent={sent}")
        except Exception as e:
            logging.error(f"UPLOAD ERROR: sid={sid}, error={e}")
        finally:
            if sid in self.bot.pending_files: del self.bot.pending_files[sid]
            if sid in self.jingle_sessions: del self.jingle_sessions[sid]
            try:
                writer.close()
                await writer.wait_closed()
            except: pass

    async def _webrtc_upload_task(self, session, file_info, peer_jid, sid):
        logging.info(f"WebRTC UPLOAD START: sid={sid}, peer={peer_jid}")
        filepath = file_info.get('path')
        if not filepath or not os.path.exists(filepath): return

        # Wait for data channel to be open
        while session.webrtc_dc is None or session.webrtc_dc.readyState != "open":
            await asyncio.sleep(0.5)
            if not session.webrtc_pc or session.webrtc_pc.connectionState in ["failed", "closed"]: return

        loop = asyncio.get_event_loop()
        try:
            with open(filepath, 'rb') as f:
                sent = 0
                while sent < file_info['size']:
                    chunk = await loop.run_in_executor(None, f.read, 16384)
                    if not chunk: break

                    # aiortc datachannel has a bufferedAmount property and can be slow
                    while session.webrtc_dc.bufferedAmount > 1024 * 1024:
                         await asyncio.sleep(0.1)

                    session.webrtc_dc.send(chunk)
                    sent += len(chunk)
                    if sid in self.bot.pending_files: self.bot.pending_files[sid]['timestamp'] = loop.time()

            logging.info(f"WebRTC UPLOAD COMPLETE: sid={sid}, sent={sent}")
            # signaling success
            self.bot.event('jingle_done', {'sid': sid, 'peer_jid': peer_jid, 'file_info': file_info})
        except Exception as e:
            logging.error(f"WebRTC UPLOAD ERROR: {e}")
        finally:
             if session.webrtc_pc: await session.webrtc_pc.close()
             if sid in self.bot.pending_files: del self.bot.pending_files[sid]
             if sid in self.jingle_sessions: del self.jingle_sessions[sid]
