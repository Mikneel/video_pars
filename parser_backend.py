#!/usr/bin/env python3
"""
VideoParser CMS — Масштабируемая видеоплатформа с парсером
Версия: 2.0 (Full CMS с SEO, админкой, рекламой, категориями)
Использование: python parser_backend.py
API запускается на http://localhost:5000
"""

from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import json
import sqlite3
import os
import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from functools import wraps

# ─────────────────────────────────────────────
# КОНФИГУРАЦИЯ
# ─────────────────────────────────────────────

app = Flask(__name__)
CORS(app, supports_credentials=True)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DB_PATH = "videos.db"
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = hashlib.sha256(
    os.environ.get("ADMIN_PASSWORD", "admin123").encode()
).hexdigest()

# ─────────────────────────────────────────────
# БАЗА ДАННЫХ — РАСШИРЕННАЯ СХЕМА CMS
# ─────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Таблица категорий
    c.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            slug        TEXT NOT NULL UNIQUE,
            description TEXT,
            parent_id   INTEGER REFERENCES categories(id),
            sort_order  INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица видео — расширенная с SEO полями
    c.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT,
            thumbnail   TEXT,
            video_url   TEXT,
            source_url  TEXT UNIQUE,
            duration    TEXT,
            views       INTEGER DEFAULT 0,
            author      TEXT,
            category_id INTEGER REFERENCES categories(id),
            
            -- SEO поля
            meta_title      TEXT,
            meta_desc       TEXT,
            keywords        TEXT,
            og_image        TEXT,
            slug            TEXT UNIQUE,
            
            -- Контент
            content_html    TEXT,
            status          TEXT DEFAULT 'published',
            
            parsed_at   TEXT,
            added_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица настроек (реклама, конфиги)
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key_name    TEXT NOT NULL UNIQUE,
            value       TEXT,
            type        TEXT DEFAULT 'text',
            description TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица рекламных баннеров
    c.execute('''
        CREATE TABLE IF NOT EXISTS ads (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            position    TEXT NOT NULL,
            image_url   TEXT,
            link_url    TEXT,
            html_code   TEXT,
            is_active   INTEGER DEFAULT 1,
            impressions INTEGER DEFAULT 0,
            clicks      INTEGER DEFAULT 0,
            start_date  TEXT,
            end_date    TEXT,
            priority    INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Индексы для производительности
    c.execute('CREATE INDEX IF NOT EXISTS idx_videos_category ON videos(category_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_videos_slug ON videos(slug)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ads_position ON ads(position)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ads_active ON ads(is_active)')
    
    # Заполняем настройки по умолчанию
    default_settings = [
        ('site_name', 'VideoStream CMS', 'text', 'Название сайта'),
        ('site_description', 'Видеоплатформа с парсером контента', 'text', 'Описание сайта'),
        ('top_banner_html', '', 'html', 'HTML код верхнего баннера'),
        ('bottom_banner_html', '', 'html', 'HTML код нижнего баннера'),
        ('pre_roll_video', '', 'url', 'URL Pre-roll видео'),
        ('google_analytics', '', 'text', 'Google Analytics ID'),
        ('enable_comments', '1', 'boolean', 'Включить комментарии'),
    ]
    for key, value, stype, desc in default_settings:
        c.execute('''
            INSERT OR IGNORE INTO settings (key_name, value, type, description)
            VALUES (?, ?, ?, ?)
        ''', (key, value, stype, desc))
    
    # Добавляем дефолтные категории
    default_categories = [
        ('Фильмы', 'films', 'Художественные фильмы и сериалы', None),
        ('Музыка', 'music', 'Музыкальные клипы и концерты', None),
        ('Образование', 'education', 'Обучающий контент', None),
        ('Спорт', 'sports', 'Спортивные трансляции и обзоры', None),
        ('Новости', 'news', 'Новостные сюжеты', None),
    ]
    for name, slug, desc, parent in default_categories:
        c.execute('''
            INSERT OR IGNORE INTO categories (name, slug, description, parent_id)
            VALUES (?, ?, ?, ?)
        ''', (name, slug, desc, parent))
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────
# ДЕКОРАТОРЫ БЕЗОПАСНОСТИ
# ─────────────────────────────────────────────

def login_required(f):
    """Декоратор для защиты админских endpoints"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({"error": "Требуется авторизация"}), 401
        return f(*args, **kwargs)
    return decorated_function


# ─────────────────────────────────────────────
# ПАРСЕР С РАСШИРЕННЫМИ ВОЗМОЖНОСТЯМИ
# ─────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

def parse_video_page(url: str) -> dict:
    """
    Универсальный парсер страницы с видео.
    Извлекает: заголовок, описание, превью, прямую ссылку, SEO-данные.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": f"Не удалось загрузить страницу: {e}"}

    soup = BeautifulSoup(resp.text, "html.parser")
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    # Заголовок
    title = (
        _meta(soup, "og:title")
        or _meta(soup, "twitter:title")
        or (soup.find("h1").get_text(strip=True) if soup.find("h1") else None)
        or (soup.title.string.strip() if soup.title else "Без названия")
    )

    # Описание
    description = (
        _meta(soup, "og:description")
        or _meta(soup, "description")
        or _meta(soup, "twitter:description")
        or ""
    )

    # Превью
    thumbnail = (
        _meta(soup, "og:image")
        or _meta(soup, "twitter:image")
        or ""
    )
    if thumbnail and thumbnail.startswith("/"):
        thumbnail = urljoin(base, thumbnail)

    # Прямая ссылка на видео
    video_url = _extract_video_url(soup, resp.text, url, base)

    # Автор
    author = (
        _meta(soup, "author")
        or _meta(soup, "og:site_name")
        or urlparse(url).netloc
    )

    # Длительность
    duration = _meta(soup, "og:video:duration") or _extract_duration(soup, resp.text)

    # Ключевые слова
    keywords = _meta(soup, "keywords") or ""

    return {
        "title":       title,
        "description": description,
        "thumbnail":   thumbnail,
        "video_url":   video_url,
        "source_url":  url,
        "author":      author,
        "duration":    duration,
        "keywords":    keywords,
        "parsed_at":   datetime.utcnow().isoformat(),
    }


def _meta(soup, name: str) -> str | None:
    tag = (
        soup.find("meta", property=name)
        or soup.find("meta", attrs={"name": name})
    )
    return tag["content"].strip() if tag and tag.get("content") else None


def _extract_duration(soup, html: str) -> str | None:
    """Извлекает длительность видео из HTML"""
    patterns = [
        r'"duration"\s*:\s*"([^"]+)"',
        r'"duration"\s*:\s*(\d+)',
        r'content="PT(\d+H)?(\d+M)?(\d+S)?"',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(0)[:20]  # Ограничиваем длину
    return None


def _extract_video_url(soup, html: str, page_url: str, base: str) -> str:
    # 1. og:video
    og_video = _meta(soup, "og:video") or _meta(soup, "og:video:url")
    if og_video:
        return og_video

    # 2. <video src=...>
    video_tag = soup.find("video")
    if video_tag:
        src = video_tag.get("src") or ""
        if src:
            return urljoin(base, src)
        source = video_tag.find("source")
        if source and source.get("src"):
            return urljoin(base, source["src"])

    # 3. JSON-like паттерны в HTML (m3u8, mp4)
    patterns = [
        r'"(https?://[^"]+\.m3u8[^"]*)"',
        r'"(https?://[^"]+\.mp4[^"]*)"',
        r"'(https?://[^']+\.m3u8[^']*)'",
        r"'(https?://[^']+\.mp4[^']*)'",
        r'file:\s*["\']([^"\']+)["\']',
        r'src:\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)

    # 4. <iframe> — может быть embed
    iframe = soup.find("iframe")
    if iframe and iframe.get("src"):
        return iframe["src"]

    return ""


def generate_slug(text: str) -> str:
    """Генерирует URL-friendly slug из текста"""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text[:80]


def generate_seo_data(title: str, description: str, keywords: str = "") -> dict:
    """Генерирует SEO мета-теги для видео"""
    return {
        "meta_title": f"{title[:60]} - VideoStream CMS" if len(title) > 60 else f"{title} - VideoStream CMS",
        "meta_desc": description[:160] if description else "",
        "keywords": keywords[:500] if keywords else "",
        "slug": generate_slug(title),
    }


def parse_catalog_page(url: str, max_items: int = 20) -> list[dict]:
    """
    Парсит каталог/список видео со страницы.
    Возвращает список ссылок на видео.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    found = []

    # Ищем ссылки на страницы видео
    video_patterns = re.compile(
        r'/(video|watch|play|v|embed|film|movie|clip)/[\w\-]+', re.I
    )

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if video_patterns.search(href):
            full = urljoin(base, href)
            if full not in found:
                found.append(full)
        if len(found) >= max_items:
            break

    return found


# ─────────────────────────────────────────────
# API ENDPOINTS — АВТОРИЗАЦИЯ
# ─────────────────────────────────────────────

@app.route("/api/admin/login", methods=["POST"])
def api_login():
    """Вход в админ-панель"""
    data = request.json or {}
    username = data.get("username", "")
    password = data.get("password", "")
    
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    
    if username == ADMIN_USERNAME and password_hash == ADMIN_PASSWORD_HASH:
        session['logged_in'] = True
        session['username'] = username
        session['expires_at'] = (datetime.utcnow() + timedelta(hours=24)).isoformat()
        return jsonify({
            "success": True,
            "message": "Вход выполнен успешно",
            "username": username
        })
    
    return jsonify({"error": "Неверный логин или пароль"}), 401


@app.route("/api/admin/logout", methods=["POST"])
def api_logout():
    """Выход из админ-панели"""
    session.clear()
    return jsonify({"success": True})


@app.route("/api/admin/status", methods=["GET"])
def api_admin_status():
    """Проверка статуса авторизации"""
    if session.get('logged_in'):
        expires = session.get('expires_at', '')
        if expires and datetime.fromisoformat(expires) < datetime.utcnow():
            session.clear()
            return jsonify({"logged_in": False})
        return jsonify({
            "logged_in": True,
            "username": session.get('username')
        })
    return jsonify({"logged_in": False})


# ─────────────────────────────────────────────
# API ENDPOINTS — ВИДЕО
# ─────────────────────────────────────────────

@app.route("/api/parse", methods=["POST"])
@login_required
def api_parse():
    """Парсит одну страницу с видео и сохраняет в БД (только для авторизованных)"""
    data = request.json or {}
    url = data.get("url", "").strip()
    category_id = data.get("category_id")
    
    if not url:
        return jsonify({"error": "URL обязателен"}), 400

    result = parse_video_page(url)
    if "error" in result:
        return jsonify(result), 400

    # Генерируем SEO данные
    seo_data = generate_seo_data(result["title"], result["description"], result.get("keywords", ""))
    result.update(seo_data)

    # Сохраняем в БД
    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO videos
               (title, description, thumbnail, video_url, source_url, author, 
                duration, keywords, meta_title, meta_desc, og_image, slug, 
                category_id, parsed_at)
               VALUES (:title, :description, :thumbnail, :video_url, :source_url, :author,
                       :duration, :keywords, :meta_title, :meta_desc, :thumbnail, :slug,
                       ?, :parsed_at)""",
            {**result, "category_id": category_id}
        )
        db.commit()
        result["saved"] = True
        result["id"] = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception as e:
        result["saved"] = False
        result["db_error"] = str(e)
    finally:
        db.close()

    return jsonify(result)


@app.route("/api/parse-bulk", methods=["POST"])
@login_required
def api_parse_bulk():
    """Парсит каталог и добавляет все найденные видео (только для авторизованных)"""
    data = request.json or {}
    url = data.get("url", "").strip()
    max_items = int(data.get("max_items", 10))
    category_id = data.get("category_id")
    
    if not url:
        return jsonify({"error": "URL обязателен"}), 400

    links = parse_catalog_page(url, max_items)
    results = []
    success_count = 0
    
    for link in links:
        video_data = parse_video_page(link)
        if "error" not in video_data:
            seo_data = generate_seo_data(video_data["title"], video_data["description"])
            video_data.update(seo_data)
            
            db = get_db()
            try:
                db.execute(
                    """INSERT OR REPLACE INTO videos
                       (title, description, thumbnail, video_url, source_url, author,
                        duration, meta_title, meta_desc, og_image, slug, category_id, parsed_at)
                       VALUES (:title, :description, :thumbnail, :video_url, :source_url, :author,
                               :duration, :meta_title, :meta_desc, :thumbnail, :slug, ?, :parsed_at)""",
                    {**video_data, "category_id": category_id}
                )
                db.commit()
                success_count += 1
            except:
                pass
            finally:
                db.close()
        results.append(video_data)

    return jsonify({"parsed": len(results), "success": success_count, "videos": results})


@app.route("/api/videos", methods=["GET"])
def api_videos():
    """Список всех видео из БД с поиском и фильтрацией по категориям"""
    search = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    category_id = request.args.get("category_id", "").strip()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    offset = (page - 1) * per_page

    db = get_db()
    query = "SELECT * FROM videos WHERE status='published'"
    params = []

    if search:
        query += " AND (title LIKE ? OR description LIKE ? OR author LIKE ? OR keywords LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like, like]
    if category_id:
        query += " AND category_id = ?"
        params.append(category_id)
    elif category:
        # Поиск по slug категории
        cat = db.execute("SELECT id FROM categories WHERE slug=?", (category,)).fetchone()
        if cat:
            query += " AND category_id = ?"
            params.append(cat["id"])

    query += " ORDER BY added_at DESC LIMIT ? OFFSET ?"
    params += [per_page, offset]

    rows = db.execute(query, params).fetchall()
    
    # Получаем общее количество
    count_query = "SELECT COUNT(*) FROM videos WHERE status='published'"
    count_params = []
    if search:
        count_query += " AND (title LIKE ? OR description LIKE ? OR author LIKE ? OR keywords LIKE ?)"
        count_params += [f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"]
    count = db.execute(count_query, count_params).fetchone()[0]
    
    db.close()

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "videos": [dict(r) for r in rows],
    })


@app.route("/api/videos/<int:vid_id>", methods=["GET"])
def api_video(vid_id):
    """Получение одного видео с SEO данными"""
    db = get_db()
    row = db.execute("SELECT * FROM videos WHERE id=?", (vid_id,)).fetchone()
    
    if not row:
        db.close()
        return jsonify({"error": "Не найдено"}), 404
    
    # Увеличиваем счетчик просмотров
    db.execute("UPDATE videos SET views = views + 1 WHERE id=?", (vid_id,))
    db.commit()
    
    # Получаем категорию
    category = None
    if row["category_id"]:
        cat = db.execute("SELECT * FROM categories WHERE id=?", (row["category_id"],)).fetchone()
        if cat:
            category = dict(cat)
    
    db.close()
    
    video = dict(row)
    video["category"] = category
    
    # Генерируем OpenGraph теги
    video["og_tags"] = {
        "title": video.get("meta_title") or video["title"],
        "description": video.get("meta_desc") or video["description"],
        "image": video.get("og_image") or video["thumbnail"],
        "url": f"/video/{vid_id}",
        "type": "video.other"
    }
    
    return jsonify(video)


@app.route("/api/videos/<int:vid_id>", methods=["PUT"])
@login_required
def api_update_video(vid_id):
    """Обновление видео (для админки)"""
    data = request.json or {}
    db = get_db()
    
    fields = []
    values = []
    allowed_fields = ["title", "description", "thumbnail", "video_url", "category_id",
                      "meta_title", "meta_desc", "keywords", "status", "content_html"]
    
    for field in allowed_fields:
        if field in data:
            fields.append(f"{field} = ?")
            values.append(data[field])
    
    if not fields:
        db.close()
        return jsonify({"error": "Нет данных для обновления"}), 400
    
    values.append(datetime.utcnow().isoformat())
    fields.append("updated_at = ?")
    values.append(vid_id)
    
    query = f"UPDATE videos SET {', '.join(fields)} WHERE id=?"
    db.execute(query, values)
    db.commit()
    db.close()
    
    return jsonify({"success": True, "message": "Видео обновлено"})


@app.route("/api/videos/<int:vid_id>", methods=["DELETE"])
@login_required
def api_delete_video(vid_id):
    """Удаление видео (только для авторизованных)"""
    db = get_db()
    db.execute("DELETE FROM videos WHERE id=?", (vid_id,))
    db.commit()
    db.close()
    return jsonify({"deleted": True})


# ─────────────────────────────────────────────
# API ENDPOINTS — КАТЕГОРИИ
# ─────────────────────────────────────────────

@app.route("/api/categories", methods=["GET"])
def api_categories():
    """Список всех категорий"""
    db = get_db()
    rows = db.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    db.close()
    return jsonify({"categories": [dict(r) for r in rows]})


@app.route("/api/categories", methods=["POST"])
@login_required
def api_create_category():
    """Создание категории"""
    data = request.json or {}
    name = data.get("name", "").strip()
    slug = data.get("slug", "").strip() or generate_slug(name)
    description = data.get("description", "")
    parent_id = data.get("parent_id")
    
    if not name:
        return jsonify({"error": "Название категории обязательно"}), 400
    
    db = get_db()
    try:
        db.execute(
            "INSERT INTO categories (name, slug, description, parent_id) VALUES (?, ?, ?, ?)",
            (name, slug, description, parent_id)
        )
        db.commit()
        cat_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return jsonify({"success": True, "id": cat_id, "slug": slug})
    except sqlite3.IntegrityError:
        db.close()
        return jsonify({"error": "Категория с таким slug уже существует"}), 400


@app.route("/api/categories/<int:cat_id>", methods=["DELETE"])
@login_required
def api_delete_category(cat_id):
    """Удаление категории"""
    db = get_db()
    db.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    db.commit()
    db.close()
    return jsonify({"deleted": True})


# ─────────────────────────────────────────────
# API ENDPOINTS — РЕКЛАМА И НАСТРОЙКИ
# ─────────────────────────────────────────────

@app.route("/api/ads", methods=["GET"])
def api_ads():
    """Получение активных рекламных баннеров"""
    position = request.args.get("position", "")
    db = get_db()
    
    query = "SELECT * FROM ads WHERE is_active=1"
    params = []
    
    if position:
        query += " AND position = ?"
        params.append(position)
    
    query += " ORDER BY priority DESC, created_at DESC"
    
    rows = db.execute(query, params).fetchall()
    db.close()
    
    ads_list = []
    for row in rows:
        ad = dict(row)
        # Проверяем даты
        now = datetime.utcnow()
        if ad.get("start_date") and ad["start_date"] > now.isoformat():
            continue
        if ad.get("end_date") and ad["end_date"] < now.isoformat():
            continue
        ads_list.append(ad)
    
    return jsonify({"ads": ads_list})


@app.route("/api/ads", methods=["POST"])
@login_required
def api_create_ad():
    """Создание рекламного баннера"""
    data = request.json or {}
    required = ["position"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Поле {field} обязательно"}), 400
    
    db = get_db()
    db.execute(
        """INSERT INTO ads (position, image_url, link_url, html_code, is_active, priority, start_date, end_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get("position"),
            data.get("image_url"),
            data.get("link_url"),
            data.get("html_code"),
            data.get("is_active", 1),
            data.get("priority", 0),
            data.get("start_date"),
            data.get("end_date")
        )
    )
    db.commit()
    ad_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    
    return jsonify({"success": True, "id": ad_id})


@app.route("/api/ads/<int:ad_id>", methods=["PUT"])
@login_required
def api_update_ad(ad_id):
    """Обновление рекламного баннера"""
    data = request.json or {}
    db = get_db()
    
    fields = []
    values = []
    allowed_fields = ["position", "image_url", "link_url", "html_code", "is_active", "priority", "start_date", "end_date"]
    
    for field in allowed_fields:
        if field in data:
            fields.append(f"{field} = ?")
            values.append(data[field])
    
    if not fields:
        db.close()
        return jsonify({"error": "Нет данных для обновления"}), 400
    
    values.append(ad_id)
    query = f"UPDATE ads SET {', '.join(fields)} WHERE id=?"
    db.execute(query, values)
    db.commit()
    db.close()
    
    return jsonify({"success": True})


@app.route("/api/ads/<int:ad_id>", methods=["DELETE"])
@login_required
def api_delete_ad(ad_id):
    """Удаление рекламного баннера"""
    db = get_db()
    db.execute("DELETE FROM ads WHERE id=?", (ad_id,))
    db.commit()
    db.close()
    return jsonify({"deleted": True})


@app.route("/api/settings", methods=["GET"])
def api_settings():
    """Получение настроек сайта"""
    db = get_db()
    rows = db.execute("SELECT * FROM settings").fetchall()
    db.close()
    
    settings = {}
    for row in rows:
        key = row["key_name"]
        value = row["value"]
        if row["type"] == "boolean":
            value = value == "1"
        elif row["type"] == "number":
            value = int(value) if value.isdigit() else float(value)
        settings[key] = value
    
    return jsonify({"settings": settings})


@app.route("/api/settings", methods=["PUT"])
@login_required
def api_update_settings():
    """Обновление настроек сайта"""
    data = request.json or {}
    db = get_db()
    
    for key, value in data.items():
        db.execute(
            "UPDATE settings SET value = ?, updated_at = ? WHERE key_name = ?",
            (str(value), datetime.utcnow().isoformat(), key)
        )
    
    db.commit()
    db.close()
    return jsonify({"success": True})


# ─────────────────────────────────────────────
# API ENDPOINTS — СТАТИСТИКА
# ─────────────────────────────────────────────

@app.route("/api/admin/stats", methods=["GET"])
@login_required
def api_admin_stats():
    """Статистика для админ-панели"""
    db = get_db()
    
    total_videos = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    total_views = db.execute("SELECT SUM(views) FROM videos").fetchone()[0] or 0
    total_categories = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    active_ads = db.execute("SELECT COUNT(*) FROM ads WHERE is_active=1").fetchone()[0]
    
    # Топ видео по просмотрам
    top_videos = db.execute(
        "SELECT id, title, views FROM videos ORDER BY views DESC LIMIT 5"
    ).fetchall()
    
    db.close()
    
    return jsonify({
        "total_videos": total_videos,
        "total_views": total_views,
        "total_categories": total_categories,
        "active_ads": active_ads,
        "top_videos": [dict(v) for v in top_videos]
    })


# ─────────────────────────────────────────────
# ЗАПУСК ПРИЛОЖЕНИЯ
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("✅ База данных инициализирована")
    print("📁 Таблицы: videos, categories, settings, ads")
    print("🔐 Admin login: admin / admin123 (измените через ENV)")
    print("🚀 Сервер запущен: http://localhost:5000")
    print("\nAPI Endpoints:")
    print("  POST /api/admin/login - Вход")
    print("  GET  /api/videos - Список видео")
    print("  GET  /api/categories - Категории")
    print("  GET  /api/ads - Реклама")
    print("  POST /api/parse - Парсинг (требует авторизации)")
    app.run(debug=True, port=5000)
