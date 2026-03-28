
import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock
from plugins.file_transfer import FileTransferPlugin
from slixmpp.xmlstream import ET
import pytest

@pytest.mark.asyncio
async def test_proxy_discovery():
    logging.basicConfig(level=logging.DEBUG)

    bot = MagicMock()
    bot.boundjid.domain = 'server.com'

    xep_0030 = MagicMock()
    xep_0030.get_info = AsyncMock()
    xep_0030.get_items = AsyncMock()

    bot.__getitem__.side_effect = lambda key: xep_0030 if key == 'xep_0030' else MagicMock()

    # Mock disco info
    info_server = MagicMock()
    info_server.__getitem__.side_effect = lambda key: ['http://jabber.org/protocol/bytestreams'] if key == 'features' else []

    info_proxy1 = MagicMock()
    info_proxy1.__getitem__.side_effect = lambda key: ['http://jabber.org/protocol/bytestreams'] if key == 'features' else []

    info_proxy2 = MagicMock()
    info_proxy2.__getitem__.side_effect = lambda key: ['http://jabber.org/protocol/bytestreams'] if key == 'features' else []

    xep_0030.get_info.side_effect = [info_server, info_proxy1, info_proxy2]

    # Mock disco items
    items = MagicMock()
    items.__getitem__.side_effect = lambda key: {'items': [('proxy1@server.com', '', ''), ('proxy2@server.com', '', '')]} if key == 'disco_items' else {}
    xep_0030.get_items.return_value = items

    plugin = FileTransferPlugin(bot)
    plugin.proxies = {}

    def make_mock_iq(ito):
        iq = MagicMock()
        iq.xml = ET.Element('iq')

        resp = MagicMock()
        query = ET.Element('{http://jabber.org/protocol/bytestreams}query')
        if ito == 'server.com':
            ET.SubElement(query, '{http://jabber.org/protocol/bytestreams}streamhost', host='1.1.1.1', port='1080')
        elif ito == 'proxy1@server.com':
            ET.SubElement(query, '{http://jabber.org/protocol/bytestreams}streamhost', host='2.2.2.2', port='1081')
        elif ito == 'proxy2@server.com':
            ET.SubElement(query, '{http://jabber.org/protocol/bytestreams}streamhost', host='3.3.3.3', port='')

        resp.xml = ET.Element('iq')
        resp.xml.append(query)

        # The plugin code does resp.xml.find('{http://jabber.org/protocol/bytestreams}query')
        # We need to make sure this works. ET.Element.find should work.

        iq.send = AsyncMock(return_value=resp)
        return iq

    bot.make_iq_get.side_effect = make_mock_iq

    await plugin.discover_proxies(None)

    print(f"Discovered proxies: {plugin.proxies}")
    assert 'server.com' in plugin.proxies
    assert plugin.proxies['server.com'] == {'host': '1.1.1.1', 'port': 1080}
    assert 'proxy1@server.com' in plugin.proxies
    assert plugin.proxies['proxy1@server.com'] == {'host': '2.2.2.2', 'port': 1081}
    assert 'proxy2@server.com' not in plugin.proxies
    print("Manual proxy discovery test PASSED")

if __name__ == "__main__":
    asyncio.run(test_proxy_discovery())
