const fs = require('fs-extra');
const path = require('path');
const datetime = require('date-fns');
const BasePlugin = require('./base');
const config = require('../config');
const utils = require('../utils');

class CommandsPlugin extends BasePlugin {
    constructor(bot) {
        super(bot);
        this.bot.xmpp.on('stanza', stanza => {
            if (stanza.is('message') && (stanza.attrs.type === 'chat' || stanza.attrs.type === 'normal')) {
                this.handleMessage(stanza);
            }
        });
    }

    async handleMessage(msg) {
        const from = msg.attrs.from;
        const body = msg.getChildText('body');
        const oob = msg.getChild('x', 'jabber:x:oob');

        if (!this.bot.isAllowed(from)) {
            if (body || oob) {
                this.reply({ from }, `⚠️ Доступ запрещён. Пожалуйста, обратитесь к администратору для получения доступа: ${config.ADMIN_JID}`);
            }
            return;
        }

        const { userDir, userHash } = this.bot.getUserInfo(from);
        let cmdExecuted = false;
        const oobUrls = new Set();

        if (oob) {
            const urlEl = oob.getChild('url');
            if (urlEl && urlEl.text()) {
                const url = urlEl.text().trim();
                cmdExecuted = true;
                oobUrls.add(url);
                const desc = oob.getChildText('desc');
                const fname = desc || path.basename(url);
                this.bot.fileTransfer.downloadFromUrl(url, fname, from);
            }
        }

        if (!body) return;

        const urlRegex = /https?:\/\/(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:\/[^\s<>"]*)?/g;
        const rawUrls = body.match(urlRegex) || [];
        const cleanUrls = [];
        for (let u of rawUrls) {
            u = u.replace(/[.,!?;:]+$/, '');
            if (!oobUrls.has(u) && !cleanUrls.includes(u)) {
                cleanUrls.push(u);
            }
        }

        if (cleanUrls.length > 0) {
            cmdExecuted = true;
            for (const url of cleanUrls.slice(0, 5)) {
                this.bot.fileTransfer.downloadFromUrl(url, path.basename(url), from);
            }
        }

        const parts = body.trim().split(/\s+/);
        if (parts.length === 0) return;
        const cmd = parts[0].toLowerCase();

        if (cmd === 'help' || cmd === '?') {
            cmdExecuted = true;
            const isAdmin = config.ADMIN_JID && from.split('/')[0].toLowerCase() === config.ADMIN_JID.toLowerCase();
            const used = utils.getDirSize(userDir);
            this.reply({ from }, this.bot.getHelpText(isAdmin, userHash) + `\n\n📊 Квота: ${utils.formatSize(used)} / ${utils.formatSize(config.QUOTA_LIMIT_BYTES)}`);
        } else if (cmd === 'ping' && parts.length === 1) {
            cmdExecuted = true;
            this.reply({ from }, "pong");
        } else if (cmd === 'mkdir' && parts.length === 2) {
            cmdExecuted = true;
            const target = utils.getSafePath(userDir, parts[1]);
            if (target) {
                const rel = path.relative(userDir, target);
                if (rel !== "" && rel !== "." && rel.split(path.sep).length > config.MAX_DIR_DEPTH) {
                    this.reply({ from }, `❌ Ошибка: Максимальная глубина вложенности — ${config.MAX_DIR_DEPTH} уровня`);
                } else {
                    try {
                        fs.ensureDirSync(target);
                        this.reply({ from }, `📁 Директория создана: ${rel}`);
                    } catch (e) {
                        this.reply({ from }, `❌ Ошибка: ${e.message}`);
                    }
                }
            } else {
                this.reply({ from }, "❌ Недопустимый путь");
            }
        } else if (cmd === 'rmdir' && parts.length === 2) {
            cmdExecuted = true;
            const items = utils.getAllItems(userDir);
            const resolvedPaths = utils.resolveItemsList(userDir, parts[1], items);
            let removedCount = 0;
            for (const target of resolvedPaths) {
                if (fs.existsSync(target) && fs.statSync(target).isDirectory()) {
                    try {
                        fs.rmdirSync(target);
                        removedCount++;
                    } catch (e) {}
                }
            }
            if (removedCount > 0) this.reply({ from }, `🗑 Удалено директорий: ${removedCount}`);
            else this.reply({ from }, "❌ Директории не найдены или не пусты");
        } else if (cmd === 'mv' && parts.length === 3) {
            cmdExecuted = true;
            const items = utils.getAllItems(userDir);
            const dst = utils.resolveItem(userDir, parts[2], items);
            if (!dst) {
                this.reply({ from }, "❌ Недопустимый путь назначения");
            } else {
                const resolvedSrcs = utils.resolveItemsList(userDir, parts[1], items);
                if (resolvedSrcs.length === 0) {
                    this.reply({ from }, "❌ Объекты для перемещения не найдены");
                } else if (resolvedSrcs.length > 1) {
                    if (!fs.existsSync(dst) || !fs.statSync(dst).isDirectory()) {
                        this.reply({ from }, "❌ При перемещении нескольких объектов назначение должно быть директорией");
                    } else {
                        let movedCount = 0;
                        for (const src of resolvedSrcs) {
                            if (path.resolve(src) === path.resolve(dst)) continue;
                            const newDst = path.join(dst, path.basename(src.replace(/\/$/, '')));
                            if (utils.isPhpFile(newDst)) {
                                this.reply({ from }, `⚠️ Ошибка: Переименование в PHP-файлы запрещено (${path.basename(newDst)})`);
                                continue;
                            }
                            const relDst = path.relative(userDir, newDst);
                            const isDir = fs.statSync(src).isDirectory();
                            const limit = isDir ? config.MAX_DIR_DEPTH - 1 : config.MAX_DIR_DEPTH;
                            if (relDst !== "" && relDst !== "." && relDst.split(path.sep).length - (relDst.endsWith(path.sep) ? 1 : 0) > limit) continue;
                            try {
                                const uniqueDst = utils.getUniquePath(newDst);
                                fs.renameSync(src, uniqueDst);
                                movedCount++;
                            } catch (e) {}
                        }
                        this.reply({ from }, `🚚 Перемещено объектов: ${movedCount}`);
                    }
                } else {
                    const src = resolvedSrcs[0];
                    if (fs.existsSync(src)) {
                        try {
                            let finalDst = dst;
                            if (fs.statSync(dst).isDirectory()) {
                                finalDst = path.join(dst, path.basename(src.replace(/\/$/, '')));
                            } else {
                                if (fs.statSync(src).isFile()) {
                                    const originalExt = path.extname(src);
                                    if (!finalDst.toLowerCase().endsWith(originalExt.toLowerCase())) {
                                        finalDst += originalExt;
                                    }
                                }
                            }
                            const relDst = path.relative(userDir, finalDst);
                            const isDir = fs.statSync(src).isDirectory();
                            const limit = isDir ? config.MAX_DIR_DEPTH - 1 : config.MAX_DIR_DEPTH;
                            if (relDst !== "" && relDst !== "." && relDst.split(path.sep).length - (relDst.endsWith(path.sep) ? 1 : 0) > limit) {
                                this.reply({ from }, "❌ Ошибка: Превышена максимальная глубина вложенности");
                            } else if (utils.isPhpFile(finalDst)) {
                                this.reply({ from }, `⚠️ Ошибка: Переименование в PHP-файлы запрещено (${path.basename(finalDst)})`);
                            } else {
                                const uniqueDst = utils.getUniquePath(finalDst);
                                fs.renameSync(src, uniqueDst);
                                this.reply({ from }, `🚚 Перемещено: ${path.relative(userDir, src)} -> ${path.relative(userDir, uniqueDst)}`);
                            }
                        } catch (e) {
                            this.reply({ from }, `❌ Ошибка: ${e.message}`);
                        }
                    } else {
                        this.reply({ from }, "❌ Файл не найден");
                    }
                }
            }
        } else if ((cmd === 'ls' || cmd === 'lss' || cmd === 'lsl') && parts.length <= 2) {
            let mode = 'links';
            if (cmd === 'lss') mode = 'size';
            else if (cmd === 'lsl') mode = 'long';
            else if (parts.length === 2) {
                if (parts[1] === '-s') mode = 'size';
                else if (parts[1] === '-l') mode = 'long';
                else mode = null;
            }

            if (mode) {
                cmdExecuted = true;
                const items = utils.getAllItems(userDir);
                const used = utils.getDirSize(userDir);
                let footer = `\n\n📊 Квота: ${utils.formatSize(used)} / ${utils.formatSize(config.QUOTA_LIMIT_BYTES)}`;
                footer += `\n📂 Ваш архив: ${config.BASE_URL}/${userHash}/`;

                if (items.length === 0) {
                    this.reply({ from }, "📁 Папка пуста" + footer);
                } else {
                    const res = ["Список файлов:"];
                    for (let i = 0; i < items.length; i++) {
                        const itm = items[i];
                        const depth = itm.split('/').length - (itm.endsWith('/') ? 1 : 0) - 1;
                        let name = path.basename(itm.replace(/\/$/, ''));
                        if (itm.endsWith('/')) name += "/";
                        const displayItm = (depth > 0 ? "    ".repeat(depth) + "└── " : "") + name;
                        const fullPath = path.join(userDir, itm);

                        if (mode === 'links') {
                            res.push(`${i + 1} - ${displayItm}`);
                        } else if (mode === 'size') {
                            if (itm.endsWith('/')) res.push(`${i + 1} - ${displayItm} [директория]`);
                            else res.push(`${i + 1} - ${displayItm} [${utils.formatSize(fs.statSync(fullPath).size)}]`);
                        } else if (mode === 'long') {
                            const st = fs.statSync(fullPath);
                            const size = utils.formatSize(st.size);
                            const mtime = datetime.format(st.mtime, 'yyyy-MM-dd HH:mm');
                            if (itm.endsWith('/')) {
                                res.push(`${i + 1} - ${displayItm} [директория, ${mtime}]`);
                            } else {
                                res.push(`${i + 1} - ${displayItm} [${size}, загружен ${mtime}]`);
                            }
                        }
                    }
                    this.reply({ from }, res.join('\n') + footer);
                }
            }
        } else if ((cmd === 'link' || cmd === 'lnk') && parts.length === 2) {
            cmdExecuted = true;
            const items = utils.getAllItems(userDir);
            if (items.length === 0) {
                this.reply({ from }, "📁 Папка пуста");
            } else if (parts[1] === '*') {
                const res = items.filter(itm => !itm.endsWith('/')).map((itm, i) => {
                    const idx = i + 1;
                    return `${idx} - ${config.BASE_URL}/${userHash}/${utils.safeQuote(itm)}`;
                });
                this.reply({ from }, res.join('\n'));
            } else {
                const resolvedPaths = utils.resolveItemsList(userDir, parts[1], items);
                const res = [];
                for (const p of resolvedPaths) {
                    if (!fs.statSync(p).isDirectory()) {
                        const rel = path.relative(userDir, p);
                        const idx = items.indexOf(rel);
                        res.push(`${idx >= 0 ? idx + 1 : '?'} - ${config.BASE_URL}/${userHash}/${utils.safeQuote(rel)}`);
                    }
                }
                if (res.length > 0) this.reply({ from }, res.join('\n'));
            }
        } else if (cmd === 'rm' && parts.length >= 2 && parts.length <= 3) {
            cmdExecuted = true;
            const items = utils.getAllItems(userDir);
            if (items.length === 0) {
                this.reply({ from }, "📁 Папка пуста");
            } else if (parts[1] === '*') {
                if (parts.length === 3 && parts[2].toLowerCase() === 'confirm') {
                    const topItems = fs.readdirSync(userDir);
                    for (const itm of topItems) {
                        try { fs.removeSync(path.join(userDir, itm)); } catch (e) {}
                    }
                    this.reply({ from }, "🗑 Все файлы и папки удалены.");
                } else {
                    this.reply({ from }, "⚠ Чтобы удалить ВСЕ файлы, напишите: rm * confirm");
                }
            } else {
                if (parts.length === 2) {
                    const resolvedPaths = utils.resolveItemsList(userDir, parts[1], items);
                    let removedCount = 0;
                    for (const p of resolvedPaths) {
                        try {
                            fs.removeSync(p);
                            removedCount++;
                        } catch (e) {}
                    }
                    if (removedCount > 0) this.reply({ from }, `🗑 Удалено объектов: ${removedCount}`);
                }
            }
        } else if (cmd === 'priv' && parts.length === 1) {
            cmdExecuted = true;
            const indexPath = path.join(userDir, 'index.html');
            const phpPath = path.join(userDir, 'index.php');
            if (fs.existsSync(phpPath)) fs.removeSync(phpPath);
            if (!fs.existsSync(indexPath)) {
                fs.writeFileSync(indexPath, "<html><body><h1>Private Archive</h1></body></html>");
                this.reply({ from }, "🔒 Архив теперь приватный (создан index.html, index.php удалён)");
            } else {
                this.reply({ from }, "ℹ Архив уже приватный.");
            }
        } else if (cmd === 'pub' && parts.length === 1) {
            cmdExecuted = true;
            const indexPath = path.join(userDir, 'index.html');
            const phpPath = path.join(userDir, 'index.php');
            const removed = [];
            if (fs.existsSync(indexPath)) {
                fs.removeSync(indexPath);
                removed.push("index.html");
            }
            if (fs.existsSync(phpPath)) {
                fs.removeSync(phpPath);
                removed.push("index.php");
            }
            if (removed.length > 0) {
                this.reply({ from }, `🔓 Архив теперь публичный (удалено: ${removed.join(', ')})`);
            } else {
                this.reply({ from }, "ℹ Архив уже публичный.");
            }
        } else if (cmd === 'album' && parts.length === 1) {
            cmdExecuted = true;
            const templatePath = 'index.php';
            const targetPath = path.join(userDir, 'index.php');
            const indexHtml = path.join(userDir, 'index.html');
            if (fs.existsSync(templatePath)) {
                try {
                    fs.copySync(templatePath, targetPath);
                    if (fs.existsSync(indexHtml)) fs.removeSync(indexHtml);
                    this.reply({ from }, "🖼 Режим альбома включён (index.php скопирован, index.html удалён)");
                } catch (e) {
                    this.reply({ from }, `❌ Ошибка при создании альбома: ${e.message}`);
                }
            } else {
                this.reply({ from }, "❌ Ошибка: Шаблон index.php не найден в корне бота");
            }
        }

        // Admin commands
        if (!cmdExecuted && config.ADMIN_JID && from.split('/')[0].toLowerCase() === config.ADMIN_JID.toLowerCase()) {
            if (cmd === 'add' && parts.length === 2) {
                cmdExecuted = true;
                const entries = parts[1].split(',').map(e => e.trim().toLowerCase()).filter(e => e);
                const added = [];
                for (const entry of entries) {
                    if (entry === '*' || entry.includes('@') || entry.includes('.')) {
                        this.db.addToWhitelist(entry);
                        added.push(entry);
                    }
                }
                if (added.length > 0) {
                    if (added.includes('*')) this.reply({ from }, "🌟 Доступ разрешён для ВСЕХ пользователей.");
                    else this.reply({ from }, `➕ Добавлено в белый список: ${added.join(', ')}`);
                } else {
                    this.reply({ from }, "⚠ Неверный формат. Используйте user@domain, domain или *");
                }
            } else if (cmd === 'del' && parts.length === 2) {
                cmdExecuted = true;
                const entries = parts[1].split(',').map(e => e.trim().toLowerCase()).filter(e => e);
                const whitelist = this.db.getWhitelist();
                const removed = entries.filter(e => whitelist.has(e));
                for (const e of removed) this.db.removeFromWhitelist(e);
                if (removed.length > 0) this.reply({ from }, `➖ Удалено из белого списка: ${removed.join(', ')}`);
                else this.reply({ from }, "❓ Ничего не найдено для удаления из белого списка.");
            } else if (cmd === 'block' && parts.length === 2) {
                cmdExecuted = true;
                const entries = parts[1].split(',').map(e => e.trim().toLowerCase()).filter(e => e);
                const added = entries.filter(e => e.includes('@') || e.includes('.'));
                for (const e of added) this.db.addToBlacklist(e);
                if (added.length > 0) this.reply({ from }, `🚫 Добавлено в чёрный список: ${added.join(', ')}`);
                else this.reply({ from }, "⚠ Неверный формат. Используйте user@domain или domain");
            } else if (cmd === 'unblock' && parts.length === 2) {
                cmdExecuted = true;
                const entries = parts[1].split(',').map(e => e.trim().toLowerCase()).filter(e => e);
                const blacklist = this.db.getBlacklist();
                const removed = entries.filter(e => blacklist.has(e));
                for (const e of removed) this.db.removeFromBlacklist(e);
                if (removed.length > 0) this.reply({ from }, `✅ Удалено из чёрного списка: ${removed.join(', ')}`);
                else this.reply({ from }, "❓ Ничего не найдено для удаления из чёрного списка.");
            } else if (cmd === 'list' && parts.length === 1) {
                cmdExecuted = true;
                const resW = [...this.db.getWhitelist()].sort().join('\n');
                const resB = [...this.db.getBlacklist()].sort().join('\n');
                this.reply({ from }, `📄 Белый список:\n${resW || '(пусто)'}\n\n🚫 Чёрный список:\n${resB || '(пусто)'}`);
            }
        }

        if (!cmdExecuted) {
            const isAdmin = config.ADMIN_JID && from.split('/')[0].toLowerCase() === config.ADMIN_JID.toLowerCase();
            const used = utils.getDirSize(userDir);
            this.reply({ from }, this.bot.getHelpText(isAdmin, userHash) + `\n\n📊 Квота: ${utils.formatSize(used)} / ${utils.formatSize(config.QUOTA_LIMIT_BYTES)}`);
        }
    }
}

module.exports = CommandsPlugin;
