const fs = require('fs');
const path = require('path');
const url = require('url');
const minimatch = require('minimatch');
const config = require('./config');

function formatSize(size) {
    const units = ['Б', 'кБ', 'МБ', 'ГБ'];
    let unitIdx = 0;
    while (size >= 1024 && unitIdx < units.length - 1) {
        size /= 1024;
        unitIdx++;
    }
    return `${size.toFixed(1).replace('.', ',')} ${units[unitIdx]}`;
}

function getDirSize(dirPath) {
    let totalSize = 0;
    const galleryFiles = new Set(['index.html', 'index.php']);
    const galleryDirs = new Set(['_sfpg_data']);

    function walk(currentPath) {
        const files = fs.readdirSync(currentPath);
        for (const file of files) {
            const fullPath = path.join(currentPath, file);
            const stats = fs.statSync(fullPath);
            if (stats.isDirectory()) {
                if (!galleryDirs.has(file)) {
                    walk(fullPath);
                }
            } else {
                if (!galleryFiles.has(file) && !file.endsWith('.part')) {
                    totalSize += stats.size;
                }
            }
        }
    }

    if (fs.existsSync(dirPath)) {
        walk(dirPath);
    }
    return totalSize;
}

function safeQuote(text) {
    text = text.replace(/ /g, '_');
    return text.split('').map(c => {
        if (c.charCodeAt(0) >= 128 || /[a-zA-Z0-9._\-~/:?=&()]/.test(c)) {
            return c;
        }
        return encodeURIComponent(c);
    }).join('');
}

function getSafePath(userDir, pathStr) {
    userDir = path.resolve(userDir);
    const targetPath = path.resolve(path.join(userDir, pathStr.trim().replace(/^\/+/, '')));
    if (!targetPath.startsWith(userDir)) {
        return null;
    }
    return targetPath;
}

function getUniquePath(filePath) {
    function isTaken(p) {
        return fs.existsSync(p) || fs.existsSync(p + ".part");
    }

    if (!isTaken(filePath)) {
        return filePath;
    }

    const ext = path.extname(filePath);
    const base = filePath.slice(0, filePath.length - ext.length);
    let counter = 1;
    while (true) {
        const newPath = `${base}_${counter}${ext}`;
        if (!isTaken(newPath)) {
            return newPath;
        }
        counter++;
    }
}

function resolveItem(userDir, arg, items) {
    const idx = parseInt(arg, 10) - 1;
    if (!isNaN(idx) && idx >= 0 && idx < items.length) {
        return getSafePath(userDir, items[idx]);
    }
    return getSafePath(userDir, arg);
}

function resolveItemsList(userDir, arg, items) {
    const resolved = [];
    const parts = arg.split(',').map(p => p.trim()).filter(p => p);
    for (const p of parts) {
        if (p.includes('*') || p.includes('?')) {
            if (!p.includes('/')) {
                for (const itm of items) {
                    const name = path.basename(itm.replace(/\/$/, ''));
                    if (minimatch(name, p)) {
                        const targetPath = getSafePath(userDir, itm);
                        if (targetPath) resolved.push(targetPath);
                    }
                }
            } else {
                const matches = items.filter(itm => minimatch(itm, p));
                for (const m of matches) {
                    const targetPath = getSafePath(userDir, m);
                    if (targetPath) resolved.push(targetPath);
                }
            }
        } else {
            const targetPath = resolveItem(userDir, p, items);
            if (targetPath) resolved.push(targetPath);
        }
    }
    return [...new Set(resolved)];
}

function isPhpFile(filename) {
    const forbiddenExtensions = new Set([
        '.php', '.php3', '.php4', '.php5', '.php7', '.phtml',
        '.pht', '.phar', '.phps'
    ]);
    const forbiddenFilenames = new Set(['.htaccess', 'web.config']);

    filename = filename.toLowerCase();
    const cleanName = filename.split('?')[0].split('#')[0].trim().replace(/\.+$/, '');

    if (forbiddenFilenames.has(cleanName)) {
        return true;
    }

    const baseName = path.basename(cleanName);
    if (forbiddenFilenames.has(baseName)) {
        return true;
    }

    const ext = path.extname(cleanName);
    return forbiddenExtensions.has(ext);
}

function getAllItems(userDir) {
    const items = [];
    const galleryFiles = new Set(['index.html', 'index.php']);
    const galleryDirs = new Set(['_sfpg_data']);

    function walk(currentPath, depth = 0) {
        if (depth > config.MAX_DIR_DEPTH) return;

        const files = fs.readdirSync(currentPath);
        const dirsToProcess = [];
        const filesToProcess = [];

        for (const file of files) {
            const fullPath = path.join(currentPath, file);
            const stats = fs.statSync(fullPath);
            const relPath = path.relative(userDir, fullPath);

            if (stats.isDirectory()) {
                if (!galleryDirs.has(file)) {
                    if (relPath.split(path.sep).length <= config.MAX_DIR_DEPTH) {
                        items.push(relPath + "/");
                        dirsToProcess.push(fullPath);
                    }
                }
            } else {
                if (!galleryFiles.has(file) && !file.endsWith('.part')) {
                    if (relPath.split(path.sep).length <= config.MAX_DIR_DEPTH + 1) {
                        items.push(relPath);
                    }
                }
            }
        }

        for (const d of dirsToProcess) {
            walk(d, depth + 1);
        }
    }

    if (fs.existsSync(userDir)) {
        walk(userDir);
    }
    return items.sort();
}

module.exports = {
    formatSize,
    getDirSize,
    safeQuote,
    getSafePath,
    getUniquePath,
    resolveItem,
    resolveItemsList,
    isPhpFile,
    getAllItems
};
