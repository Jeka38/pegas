const { client, xml } = require('@xmpp/client');
const debug = require('@xmpp/debug');
const crypto = require('crypto');
const fs = require('fs-extra');
const path = require('path');
const DB = require('./database');
const config = require('./config');
const PresencePlugin = require('./plugins/presence');
const FileTransferPlugin = require('./plugins/file-transfer');
const CommandsPlugin = require('./plugins/commands');

class OBBFastBot {
    constructor(jid, password, destDir) {
        this.jid = jid;
        this.destDir = destDir;
        this.baseUrl = config.BASE_URL;
        this.pendingFiles = {};

        this.xmpp = client({
            service: `xmpp://${config.XMPP_HOST}:${config.XMPP_PORT}`,
            domain: jid.split('@')[1],
            resource: config.XMPP_RESOURCE,
            username: jid.split('@')[0],
            password: password,
        });

        // debug(this.xmpp); // Uncomment for full XML debugging

        this.db = new DB();
        this.migrateJsonToDb();
        this.migrateFilenames();
        this.cleanupLeftoverParts();

        setInterval(() => this.cleanupPendingFiles(), 60000);

        this.presence = new PresencePlugin(this);
        this.fileTransfer = new FileTransferPlugin(this);
        this.commands = new CommandsPlugin(this);

        this.xmpp.on('error', err => {
            console.error('XMPP Error:', err.message);
        });

        this.xmpp.on('status', status => {
            console.log('XMPP Status:', status);
        });

        this.xmpp.on('stanza', async stanza => {
            if (stanza.is('iq') && stanza.getChild('ping', 'urn:xmpp:ping')) {
                this.handlePing(stanza);
            }
        });
    }

    handlePing(iq) {
        if (iq.attrs.type === 'error' || iq.attrs.type === 'result') return;
        console.log(`PING RECV from ${iq.attrs.from}`);
        const reply = xml('iq', {
            to: iq.attrs.from,
            id: iq.attrs.id,
            type: 'result'
        });
        this.xmpp.send(reply);
        console.log(`PONG SENT to ${iq.attrs.from}`);
    }

    async start() {
        await this.xmpp.start();
    }

    sendMessage(to, body) {
        const message = xml(
            'message',
            { to, type: 'chat' },
            xml('body', {}, body)
        );
        this.xmpp.send(message);
    }

    cleanupPendingFiles() {
        const now = Date.now();
        for (const sid in this.pendingFiles) {
            const info = this.pendingFiles[sid];
            if (info.timestamp && now - info.timestamp > 60000) {
                console.log(`CLEANUP: Removing pending item sid=${sid}`);
                if (info.task && typeof info.task.cancel === 'function') {
                    info.task.cancel();
                }
                delete this.pendingFiles[sid];
            }
        }
    }

    cleanupLeftoverParts() {
        console.log("START: Cleanup leftover .part files");
        let count = 0;
        const walk = (dir) => {
            const files = fs.readdirSync(dir);
            for (const file of files) {
                const fullPath = path.join(dir, file);
                if (fs.statSync(fullPath).isDirectory()) {
                    walk(fullPath);
                } else if (file.endsWith('.part')) {
                    fs.removeSync(fullPath);
                    count++;
                }
            }
        };
        if (fs.existsSync(this.destDir)) {
            walk(this.destDir);
        }
        if (count > 0) console.log(`FINISH: Removed ${count} .part files during startup`);
    }

    migrateJsonToDb() {
        const whitelistFile = config.WHITELIST_FILE;
        try {
            if (fs.existsSync(whitelistFile)) {
                if (fs.lstatSync(whitelistFile).isFile()) {
                    const data = fs.readJsonSync(whitelistFile);
                    for (const entry of data) {
                        this.db.addToWhitelist(entry);
                    }
                    console.log(`MIGRATED ${data.length} entries from ${whitelistFile} to database`);
                    fs.removeSync(whitelistFile);
                } else if (fs.lstatSync(whitelistFile).isDirectory()) {
                    fs.removeSync(whitelistFile);
                }
            }
        } catch (e) {
            console.error(`MIGRATION ERROR: ${e.message}`);
        }
    }

    isAllowed(jid) {
        if (!jid) return false;
        const bareJid = (typeof jid === 'string' ? jid : jid.toString()).split('/')[0].toLowerCase();
        const domain = bareJid.split('@')[1];

        if (config.ADMIN_JID && bareJid === config.ADMIN_JID.toLowerCase()) return true;

        const blacklist = this.db.getBlacklist();
        if (blacklist.has(bareJid) || (domain && blacklist.has(domain))) return false;

        const whitelist = this.db.getWhitelist();
        if (whitelist.has('*')) return true;
        return whitelist.has(bareJid) || (domain && whitelist.has(domain));
    }

    migrateFilenames() {
        console.log("START: Filename migration (spaces to underscores)");
        let count = 0;
        const walk = (dir) => {
            const files = fs.readdirSync(dir);
            for (const file of files) {
                const fullPath = path.join(dir, file);
                if (fs.statSync(fullPath).isDirectory()) {
                    walk(fullPath);
                } else if (file.includes(' ')) {
                    const newPath = path.join(dir, file.replace(/ /g, '_'));
                    fs.renameSync(fullPath, newPath);
                    count++;
                }
            }
        };
        if (fs.existsSync(this.destDir)) {
            walk(this.destDir);
        }
        if (count > 0) console.log(`FINISH: Renamed ${count} files during migration`);
    }

    getUserInfo(jid) {
        const bareJid = (typeof jid === 'string' ? jid : jid.toString()).split('/')[0].toLowerCase();
        let userHash = this.db.getUserFolder(bareJid);
        let isNew = false;
        if (!userHash) {
            userHash = crypto.createHash('md5').update(bareJid).digest('hex');
            this.db.setUserFolder(bareJid, userHash);
            isNew = true;
        }
        const userDir = path.join(this.destDir, userHash);
        if (!fs.existsSync(userDir)) {
            fs.ensureDirSync(userDir);
            isNew = true;
        }
        if (isNew) {
            if (config.ADMIN_JID && (config.ADMIN_NOTIFY_LEVEL === 'all' || config.ADMIN_NOTIFY_LEVEL === 'registrations')) {
                this.sendMessage(config.ADMIN_JID, `🆕 Новый пользователь: ${bareJid} (${userHash})`);
            }
        }
        return { userDir, userHash };
    }

    getHelpText(isAdmin = false, userHash = null) {
        let text = (
            "команды:\n" +
            "ls - список файлов и каталогов в папке пользователя.\n" +
            "ls <-s|-l>, lss, lsl - список файлов (-s: размер, -l: подробно). Пример: ls -l\n" +
            "mkdir <путь> - создать директорию.\n" +
            "rmdir <номер|путь> - удалить пустую директорию.\n" +
            "mv <номер|путь> <номер|путь> - переместить/переименовать.\n" +
            "rm <номер>[,<номер>],.. - удаление файлов по номеру или rm * - для удаления всех файлов.\n" +
            "link <номер>[,<номер>],.. - получение ссылок на файлы или lnk * - для всех файлов.\n" +
            "priv - сделать архив приватным (создать index.html).\n" +
            "pub - сделать архив публичным (удалить index.html).\n" +
            "album - включить режим галереи (создать index.php).\n" +
            "ping - проверить доступность бота.\n" +
            "help или ? - список команд."
        );
        if (userHash) {
            text += `\n\n📂 Ваш архив: ${this.baseUrl}/${userHash}/`;
            text += "\nЧтобы запретить просмотр списка файлов через браузер, используйте команду priv.";
        }
        if (isAdmin) {
            text += (
                "\n\n🔧 Админ-команды:\n" +
                "add <jid|domain|*> - разрешить доступ.\n" +
                "del <jid|domain|*> - запретить доступ.\n" +
                "block <jid|domain> - в чёрный список.\n" +
                "unblock <jid|domain> - убрать из чёрного списка.\n" +
                "list - показать белый и чёрный списки."
            );
        }
        return text;
    }
}

module.exports = OBBFastBot;
