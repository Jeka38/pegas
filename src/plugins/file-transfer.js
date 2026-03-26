const { xml } = require('@xmpp/client');
const fs = require('fs-extra');
const path = require('path');
const axios = require('axios');
const crypto = require('crypto');
const net = require('net');
const ip = require('ip');
const BasePlugin = require('./base');
const config = require('../config');
const utils = require('../utils');

class FileTransferPlugin extends BasePlugin {
    constructor(bot) {
        super(bot);
        this.bot.xmpp.on('stanza', stanza => this.handleStanza(stanza));

        // SOCKS5 Server
        this.server = net.createServer(socket => this.handleSocks5Client(socket));
        this.server.listen(config.SOCKS5_PORT, '0.0.0.0');
    }

    handleStanza(stanza) {
        if (stanza.is('message')) {
            const dataEl = stanza.getChild('data', 'http://jabber.org/protocol/ibb');
            if (dataEl) {
                const sid = dataEl.attrs.sid;
                const info = this.bot.pendingFiles[sid];
                if (info && info.writer) {
                    const chunk = Buffer.from(dataEl.text(), 'base64');
                    info.writer.write(chunk);
                    info.received = (info.received || 0) + chunk.length;
                    info.timestamp = Date.now();
                }
            }
            const closeEl = stanza.getChild('close', 'http://jabber.org/protocol/ibb');
            if (closeEl) {
                const sid = closeEl.attrs.sid;
                const info = this.bot.pendingFiles[sid];
                if (info && info.writer) {
                    this.finishDownload(info, sid);
                }
            }
            return;
        }

        if (!stanza.is('iq')) return;
        const from = stanza.attrs.from;
        const type = stanza.attrs.type;
        if (type === 'error' || type === 'result') return;

        const si = stanza.getChild('si', 'http://jabber.org/protocol/si');
        const queryOob = stanza.getChild('query', 'jabber:iq:oob');
        const queryS5B = stanza.getChild('query', 'http://jabber.org/protocol/bytestreams');
        const jingle = stanza.getChild('jingle', 'urn:xmpp:jingle:1');

        const openIBB = stanza.getChild('open', 'http://jabber.org/protocol/ibb');

        if (si) {
            this.handleRawSI(stanza);
        } else if (openIBB) {
            this.handleOpenIBB(stanza);
        } else if (queryOob) {
            this.handleIQOOB(stanza);
        } else if (queryS5B) {
            this.handleRawS5B(stanza);
        } else if (jingle) {
            this.handleJingle(stanza);
        }
    }

    async handleIQOOB(iq) {
        const query = iq.getChild('query', 'jabber:iq:oob');
        const urlEl = query.getChild('url');
        if (!urlEl) return;
        const url = urlEl.text();
        const desc = query.getChildText('desc');
        const fname = desc || path.basename(url);

        this.downloadFromUrl(url, fname, iq.attrs.from);

        this.bot.xmpp.send(xml('iq', {
            to: iq.attrs.from,
            id: iq.attrs.id,
            type: 'result'
        }));
    }

    async downloadFromUrl(url, fname, peerJid) {
        console.log(`Downloading OOB from ${url}`);

        try {
            const parsedUrl = new URL(url);
            const host = parsedUrl.hostname;
            if (ip.isPrivate(host) || ip.isLoopback(host)) {
                console.error(`OOB: SSRF attempt blocked for ${url}`);
                this.bot.sendMessage(peerJid, `⚠️ Ошибка: Доступ к этому адресу запрещён.`);
                return;
            }
        } catch (e) {
            console.error(`OOB: Invalid URL ${url}`);
            return;
        }

        if (!fname || fname === path.basename(url)) {
            const pathPart = new URL(url).pathname;
            fname = path.basename(pathPart) || "downloaded_file";
        }

        fname = fname.replace(/ /g, '_');
        if (utils.isPhpFile(fname)) {
            this.bot.sendMessage(peerJid, `⚠️ Ошибка: Загрузка PHP-файлов запрещена (${fname})`);
            return;
        }

        const { userDir, userHash } = this.bot.getUserInfo(peerJid);
        const targetPath = utils.getUniquePath(path.join(userDir, fname));
        const partPath = targetPath + ".part";

        try {
            const response = await axios({
                method: 'get',
                url: url,
                responseType: 'stream',
                timeout: 300000
            });

            const fsize = parseInt(response.headers['content-length'] || '0', 10);
            const MAX_OOB_SIZE = 500 * 1024 * 1024;

            if (fsize > MAX_OOB_SIZE) {
                this.bot.sendMessage(peerJid, "⚠️ Ошибка: Размер файла превышает лимит (500 МБ).");
                return;
            }

            if (fsize > 0 && utils.getDirSize(userDir) + fsize > config.QUOTA_LIMIT_BYTES) {
                this.bot.sendMessage(peerJid, "⚠ Квота превышена!");
                return;
            }

            const writer = fs.createWriteStream(partPath);
            let received = 0;

            response.data.on('data', chunk => {
                received += chunk.length;
                if (received > MAX_OOB_SIZE) {
                    response.data.destroy();
                    writer.destroy();
                    fs.removeSync(partPath);
                    this.bot.sendMessage(peerJid, "⚠️ Ошибка: Размер файла превышает лимит (500 МБ).");
                }
            });

            response.data.pipe(writer);

            await new Promise((resolve, reject) => {
                writer.on('finish', resolve);
                writer.on('error', reject);
                response.data.on('error', reject);
            });

            if (fsize > 0 && received !== fsize) {
                console.error(`OOB INCOMPLETE: ${url}, received ${received}/${fsize}`);
                this.bot.sendMessage(peerJid, "⚠️ Ошибка: Файл получен не полностью. Пожалуйста, попробуйте отправить снова.");
                fs.removeSync(partPath);
                return;
            }

            fs.renameSync(partPath, targetPath);
            const realFname = path.basename(targetPath);
            this.bot.sendMessage(peerJid, `✅ Готово!\n${config.BASE_URL}/${userHash}/${utils.safeQuote(realFname)}`);

        } catch (e) {
            console.error(`OOB download error: ${e.message}`);
            if (fs.existsSync(partPath)) fs.removeSync(partPath);
            this.bot.sendMessage(peerJid, `⚠️ Ошибка: Не удалось загрузить файл (${e.message})`);
        }
    }

    handleRawSI(iq) {
        const from = iq.attrs.from;
        if (!this.bot.isAllowed(from)) {
            console.warn(`SI access denied for ${from}`);
            this.bot.sendMessage(from, `⚠️ Доступ запрещён. Пожалуйста, обратитесь к администратору для получения доступа: ${config.ADMIN_JID}`);
            this.sendError(iq, 'not-allowed');
            return;
        }

        try {
            const si = iq.getChild('si', 'http://jabber.org/protocol/si');
            const file = si.getChild('file', 'http://jabber.org/protocol/si/profile/file-transfer');
            const sid = si.attrs.id;
            const fname = (file.attrs.name || "file").replace(/ /g, '_');
            const fsize = parseInt(file.attrs.size || '0', 10);

            console.log(`SI REQUEST: sid=${sid}, from=${from}, file=${fname}, size=${fsize}`);

            if (utils.isPhpFile(fname)) {
                this.bot.sendMessage(from, `⚠️ Ошибка: Загрузка PHP-файлов запрещена (${fname})`);
                this.sendError(iq, 'not-acceptable');
                return;
            }

            const { userDir } = this.bot.getUserInfo(from);
            if (utils.getDirSize(userDir) + fsize > config.QUOTA_LIMIT_BYTES) {
                this.sendError(iq, 'not-acceptable');
                return;
            }

            const featureNeg = si.getChild('feature', 'http://jabber.org/protocol/feature-neg');
            let offeredMethods = [];
            if (featureNeg) {
                const x = featureNeg.getChild('x', 'jabber:x:data');
                if (x) {
                    const field = x.getChildren('field').find(f => f.attrs.var === 'stream-method');
                    if (field) {
                        offeredMethods = field.getChildren('value').map(v => v.text());
                    }
                }
            }

            const chosenMethod = ['http://jabber.org/protocol/bytestreams', 'http://jabber.org/protocol/ibb'].find(m => offeredMethods.includes(m));

            if (!chosenMethod) {
                this.sendError(iq, 'bad-request');
                return;
            }

            this.bot.pendingFiles[sid] = {
                name: fname,
                size: fsize,
                timestamp: Date.now(),
                peerJid: from,
                transportSid: sid,
                downloading: false,
                method: chosenMethod
            };

            const reply = xml('iq', { to: from, id: iq.attrs.id, type: 'result' },
                xml('si', { xmlns: 'http://jabber.org/protocol/si', id: sid },
                    xml('feature', { xmlns: 'http://jabber.org/protocol/feature-neg' },
                        xml('x', { xmlns: 'jabber:x:data', type: 'submit' },
                            xml('field', { var: 'stream-method' },
                                xml('value', {}, chosenMethod)
                            )
                        )
                    )
                )
            );
            this.bot.xmpp.send(reply);

        } catch (e) {
            console.error(`SI ERROR: ${e.message}`);
            this.sendError(iq, 'internal-server-error');
        }
    }

    handleRawS5B(iq) {
        const query = iq.getChild('query', 'http://jabber.org/protocol/bytestreams');
        if (query && query.getChild('streamhost-used')) {
            this.socks5ConnectAndSave(iq);
        } else {
            this.socks5ConnectAndSave(iq);
        }
    }

    async socks5ConnectAndSave(iq, jingleSid = null) {
        let sid = null;
        let peerFull = iq.attrs.from;
        let hosts = [];
        let used = null;

        try {
            if (jingleSid) {
                sid = jingleSid;
                const jingle = iq.getChild('jingle', 'urn:xmpp:jingle:1');
                const content = jingle.getChild('content');
                const transport = content.getChild('transport', 'urn:xmpp:jingle:transports:s5b:1');
                hosts = transport.getChildren('candidate').map(c => ({
                    host: c.attrs.host,
                    port: parseInt(c.attrs.port || '1080', 10),
                    jid: c.attrs.jid,
                    cid: c.attrs.cid
                }));
            } else {
                const query = iq.getChild('query', 'http://jabber.org/protocol/bytestreams');
                sid = query.attrs.sid;
                used = query.getChild('streamhost-used');
                if (used) {
                    const jid = used.attrs.jid;
                    // In a real scenario, we'd need to look up the proxy info.
                    // For now, let's assume direct connection or known proxies.
                    // This part is simplified compared to Python version.
                    return;
                } else {
                    hosts = query.getChildren('streamhost').map(h => ({
                        host: h.attrs.host,
                        port: parseInt(h.attrs.port || '1080', 10),
                        jid: h.attrs.jid
                    }));
                }

                if (hosts.length === 0 && !used) {
                    // Send our own streamhosts
                    const localIp = this.getLocalIp();
                    const reply = xml('iq', { to: peerFull, id: iq.attrs.id, type: 'result' },
                        xml('query', { xmlns: 'http://jabber.org/protocol/bytestreams', sid: sid },
                            xml('streamhost', { host: localIp, port: config.SOCKS5_PORT, jid: this.bot.xmpp.jid.toString() })
                        )
                    );
                    this.bot.xmpp.send(reply);
                    return;
                }
            }

            const fileInfo = this.bot.pendingFiles[sid];
            if (!fileInfo) return;
            const tSid = fileInfo.transportSid || sid;
            const dstAddr = crypto.createHash('sha1').update(`${tSid}${peerFull}${this.bot.xmpp.jid.toString()}`).digest('hex');

            for (const host of hosts) {
                try {
                    console.log(`S5B: Connecting to ${host.host}:${host.port} for sid=${sid}`);
                    const socket = await this.connectSocks5(host.host, host.port, dstAddr);

                    if (jingleSid) {
                        const reply = xml('iq', { to: peerFull, type: 'set', id: this.bot.xmpp.id() },
                            xml('jingle', { xmlns: 'urn:xmpp:jingle:1', action: 'transport-info', sid: jingleSid, initiator: peerFull },
                                xml('content', { creator: fileInfo.contentCreator || 'initiator', name: fileInfo.contentName || 'file' },
                                    xml('transport', { xmlns: 'urn:xmpp:jingle:transports:s5b:1', sid: sid },
                                        xml('candidate-used', { cid: host.cid })
                                    )
                                )
                            )
                        );
                        this.bot.xmpp.send(reply);
                    } else {
                        const reply = xml('iq', { to: peerFull, id: iq.attrs.id, type: 'result' },
                            xml('query', { xmlns: 'http://jabber.org/protocol/bytestreams', sid: sid },
                                xml('streamhost-used', { jid: host.jid })
                            )
                        );
                        this.bot.xmpp.send(reply);
                    }

                    console.log(`S5B: SUCCESS connect to ${host.host}:${host.port} for sid=${sid}`);
                    fileInfo.downloading = true;
                    await this.downloadFileTask(socket, fileInfo, peerFull, sid);
                    return;
                } catch (e) {
                    console.log(`S5B: Failed connect to ${host.host} for sid=${sid}: ${e.message}`);
                }
            }

            if (!jingleSid) this.sendError(iq, 'service-unavailable');

        } catch (e) {
            console.error(`SOCKS5 ERROR: ${e.message}`);
        }
    }

    async connectSocks5(host, port, dstAddr) {
        return new Promise((resolve, reject) => {
            const socket = net.createConnection({ host, port }, () => {
                socket.write(Buffer.from([0x05, 0x01, 0x00]));
            });
            socket.setTimeout(5000);

            socket.once('data', data => {
                if (data[0] !== 0x05 || data[1] !== 0x00) {
                    socket.destroy();
                    reject(new Error('SOCKS5 auth failed'));
                    return;
                }
                const req = Buffer.concat([
                    Buffer.from([0x05, 0x01, 0x00, 0x03]),
                    Buffer.from([dstAddr.length]),
                    Buffer.from(dstAddr),
                    Buffer.from([0x00, 0x00])
                ]);
                socket.write(req);

                socket.once('data', data => {
                    if (data[1] !== 0x00) {
                        socket.destroy();
                        reject(new Error('SOCKS5 connect failed'));
                        return;
                    }
                    // Skip address info
                    resolve(socket);
                });
            });

            socket.on('error', reject);
            socket.on('timeout', () => {
                socket.destroy();
                reject(new Error('Timeout'));
            });
        });
    }

    handleJingle(iq) {
        try {
            const jingle = iq.getChild('jingle', 'urn:xmpp:jingle:1');
            const action = jingle.attrs.action;
            const sid = jingle.attrs.sid;
            const from = iq.attrs.from;

            console.log(`JINGLE EVENT: action=${action}, sid=${sid}, from=${from}`);

            if (action === 'session-initiate') {
                if (!this.bot.isAllowed(from)) {
                    this.bot.sendMessage(from, `⚠️ Доступ запрещён. Пожалуйста, обратитесь к администратору для получения доступа: ${config.ADMIN_JID}`);
                    this.sendError(iq, 'not-allowed');
                    return;
                }
                const content = jingle.getChild('content');
                let ftNs = 'urn:xmpp:jingle:apps:file-transfer:5';
                let description = content.getChild('description', ftNs);
                if (!description) {
                    ftNs = 'urn:xmpp:jingle:apps:file-transfer:4';
                    description = content.getChild('description', ftNs);
                }
                const fileTag = description.getChild('file');
                const nameTag = fileTag.getChild('name');
                const sizeTag = fileTag.getChild('size');

                const fname = (nameTag.text() || "file").replace(/ /g, '_');
                if (utils.isPhpFile(fname)) {
                    this.bot.sendMessage(from, `⚠️ Ошибка: Загрузка PHP-файлов запрещена (${fname})`);
                    this.sendError(iq, 'not-acceptable');
                    return;
                }
                const fsize = parseInt(sizeTag.text() || '0', 10);
                const { userDir } = this.bot.getUserInfo(from);
                if (utils.getDirSize(userDir) + fsize > config.QUOTA_LIMIT_BYTES) {
                    this.sendError(iq, 'not-acceptable');
                    return;
                }

                const s5bT = content.getChild('transport', 'urn:xmpp:jingle:transports:s5b:1');
                const ibbT = content.getChild('transport', 'urn:xmpp:jingle:transports:ibb:1');
                const transportSid = (s5bT && s5bT.attrs.sid) || (ibbT && ibbT.attrs.sid) || sid;

                this.bot.pendingFiles[sid] = {
                    name: fname,
                    size: fsize,
                    timestamp: Date.now(),
                    peerJid: from,
                    ibbAllowed: true,
                    contentName: content.attrs.name,
                    contentCreator: content.attrs.creator,
                    ftNs: ftNs,
                    transportSid: transportSid,
                    sessionSid: sid,
                    downloading: false
                };
                if (transportSid !== sid) this.bot.pendingFiles[transportSid] = this.bot.pendingFiles[sid];

                this.bot.xmpp.send(xml('iq', { to: from, id: iq.attrs.id, type: 'result' }));

                // Accept session
                const acceptIq = xml('iq', { to: from, type: 'set', id: this.bot.xmpp.id() },
                    xml('jingle', { xmlns: 'urn:xmpp:jingle:1', action: 'session-accept', sid: sid, initiator: from },
                        xml('content', { creator: content.attrs.creator, name: content.attrs.name },
                            xml('description', { xmlns: ftNs },
                                xml('file', { xmlns: ftNs },
                                    xml('name', {}, fname),
                                    xml('size', {}, fsize.toString())
                                )
                            ),
                            s5bT ? xml('transport', { xmlns: 'urn:xmpp:jingle:transports:s5b:1', sid: transportSid, mode: 'tcp' },
                                xml('candidate', { host: this.getLocalIp(), port: config.SOCKS5_PORT, jid: this.bot.xmpp.jid.toString(), cid: 'local', priority: '8253074', type: 'host' })
                            ) : xml('transport', { xmlns: 'urn:xmpp:jingle:transports:ibb:1', sid: transportSid, 'block-size': '32768' })
                        )
                    )
                );
                this.bot.xmpp.send(acceptIq);

                if (s5bT) {
                    this.socks5ConnectAndSave(iq, sid);
                }

            } else if (action === 'transport-info') {
                this.bot.xmpp.send(xml('iq', { to: from, id: iq.attrs.id, type: 'result' }));
                const content = jingle.getChild('content');
                const transport = content.getChild('transport', 'urn:xmpp:jingle:transports:s5b:1');
                if (transport && (!this.bot.pendingFiles[sid] || !this.bot.pendingFiles[sid].downloading)) {
                    this.socks5ConnectAndSave(iq, sid);
                }
            } else if (action === 'session-terminate') {
                this.bot.xmpp.send(xml('iq', { to: from, id: iq.attrs.id, type: 'result' }));
                delete this.bot.pendingFiles[sid];
            } else {
                this.bot.xmpp.send(xml('iq', { to: from, id: iq.attrs.id, type: 'result' }));
            }
        } catch (e) {
            console.error(`JINGLE IQ ERROR: ${e.message}`);
        }
    }

    async handleSocks5Client(socket) {
        try {
            socket.once('data', async data => {
                if (data[0] !== 0x05) { socket.destroy(); return; }
                socket.write(Buffer.from([0x05, 0x00]));

                socket.once('data', async data => {
                    if (data[1] !== 0x01) { socket.destroy(); return; }
                    const addrLen = data[4];
                    const dstAddr = data.slice(5, 5 + addrLen).toString();

                    let matchFound = false;
                    for (const sid in this.bot.pendingFiles) {
                        const info = this.bot.pendingFiles[sid];
                        const tSid = info.transportSid || sid;
                        const expected = crypto.createHash('sha1').update(`${tSid}${info.peerJid}${this.bot.xmpp.jid.toString()}`).digest('hex');

                        if (dstAddr === expected) {
                            if (info.downloading) {
                                socket.write(Buffer.concat([Buffer.from([0x05, 0x01, 0x00, 0x03, addrLen]), Buffer.from(dstAddr), Buffer.from([0x00, 0x00])]));
                                socket.destroy(); return;
                            }
                            info.downloading = true;
                            socket.write(Buffer.concat([Buffer.from([0x05, 0x00, 0x00, 0x03, addrLen]), Buffer.from(dstAddr), Buffer.from([0x00, 0x00])]));
                            console.log(`SOCKS5: Recognized incoming connection for sid=${sid}`);
                            await this.downloadFileTask(socket, info, info.peerJid, sid);
                            matchFound = true;
                            break;
                        }
                    }
                    if (!matchFound) {
                        socket.write(Buffer.concat([Buffer.from([0x05, 0x01, 0x00, 0x03, addrLen]), Buffer.from(dstAddr), Buffer.from([0x00, 0x00])]));
                        socket.destroy();
                    }
                });
            });
        } catch (e) {
            console.error(`SOCKS5 server error: ${e.message}`);
        }
    }

    async downloadFileTask(reader, fileInfo, peerJid, sid) {
        console.log(`DOWNLOAD START: sid=${sid}, peer=${peerJid}, file=${fileInfo.name}, size=${fileInfo.size}`);
        const { userDir, userHash } = this.bot.getUserInfo(peerJid);
        const targetPath = utils.getUniquePath(path.join(userDir, path.basename(fileInfo.name)));
        const partPath = targetPath + ".part";
        const writer = fs.createWriteStream(partPath);
        let received = 0;

        return new Promise((resolve, reject) => {
            reader.on('data', chunk => {
                writer.write(chunk);
                received += chunk.length;
                fileInfo.timestamp = Date.now();
                if (received >= fileInfo.size) {
                    reader.pause(); // Stop receiving data if we have enough
                    finish();
                }
            });

            reader.on('end', () => {
                if (received < fileInfo.size) {
                    reject(new Error('Incomplete download'));
                } else {
                    finish();
                }
            });

            reader.on('error', reject);
            writer.on('error', reject);

            const finish = () => {
                writer.end();
                fs.renameSync(partPath, targetPath);
                console.log(`DOWNLOAD COMPLETE: sid=${sid}, path=${targetPath}`);
                this.bot.sendMessage(peerJid, `✅ Готово!\n${config.BASE_URL}/${userHash}/${utils.safeQuote(path.basename(targetPath))}`);

                if (fileInfo.sessionSid && fileInfo.ftNs) {
                    const infoIq = xml('iq', { to: peerJid, type: 'set', id: this.bot.xmpp.id() },
                        xml('jingle', { xmlns: 'urn:xmpp:jingle:1', action: 'session-info', sid: fileInfo.sessionSid, initiator: peerJid },
                            xml('received', { xmlns: fileInfo.ftNs })
                        )
                    );
                    this.bot.xmpp.send(infoIq);

                    const termIq = xml('iq', { to: peerJid, type: 'set', id: this.bot.xmpp.id() },
                        xml('jingle', { xmlns: 'urn:xmpp:jingle:1', action: 'session-terminate', sid: fileInfo.sessionSid, initiator: peerJid },
                            xml('reason', {}, xml('success'))
                        )
                    );
                    this.bot.xmpp.send(termIq);
                }
                delete this.bot.pendingFiles[sid];
                resolve();
            };
        });
    }

    handleOpenIBB(iq) {
        const open = iq.getChild('open', 'http://jabber.org/protocol/ibb');
        const sid = open.attrs.sid;
        const info = this.bot.pendingFiles[sid];
        if (!info) {
            this.sendError(iq, 'item-not-found');
            return;
        }

        const { userDir } = this.bot.getUserInfo(iq.attrs.from);
        const targetPath = utils.getUniquePath(path.join(userDir, path.basename(info.name)));
        info.targetPath = targetPath;
        info.partPath = targetPath + ".part";
        info.writer = fs.createWriteStream(info.partPath);
        info.received = 0;

        this.bot.xmpp.send(xml('iq', { to: iq.attrs.from, id: iq.attrs.id, type: 'result' }));
    }

    finishDownload(info, sid) {
        if (!info.writer) return;
        info.writer.end();
        info.writer.on('finish', () => {
        const { userHash } = this.bot.getUserInfo(info.peerJid);

        if (info.received === info.size || info.size === 0) {
            fs.renameSync(info.partPath, info.targetPath);
            console.log(`DOWNLOAD COMPLETE (IBB): sid=${sid}, path=${info.targetPath}`);
            this.bot.sendMessage(info.peerJid, `✅ Готово!\n${config.BASE_URL}/${userHash}/${utils.safeQuote(path.basename(info.targetPath))}`);

            if (info.sessionSid && info.ftNs) {
                const infoIq = xml('iq', { to: info.peerJid, type: 'set', id: this.bot.xmpp.id() },
                    xml('jingle', { xmlns: 'urn:ietf:params:xml:ns:xmpp-stanzas', action: 'session-info', sid: info.sessionSid, initiator: info.peerJid },
                        xml('received', { xmlns: info.ftNs })
                    )
                );
                this.bot.xmpp.send(infoIq);

                const termIq = xml('iq', { to: info.peerJid, type: 'set', id: this.bot.xmpp.id() },
                    xml('jingle', { xmlns: 'urn:ietf:params:xml:ns:xmpp-stanzas', action: 'session-terminate', sid: info.sessionSid, initiator: info.peerJid },
                        xml('reason', {}, xml('success'))
                    )
                );
                this.bot.xmpp.send(termIq);
            }
        } else {
            console.error(`DOWNLOAD INCOMPLETE: sid=${sid}, received ${info.received}/${info.size}`);
            this.bot.sendMessage(info.peerJid, "⚠️ Ошибка: Файл получен не полностью. Пожалуйста, попробуйте отправить снова.");
            if (fs.existsSync(info.partPath)) fs.removeSync(info.partPath);
        }
        delete this.bot.pendingFiles[sid];
        });
    }

    getLocalIp() {
        if (config.SOCKS5_IP) return config.SOCKS5_IP;
        const interfaces = require('os').networkInterfaces();
        for (const name of Object.keys(interfaces)) {
            for (const iface of interfaces[name]) {
                if (iface.family === 'IPv4' && !iface.internal) {
                    return iface.address;
                }
            }
        }
        return '127.0.0.1';
    }

    sendError(iq, condition) {
        const reply = xml('iq', {
            to: iq.attrs.from,
            id: iq.attrs.id,
            type: 'error'
        }, xml('error', { type: 'cancel' }, xml(condition, { xmlns: 'urn:ietf:params:xml:ns:xmpp-stanzas' })));
        this.bot.xmpp.send(reply);
    }
}

module.exports = FileTransferPlugin;
