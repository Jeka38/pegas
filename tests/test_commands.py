import os
import pytest
from plugins.commands import CommandsPlugin
from slixmpp import JID

class MockMessage(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.xml = MockXML()

class MockXML:
    def find(self, ns): return None

class MockBot:
    def __init__(self, dest_dir):
        self.dest_dir = dest_dir
        self.base_url = 'http://example.com'
        self.db = MockDB()
        self.file_transfer = None

    def add_event_handler(self, name, handler):
        pass

    def is_allowed(self, jid):
        return True

    def get_user_info(self, jid):
        user_dir = os.path.join(self.dest_dir, 'user_hash')
        if not os.path.exists(user_dir):
            os.makedirs(user_dir)
        return user_dir, 'user_hash'

    def get_help_text(self, is_admin, user_hash):
        return "help"

class MockDB:
    def get_blacklist(self): return []
    def get_whitelist(self): return ['*']

@pytest.fixture
def bot_and_plugin(tmp_path):
    bot = MockBot(str(tmp_path))
    plugin = CommandsPlugin(bot)

    # Create some test files
    user_dir, _ = bot.get_user_info(JID('user@example.com'))
    # Sorted order should be: dir1/, dir1/file3.txt, file1.txt, file2.jpg
    os.makedirs(os.path.join(user_dir, 'dir1'))
    with open(os.path.join(user_dir, 'dir1/file3.txt'), 'w') as f: f.write('content3')
    with open(os.path.join(user_dir, 'file1.txt'), 'w') as f: f.write('content1')
    with open(os.path.join(user_dir, 'file2.jpg'), 'w') as f: f.write('content2')

    return bot, plugin

def test_ls_filtering(bot_and_plugin):
    bot, plugin = bot_and_plugin
    msg = MockMessage({'type': 'chat', 'from': JID('user@example.com'), 'body': 'ls file1.txt'})

    replies = []
    def mock_reply(m, body):
        replies.append(body)
    plugin.reply = mock_reply

    plugin.handle_message(msg)

    assert len(replies) == 1
    assert 'file1.txt' in replies[0]
    assert 'file2.jpg' not in replies[0]
    assert '3 - file1.txt' in replies[0]

def test_lss_filtering_index(bot_and_plugin):
    bot, plugin = bot_and_plugin
    msg = MockMessage({'type': 'chat', 'from': JID('user@example.com'), 'body': 'lss 3'})

    replies = []
    def mock_reply(m, body):
        replies.append(body)
    plugin.reply = mock_reply

    plugin.handle_message(msg)

    assert len(replies) == 1
    assert '3 - file1.txt' in replies[0]
    assert 'file2.jpg' not in replies[0]

def test_ls_pattern(bot_and_plugin):
    bot, plugin = bot_and_plugin
    msg = MockMessage({'type': 'chat', 'from': JID('user@example.com'), 'body': 'ls *.jpg'})

    replies = []
    def mock_reply(m, body):
        replies.append(body)
    plugin.reply = mock_reply

    plugin.handle_message(msg)

    assert len(replies) == 1
    assert 'file2.jpg' in replies[0]
    assert 'file1.txt' not in replies[0]
    assert '4 - file2.jpg' in replies[0]

def test_ls_no_match(bot_and_plugin):
    bot, plugin = bot_and_plugin
    msg = MockMessage({'type': 'chat', 'from': JID('user@example.com'), 'body': 'ls non-existent'})

    replies = []
    def mock_reply(m, body):
        replies.append(body)
    plugin.reply = mock_reply

    plugin.handle_message(msg)

    assert len(replies) == 1
    assert "ничего не найдено" in replies[0]
