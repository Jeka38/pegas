const Database = require('better-sqlite3');
const fs = require('fs-extra');
const path = require('path');
const config = require('./config');

class DB {
    constructor(dbPath = config.DB_PATH) {
        this.dbPath = path.resolve(dbPath);
        console.log(`Инициализация базы данных: ${this.dbPath}`);

        if (fs.existsSync(this.dbPath) && fs.lstatSync(this.dbPath).isDirectory()) {
            console.warn(`ВНИМАНИЕ: Путь ${this.dbPath} — директория. Используем ${this.dbPath}/bot_data.db`);
            this.dbPath = path.join(this.dbPath, "bot_data.db");
        }

        const dbDir = path.dirname(this.dbPath);
        if (dbDir && !fs.existsSync(dbDir)) {
            console.log(`Создание директории для БД: ${dbDir}`);
            fs.ensureDirSync(dbDir);
        }

        this.db = new Database(this.dbPath);
        this._createTables();
    }

    _createTables() {
        this.db.exec(`
            CREATE TABLE IF NOT EXISTS whitelist (
                entry TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                entry TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS user_folders (
                jid TEXT PRIMARY KEY,
                folder_hash TEXT NOT NULL
            );
        `);
    }

    addToWhitelist(entry) {
        entry = entry.toLowerCase();
        const stmt = this.db.prepare("INSERT OR IGNORE INTO whitelist (entry) VALUES (?)");
        stmt.run(entry);
    }

    removeFromWhitelist(entry) {
        entry = entry.toLowerCase();
        const stmt = this.db.prepare("DELETE FROM whitelist WHERE entry = ?");
        stmt.run(entry);
    }

    getWhitelist() {
        const rows = this.db.prepare("SELECT entry FROM whitelist").all();
        return new Set(rows.map(row => row.entry));
    }

    addToBlacklist(entry) {
        entry = entry.toLowerCase();
        const stmt = this.db.prepare("INSERT OR IGNORE INTO blacklist (entry) VALUES (?)");
        stmt.run(entry);
    }

    removeFromBlacklist(entry) {
        entry = entry.toLowerCase();
        const stmt = this.db.prepare("DELETE FROM blacklist WHERE entry = ?");
        stmt.run(entry);
    }

    getBlacklist() {
        const rows = this.db.prepare("SELECT entry FROM blacklist").all();
        return new Set(rows.map(row => row.entry));
    }

    getUserFolder(jid) {
        jid = jid.toLowerCase();
        const row = this.db.prepare("SELECT folder_hash FROM user_folders WHERE jid = ?").get(jid);
        return row ? row.folder_hash : null;
    }

    setUserFolder(jid, folderHash) {
        jid = jid.toLowerCase();
        const stmt = this.db.prepare("INSERT OR REPLACE INTO user_folders (jid, folder_hash) VALUES (?, ?)");
        stmt.run(jid, folderHash);
    }
}

module.exports = DB;
