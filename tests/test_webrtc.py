
import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock, patch
from plugins.file_transfer import FileTransferPlugin, JingleSession
from slixmpp.xmlstream import ET
import pytest

@pytest.mark.asyncio
async def test_webrtc_integration():
    logging.basicConfig(level=logging.DEBUG)

    bot = MagicMock()
    bot.boundjid.full = 'bot@server.com/bot'
    bot.is_allowed.return_value = True

    # Mock user info
    bot.get_user_info.return_value = ('/tmp/user', 'hash')

    # Mock plugins
    bot.__getitem__.return_value = MagicMock()

    plugin = FileTransferPlugin(bot)

    # Simulate session initiate with ice-udp
    sid = 'webrtc-test'
    peer_jid = MagicMock()
    peer_jid.full = 'user@server.com/client'
    peer_jid.bare = 'user@server.com'

    ft_ns = 'urn:xmpp:jingle:apps:file-transfer:5'
    jingle = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'session-initiate', 'sid': sid})
    content = ET.SubElement(jingle, '{urn:xmpp:jingle:1}content', {'creator': 'initiator', 'name': 'file-transfer'})
    desc = ET.SubElement(content, f'{{{ft_ns}}}description')
    file_tag = ET.SubElement(desc, f'{{{ft_ns}}}file')
    ET.SubElement(file_tag, f'{{{ft_ns}}}name').text = 'test.txt'
    ET.SubElement(file_tag, f'{{{ft_ns}}}size').text = '100'
    ET.SubElement(content, '{urn:xmpp:jingle:transports:ice-udp:1}transport')

    iq = MagicMock()
    iq['from'] = peer_jid
    iq['type'] = 'set'
    iq.xml = ET.Element('iq')
    iq.xml.append(jingle)

    with patch('plugins.file_transfer.HAS_WEBRTC', True), \
         patch('plugins.file_transfer.RTCPeerConnection') as mock_pc_cls, \
         patch('plugins.file_transfer.RTCIceCandidate') as mock_ice_cand:

        mock_pc = MagicMock()
        mock_pc.addIceCandidate = AsyncMock()
        mock_pc_cls.return_value = mock_pc

        # Mock bot.make_iq_set
        bot.make_iq_set.return_value = MagicMock()

        # Handle Jingle session initiate
        plugin.handle_jingle(iq)

        assert sid in plugin.jingle_sessions
        session = plugin.jingle_sessions[sid]
        assert session.transport_type == 'ice-udp'
        print("WebRTC session initialization test PASSED")

        # Test transport-info with candidates
        info_jingle = ET.Element('{urn:xmpp:jingle:1}jingle', {'action': 'transport-info', 'sid': sid})
        info_content = ET.SubElement(info_jingle, '{urn:xmpp:jingle:1}content', {'creator': 'initiator', 'name': 'file-transfer'})
        info_transport = ET.SubElement(info_content, '{urn:xmpp:jingle:transports:ice-udp:1}transport')
        ET.SubElement(info_transport, '{urn:xmpp:jingle:transports:ice-udp:1}candidate', {
            'component': '1', 'foundation': '1', 'ip': '1.2.3.4', 'port': '5678',
            'priority': '100', 'protocol': 'udp', 'type': 'host'
        })

        info_iq = MagicMock()
        info_iq['from'] = peer_jid
        info_iq['type'] = 'set'
        info_iq.xml = ET.Element('iq')
        info_iq.xml.append(info_jingle)

        # Force pc into session manually for test if initialization was delayed
        session.webrtc_pc = mock_pc

        plugin.handle_jingle(info_iq)

        # Verify addIceCandidate was called (it's called in an async task)
        await asyncio.sleep(0.1)
        assert mock_pc.addIceCandidate.called
        print("WebRTC ICE candidate signaling test PASSED")

if __name__ == "__main__":
    asyncio.run(test_webrtc_integration())
