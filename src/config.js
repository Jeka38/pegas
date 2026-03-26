const path = require('path');
require('dotenv').config();

const QUOTA_LIMIT_GB = parseInt(process.env.QUOTA_GB || '15', 10);
const QUOTA_LIMIT_BYTES = QUOTA_LIMIT_GB * 1024 * 1024 * 1024;

const ADMIN_JID = process.env.ADMIN_JID;
const ADMIN_NOTIFY_LEVEL = (process.env.ADMIN_NOTIFY_LEVEL || 'all').toLowerCase();

const DB_PATH = process.env.DB_PATH || '/app/data/bot.db';
const WHITELIST_FILE = process.env.WHITELIST_FILE || 'whitelist.json';

const MAX_DIR_DEPTH = parseInt(process.env.MAX_DIR_DEPTH || '2', 10);
const DOWNLOAD_DIR = process.env.DOWNLOAD_DIR;

const SOCKS5_PORT = parseInt(process.env.SOCKS5_PORT || '1080', 10);
const SOCKS5_IP = process.env.SOCKS5_IP;

const XMPP_JID = process.env.XMPP_JID;
const XMPP_RESOURCE = process.env.XMPP_RESOURCE;
const XMPP_PASSWORD = process.env.XMPP_PASSWORD;
const XMPP_HOST = process.env.XMPP_HOST || 'jabberworld.info';
const XMPP_PORT = parseInt(process.env.XMPP_PORT || '5222', 10);

const APP_NAME = process.env.APP_NAME || 'OBBFastBot';
const VERSION = process.env.APP_VERSION || '1.1';
const STATUS_MESSAGE = process.env.STATUS_MESSAGE || 'Для помощи по боту напиши ? или help';
const BASE_URL = (process.env.BASE_URL || "").replace(/\/+$/, "");

module.exports = {
    QUOTA_LIMIT_GB,
    QUOTA_LIMIT_BYTES,
    ADMIN_JID,
    ADMIN_NOTIFY_LEVEL,
    DB_PATH,
    WHITELIST_FILE,
    MAX_DIR_DEPTH,
    DOWNLOAD_DIR,
    SOCKS5_PORT,
    SOCKS5_IP,
    XMPP_JID,
    XMPP_RESOURCE,
    XMPP_PASSWORD,
    XMPP_HOST,
    XMPP_PORT,
    APP_NAME,
    VERSION,
    STATUS_MESSAGE,
    BASE_URL
};
