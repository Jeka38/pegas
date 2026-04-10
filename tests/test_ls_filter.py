import os
import pytest
from unittest.mock import MagicMock
from plugins.commands import CommandsPlugin
from utils import get_all_items

class MockBot:
    def __init__(self):
        self.base_url = "http://test"
        self.file_transfer = MagicMock()
        self.is_allowed = MagicMock(return_value=True)
        self.get_user_info = MagicMock(return_value=("/tmp/testuser", "userhash"))
        self.get_help_text = MagicMock(return_value="Help")
        self.add_event_handler = MagicMock()
        self.db = MagicMock()

@pytest.fixture
def mock_user_dir(tmp_path):
    user_dir = tmp_path / "testuser"
    user_dir.mkdir()
    (user_dir / "file1.txt").write_text("content1")
    (user_dir / "IMAGE.JPG").write_text("content2")
    (user_dir / "notes.doc").write_text("content3")
    (user_dir / "subdir").mkdir()
    (user_dir / "subdir" / "data.txt").write_text("content4")
    return user_dir

@pytest.mark.asyncio
async def test_ls_filter(mock_user_dir):
    bot = MockBot()
    bot.get_user_info = MagicMock(return_value=(str(mock_user_dir), "userhash"))
    plugin = CommandsPlugin(bot)
    plugin.reply = MagicMock()

    # Get all items to know the indices
    items = get_all_items(str(mock_user_dir))
    # items should be: ['IMAGE.JPG', 'file1.txt', 'notes.doc', 'subdir/', 'subdir/data.txt']

    # 1. Test filtering by index
    msg = MagicMock()
    msg.__getitem__.side_effect = lambda key: {'type': 'chat', 'from': MagicMock(), 'body': 'ls 2'}.get(key)
    msg['from'].bare = "user@test"
    msg.xml.find.return_value = None
    plugin.handle_message(msg)

    reply_call = plugin.reply.call_args[0][1]
    assert "2 - file1.txt" in reply_call
    assert "1 - IMAGE.JPG" not in reply_call
    assert "3 - notes.doc" not in reply_call

    # 2. Test filtering by case-insensitive pattern
    plugin.reply.reset_mock()
    msg.__getitem__.side_effect = lambda key: {'type': 'chat', 'from': MagicMock(), 'body': 'ls *.jpg'}.get(key)
    plugin.handle_message(msg)
    reply_call = plugin.reply.call_args[0][1]
    assert "1 - IMAGE.JPG" in reply_call
    assert "file1.txt" not in reply_call

    # 3. Test lss with filter
    plugin.reply.reset_mock()
    msg.__getitem__.side_effect = lambda key: {'type': 'chat', 'from': MagicMock(), 'body': 'lss 1,3'}.get(key)
    plugin.handle_message(msg)
    reply_call = plugin.reply.call_args[0][1]
    assert "1 - IMAGE.JPG" in reply_call
    assert "3 - notes.doc" in reply_call
    assert "2 - file1.txt" not in reply_call
    assert "[8,0 Б]" in reply_call # size of content

    # 4. Test nothing found
    plugin.reply.reset_mock()
    msg.__getitem__.side_effect = lambda key: {'type': 'chat', 'from': MagicMock(), 'body': 'ls nonexistent*'}.get(key)
    plugin.handle_message(msg)
    reply_call = plugin.reply.call_args[0][1]
    assert "Ничего не найдено" in reply_call

    # 5. Test ls -s with filter
    plugin.reply.reset_mock()
    msg.__getitem__.side_effect = lambda key: {'type': 'chat', 'from': MagicMock(), 'body': 'ls -s *.txt'}.get(key)
    plugin.handle_message(msg)
    reply_call = plugin.reply.call_args[0][1]
    assert "file1.txt" in reply_call
    assert "data.txt" in reply_call
    assert "IMAGE.JPG" not in reply_call
