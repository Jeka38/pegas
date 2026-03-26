const { xml } = require('@xmpp/client');
const BasePlugin = require('./base');
const config = require('../config');

class PresencePlugin extends BasePlugin {
    constructor(bot) {
        super(bot);
        this.bot.xmpp.on('online', address => this.start(address));
        this.bot.xmpp.on('stanza', stanza => {
            if (stanza.is('presence')) {
                this.handlePresence(stanza);
            } else if (stanza.is('iq') && stanza.getChild('query', 'http://jabber.org/protocol/disco#info')) {
                this.handleDiscoInfo(stanza);
            }
        });
    }

    handleDiscoInfo(iq) {
        if (iq.attrs.type !== 'get') return;
        const reply = xml('iq', { to: iq.attrs.from, id: iq.attrs.id, type: 'result' },
            xml('query', { xmlns: 'http://jabber.org/protocol/disco#info' },
                xml('identity', { category: 'client', type: 'bot', name: config.APP_NAME }),
                ...this.features.map(f => xml('feature', { var: f }))
            )
        );
        this.bot.xmpp.send(reply);
    }

    async start(address) {
        this.features = [
            'http://jabber.org/protocol/si',
            'http://jabber.org/protocol/bytestreams',
            'http://jabber.org/protocol/ibb',
            'http://jabber.org/protocol/si/profile/file-transfer',
            'urn:xmpp:jingle:1',
            'urn:xmpp:jingle:apps:file-transfer:4',
            'urn:xmpp:jingle:apps:file-transfer:5',
            'urn:xmpp:jingle:transports:s5b:1',
            'urn:xmpp:jingle:transports:ibb:1',
            'urn:xmpp:jingle:transports:ice-udp:1',
            'jabber:iq:oob',
            'jabber:x:oob'
        ];

        // Service Discovery is usually handled by a separate plugin or manually
        // For simplicity, we just send presence here.
        // In a full implementation, you'd want to handle disco#info IQs.

        this.bot.xmpp.send(xml('presence', {},
            xml('status', {}, config.STATUS_MESSAGE),
            xml('c', { xmlns: 'http://jabber.org/protocol/caps', hash: 'sha-1', node: 'http://obbfastbot.js', ver: 'node-bot-1.0' })
        ));

        console.log(`✅ БОТ ЗАПУЩЕН: ${address.toString()}`);
    }

    handlePresence(presence) {
        const from = presence.attrs.from;
        const type = presence.attrs.type;

        if (type === 'subscribe') {
            console.log(`🆕 Запрос подписки от ${from}`);
            if (!this.bot.isAllowed(from)) {
                console.log(`ACCESS DENIED (subscribe) from ${from}`);
                this.bot.sendMessage(from, `⚠️ Доступ запрещён. Пожалуйста, обратитесь к администратору для получения доступа: ${config.ADMIN_JID}`);
                return;
            }
            this.bot.xmpp.send(xml('presence', { to: from, type: 'subscribed' }));
            this.bot.xmpp.send(xml('presence', { to: from, type: 'subscribe' }));

            const isAdmin = config.ADMIN_JID && from.split('/')[0].toLowerCase() === config.ADMIN_JID.toLowerCase();
            const { userHash } = this.bot.getUserInfo(from);
            const welcomeMsg = `Добро пожаловать!\nЯ бот для быстрой передачи файлов.\n\n${this.bot.getHelpText(isAdmin, userHash)}`;
            this.bot.sendMessage(from, welcomeMsg);
        } else if (type === 'subscribed') {
            console.log(`✅ Подписка подтверждена от ${from}`);
            if (config.ADMIN_JID) {
                this.bot.sendMessage(config.ADMIN_JID, `✅ Пользователь ${from} добавил бота в контакты`);
            }
        } else if (type === 'unsubscribe') {
            console.log(`➖ Запрос отписки от ${from}`);
            if (config.ADMIN_JID) {
                this.bot.sendMessage(config.ADMIN_JID, `➖ Пользователь ${from} удалил бота из контактов`);
            }
        } else if (type === 'unsubscribed') {
            console.log(`❌ Подписка отменена от ${from}`);
        }
    }
}

module.exports = PresencePlugin;
