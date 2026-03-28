
import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock
from plugins.file_transfer import FileTransferPlugin

async def test_proxy_discovery():
    logging.basicConfig(level=logging.INFO)

    # Mock bot and xep_0065 plugin
    bot = MagicMock()
    xep_0065 = AsyncMock()
    bot.__getitem__.side_effect = lambda key: xep_0065 if key == 'xep_0065' else MagicMock()

    # Configure mock behavior
    xep_0065.discover_proxies.return_value = ['proxy1@server.com', 'proxy2@server.com', 'proxy_bad@server.com']
    xep_0065.get_network_address.side_effect = [
        {'host': '1.1.1.1', 'port': '1080'},
        {'host': '2.2.2.2', 'port': '1081'},
        {'host': '3.3.3.3', 'port': ''}  # Test empty port
    ]

    # Initialize plugin
    plugin = FileTransferPlugin(bot)

    # Clear initial proxies for clean test
    plugin.proxies = {}

    # Run discovery
    await plugin.discover_proxies(None)

    # Verify results
    print(f"Discovered proxies: {plugin.proxies}")
    assert 'proxy1@server.com' in plugin.proxies
    assert plugin.proxies['proxy1@server.com'] == {'host': '1.1.1.1', 'port': 1080}
    assert 'proxy2@server.com' in plugin.proxies
    assert plugin.proxies['proxy2@server.com'] == {'host': '2.2.2.2', 'port': 1081}
    print("Proxy discovery test PASSED")

if __name__ == "__main__":
    asyncio.run(test_proxy_discovery())
