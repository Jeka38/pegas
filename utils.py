import os
import urllib.parse
import fnmatch
import re
from config import MAX_DIR_DEPTH

def format_size(size):
    """Форматирует размер в человеко-читаемый вид (Б → кБ → МБ → ГБ)"""
    for unit in ['Б', 'кБ', 'МБ', 'ГБ']:
        if size < 1024:
            res = f"{size:.1f}".replace('.', ',')
            return f"{res} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ".replace('.', ',')

def get_dir_size(path):
    """Подсчитывает суммарный размер всех файлов в папке (рекурсивно), исключая служебные файлы галереи"""
    total_size = 0
    gallery_files = {'index.html', 'index.php'}
    gallery_dirs = {'_sfpg_data'}

    for d, dirs, fs in os.walk(path):
        # Исключаем папку с данными галереи
        dirs[:] = [dir_name for dir_name in dirs if dir_name not in gallery_dirs]

        for f in fs:
            if f not in gallery_files:
                total_size += os.path.getsize(os.path.join(d, f))
    return total_size

def safe_quote(text):
    """Красивое кодирование URL (сохраняем кириллицу для читаемости)"""
    text = text.replace(' ', '_')
    return "".join(c if ord(c) >= 128 or c.isalnum() or c in '._-~/:?=&()'
                   else urllib.parse.quote(c) for c in text)

def get_safe_path(user_dir, path_str):
    """Безопасное получение пути внутри папки пользователя"""
    user_dir = os.path.abspath(user_dir)
    target_path = os.path.abspath(os.path.join(user_dir, path_str.strip().lstrip('/')))
    if not target_path.startswith(user_dir):
        return None
    return target_path

def get_unique_path(path):
    """Получаем уникальный путь для предотвращения перезаписи, учитывая .part файлы"""
    def is_taken(p):
        return os.path.exists(p) or os.path.exists(p + ".part")

    if not is_taken(path):
        return path

    base, ext = os.path.splitext(path)
    counter = 1
    while True:
        new_path = f"{base}_{counter}{ext}"
        if not is_taken(new_path):
            return new_path
        counter += 1

def resolve_item(user_dir, arg, items):
    """Разрешение аргумента как индекса или пути"""
    try:
        idx = int(arg) - 1
        if 0 <= idx < len(items):
            return get_safe_path(user_dir, items[idx])
    except ValueError:
        pass

    # Try exact path match
    path = get_safe_path(user_dir, arg)
    if path and os.path.exists(path):
        return path

    # Try matching basename from items (for directories without full path)
    arg_lower = arg.lower().rstrip('/')
    for itm in items:
        name = os.path.basename(itm.rstrip('/')).lower()
        if name == arg_lower:
            return get_safe_path(user_dir, itm)

    return path

def resolve_items_list(user_dir, arg, items):
    """Разрешение списка аргументов (индексы, пути, шаблоны).
    Аргумент 'arg' может быть строкой (через запятую) или списком строк.
    """
    resolved = []
    if isinstance(arg, str):
        parts = [p.strip() for p in arg.split(',') if p.strip()]
    else:
        parts = []
        for p in arg:
            parts.extend([i.strip() for i in p.split(',') if i.strip()])
    for p in parts:
        if '*' in p or '?' in p:
            if '/' not in p:
                for itm in items:
                    name = os.path.basename(itm.rstrip('/'))
                    if fnmatch.fnmatch(name.lower(), p.lower()):
                        path = get_safe_path(user_dir, itm)
                        if path: resolved.append(path)
            else:
                regex = re.compile(fnmatch.translate(p), re.IGNORECASE)
                for m in items:
                    if regex.match(m):
                        path = get_safe_path(user_dir, m)
                        if path: resolved.append(path)
        else:
            path = resolve_item(user_dir, p, items)
            if path: resolved.append(path)
    return list(dict.fromkeys(resolved))

def is_php_file(filename):
    """Проверяет, является ли файл PHP-скриптом или опасным конфигом по его расширению"""
    # Опасные расширения, связанные с PHP
    forbidden_extensions = {
        '.php', '.php3', '.php4', '.php5', '.php7', '.phtml',
        '.pht', '.phar', '.phps'
    }
    # Опасные полные имена файлов (конфигурации)
    forbidden_filenames = {'.htaccess', 'web.config'}

    filename = filename.lower()
    # Убираем потенциальные параметры запроса и фрагменты, если имя получено из URL
    clean_name = filename.split('?')[0].split('#')[0].strip().rstrip('.')

    if clean_name in forbidden_filenames:
        return True

    base_name = os.path.basename(clean_name)
    if base_name in forbidden_filenames:
        return True

    _, ext = os.path.splitext(clean_name)
    return ext in forbidden_extensions

def get_all_items(user_dir):
    """Получаем все элементы рекурсивно с ограничением вложенности, скрывая системные файлы"""
    items = []
    gallery_files = {'index.html', 'index.php'}
    gallery_dirs = {'_sfpg_data'}

    for root, dirs, files in os.walk(user_dir):
        # Исключаем папку с данными галереи из обхода
        dirs[:] = [d for d in dirs if d not in gallery_dirs]

        rel_root = os.path.relpath(root, user_dir)
        if rel_root == ".":
            rel_root = ""

        # Ограничение вложенности: не более MAX_DIR_DEPTH уровней директорий
        if rel_root != "" and rel_root.count(os.sep) >= MAX_DIR_DEPTH:
            continue

        for d in dirs:
            path = os.path.join(rel_root, d)
            if path.count(os.sep) < MAX_DIR_DEPTH:
                items.append(path + "/")
        for f in files:
            if f in gallery_files or f.endswith('.part'):
                continue
            path = os.path.join(rel_root, f)
            # Файлы могут находиться в директориях уровня MAX_DIR_DEPTH
            if path.count(os.sep) <= MAX_DIR_DEPTH:
                items.append(path)
    return sorted(items)
