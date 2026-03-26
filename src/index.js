const OBBFastBot = require('./bot');
const config = require('./config');

async function main() {
    const bot = new OBBFastBot(
        config.XMPP_JID,
        config.XMPP_PASSWORD,
        config.DOWNLOAD_DIR
    );

    try {
        await bot.start();
    } catch (err) {
        console.error('Failed to start bot:', err);
    }
}

main();
