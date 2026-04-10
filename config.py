import os
from dotenv import load_dotenv

load_dotenv()

# Disk quota
QUOTA_LIMIT_GB = int(os.getenv('QUOTA_GB', 15))
QUOTA_LIMIT_BYTES = QUOTA_LIMIT_GB * 1024 * 1024 * 1024

# Administrative settings
ADMIN_JID = os.getenv('ADMIN_JID')
ADMIN_NOTIFY_LEVEL = os.getenv('ADMIN_NOTIFY_LEVEL', 'all').lower()

# Database and persistence
DB_PATH = os.getenv('DB_PATH', '/app/data/bot.db')

# Filesystem settings
MAX_DIR_DEPTH = int(os.getenv('MAX_DIR_DEPTH', 2))
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR')

# SOCKS5 settings
SOCKS5_PORT = int(os.getenv('SOCKS5_PORT', 1080))
SOCKS5_IP = os.getenv('SOCKS5_IP')  # Public IP/domain for S5B candidates

# XMPP account settings
XMPP_JID = os.getenv('XMPP_JID')
XMPP_RESOURCE = os.getenv('XMPP_RESOURCE')
XMPP_PASSWORD = os.getenv('XMPP_PASSWORD')
XMPP_HOST = os.getenv('XMPP_HOST', 'jabberworld.info')
XMPP_PORT = int(os.getenv('XMPP_PORT', 5222))

# Bot appearance and metadata
APP_NAME = os.getenv('APP_NAME', 'OBBFastBot')
VERSION = os.getenv('APP_VERSION', '1.1')
STATUS_MESSAGE = os.getenv('STATUS_MESSAGE', 'Для помощи по боту напиши ? или help')
BASE_URL = (os.getenv('BASE_URL') or "").rstrip('/')

# SOCKS5 Proxies for file transfer
SOCKS5_PROXIES = [
    {'host': 'proxy.eu.jabber.network', 'port': 1080, 'jid': 'proxy.eu.jabber.network'},
    {'host': 'proxy.jabber.ru', 'port': 1080, 'jid': 'proxy.jabber.ru'},
    {'host': 'proxy.jabbim.cz', 'port': 1080, 'jid': 'proxy.jabbim.cz'},
    {'host': 'proxy.yax.im', 'port': 1080, 'jid': 'proxy.yax.im'},
]
