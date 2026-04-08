#!/usr/bin/env python3
"""
VideoParser Backend — парсер видео с внешних сайтов
Использование: python parser_backend.py
API запускается на http://localhost:5000
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import json
import sqlite3
import os
from datetime import datetime
from urllib.parse import urljoin, urlparse

app = Flask(__name__)
CORS(app)

DB_PATH = "videos.db"

# ─────────────────────────────────────────────
# БАЗА ДАННЫХ
# ─────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT,
            thumbnail   TEXT,
            video_url   TEXT,
            source_url  TEXT UNIQUE,
            duration    TEXT,
            views       TEXT,
            author      TEXT,
            category    TEXT,
            parsed_at   TEXT,
            added_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────
# ПАРСЕР
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
    Пытается извлечь: заголовок, описание, превью, прямую ссылку на видео.
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

    return {
        "title":       title,
        "description": description,
        "thumbnail":   thumbnail,
        "video_url":   video_url,
        "source_url":  url,
        "author":      author,
        "parsed_at":   datetime.utcnow().isoformat(),
    }


def _meta(soup, name: str) -> str | None:
    tag = (
        soup.find("meta", property=name)
        or soup.find("meta", attrs={"name": name})
    )
    return tag["content"].strip() if tag and tag.get("content") else None


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
# API ENDPOINTS
# ─────────────────────────────────────────────

@app.route("/api/parse", methods=["POST"])
def api_parse():
    """Парсит одну страницу с видео и сохраняет в БД."""
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL обязателен"}), 400

    result = parse_video_page(url)
    if "error" in result:
        return jsonify(result), 400

    # Сохраняем в БД
    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO videos
               (title, description, thumbnail, video_url, source_url, author, parsed_at)
               VALUES (:title, :description, :thumbnail, :video_url, :source_url, :author, :parsed_at)""",
            result,
        )
        db.commit()
        result["saved"] = True
    except Exception as e:
        result["saved"] = False
        result["db_error"] = str(e)
    finally:
        db.close()

    return jsonify(result)


@app.route("/api/parse-bulk", methods=["POST"])
def api_parse_bulk():
    """Парсит каталог и добавляет все найденные видео."""
    data = request.json or {}
    url = data.get("url", "").strip()
    max_items = int(data.get("max_items", 10))
    if not url:
        return jsonify({"error": "URL обязателен"}), 400

    links = parse_catalog_page(url, max_items)
    results = []
    for link in links:
        video_data = parse_video_page(link)
        if "error" not in video_data:
            db = get_db()
            try:
                db.execute(
                    """INSERT OR REPLACE INTO videos
                       (title, description, thumbnail, video_url, source_url, author, parsed_at)
                       VALUES (:title, :description, :thumbnail, :video_url, :source_url, :author, :parsed_at)""",
                    video_data,
                )
                db.commit()
            except:
                pass
            finally:
                db.close()
        results.append(video_data)

    return jsonify({"parsed": len(results), "videos": results})


@app.route("/api/videos", methods=["GET"])
def api_videos():
    """Список всех видео из БД с поиском."""
    search = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    offset = (page - 1) * per_page

    db = get_db()
    query = "SELECT * FROM videos WHERE 1=1"
    params = []

    if search:
        query += " AND (title LIKE ? OR description LIKE ? OR author LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like]
    if category:
        query += " AND category = ?"
        params.append(category)

    query += " ORDER BY added_at DESC LIMIT ? OFFSET ?"
    params += [per_page, offset]

    rows = db.execute(query, params).fetchall()
    count = db.execute(
        "SELECT COUNT(*) FROM videos WHERE 1=1" + (
            " AND (title LIKE ? OR description LIKE ? OR author LIKE ?)"
            if search else ""
        ),
        [f"%{search}%", f"%{search}%", f"%{search}%"] if search else []
    ).fetchone()[0]
    db.close()

    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "videos": [dict(r) for r in rows],
    })


@app.route("/api/videos/<int:vid_id>", methods=["GET"])
def api_video(vid_id):
    db = get_db()
    row = db.execute("SELECT * FROM videos WHERE id=?", (vid_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({"error": "Не найдено"}), 404
    return jsonify(dict(row))


@app.route("/api/videos/<int:vid_id>", methods=["DELETE"])
def api_delete_video(vid_id):
    db = get_db()
    db.execute("DELETE FROM videos WHERE id=?", (vid_id,))
    db.commit()
    db.close()
    return jsonify({"deleted": True})


if __name__ == "__main__":
    init_db()
    print("✅ База данных инициализирована")
    print("🚀 Сервер запущен: http://localhost:5000")
    app.run(debug=True, port=5000)
