class BasePlugin {
    constructor(bot) {
        this.bot = bot;
        this.db = bot.db;
    }

    reply(msg, text) {
        this.bot.sendMessage(msg.from, text);
    }
}

module.exports = BasePlugin;
