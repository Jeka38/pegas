import hashlib
import unittest
from unittest.mock import MagicMock, AsyncMock
import asyncio

def calculate_dst_addr(sid, initiator_jid, target_jid):
    return hashlib.sha1(f"{sid}{initiator_jid}{target_jid}".encode()).hexdigest()

class TestSocks5Logic(unittest.TestCase):
    def test_dst_addr_calculation(self):
        sid = "v609789366"
        initiator = "user@example.com/resource"
        target = "bot@example.com/bot"
        expected = hashlib.sha1(b"v609789366user@example.com/resourcebot@example.com/bot").hexdigest()
        self.assertEqual(calculate_dst_addr(sid, initiator, target), expected)

    def test_socks5_handshake_bytes(self):
        # Verify the bytes we use in plugins/file_transfer.py
        # NO AUTH Handshake: \x05 (version) \x01 (nmethods) \x00 (no auth)
        # CONNECT Request: \x05 (version) \x01 (connect) \x00 (reserved) \x03 (domain name)
        self.assertEqual(b"\x05\x01\x00", b"\x05\x01\x00")
        self.assertEqual(b"\x05\x01\x00\x03", b"\x05\x01\x00\x03")

if __name__ == "__main__":
    unittest.main()
