"""
database.py — полная схема БД с защитой от риелторов
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        -- Объявления
        CREATE TABLE IF NOT EXISTS listings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source        TEXT NOT NULL,
            external_id   TEXT NOT NULL,
            url           TEXT,
            title         TEXT,
            description   TEXT,
            price_usd     INTEGER,
            district      TEXT,
            rooms         INTEGER,
            area_sqm      REAL,
            floor         TEXT,
            phone         TEXT,
            phone_norm    TEXT,           -- нормализованный номер для дедупликации
            is_owner      INTEGER,        -- 1/0/NULL
            owner_score   REAL,
            owner_reason  TEXT,
            photo_hashes  TEXT,           -- JSON-список pHash строк фото
            raw_json      TEXT,
            created_at    TEXT DEFAULT (datetime('now')),
            notified      INTEGER DEFAULT 0,
            UNIQUE(source, external_id)
        );

        -- Глобальный реестр хэшей фото
        CREATE TABLE IF NOT EXISTS photo_hashes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            hash          TEXT NOT NULL,
            listing_id    INTEGER NOT NULL,
            source        TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_photo_hash ON photo_hashes(hash);

        -- Пользователи
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY,
            username      TEXT,
            first_name    TEXT,
            active        INTEGER DEFAULT 1,
            banned        INTEGER DEFAULT 0,
            ban_reason    TEXT,
            listing_count INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now'))
        );

        -- Подписки покупателей
        CREATE TABLE IF NOT EXISTS subscriptions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id),
            district      TEXT,
            rooms_min     INTEGER DEFAULT 1,
            rooms_max     INTEGER DEFAULT 10,
            price_min     INTEGER DEFAULT 0,
            price_max     INTEGER DEFAULT 9999999,
            active        INTEGER DEFAULT 1,
            created_at    TEXT DEFAULT (datetime('now'))
        );

        -- Реестр телефонов (1 телефон = 1 собственник)
        CREATE TABLE IF NOT EXISTS phone_registry (
            phone_norm    TEXT PRIMARY KEY,
            listing_id    INTEGER NOT NULL,
            user_id       INTEGER,
            created_at    TEXT DEFAULT (datetime('now'))
        );

        -- Жалобы на объявления
        CREATE TABLE IF NOT EXISTS complaints (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id    INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            reason        TEXT DEFAULT 'realtor',
            created_at    TEXT DEFAULT (datetime('now')),
            UNIQUE(listing_id, user_id)
        );

        -- Очередь на ручную модерацию
        CREATE TABLE IF NOT EXISTS modqueue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id    INTEGER NOT NULL UNIQUE,
            reason        TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        );

        -- Уведомления
        CREATE TABLE IF NOT EXISTS notifications (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            listing_id    INTEGER NOT NULL,
            sent_at       TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, listing_id)
        );

        -- Фото объявлений (file_id для Telegram)
        CREATE TABLE IF NOT EXISTS listing_photos (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id    INTEGER NOT NULL,
            file_id       TEXT NOT NULL,
            phash         TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_listings_owner   ON listings(is_owner, notified);
        CREATE INDEX IF NOT EXISTS idx_listings_phone   ON listings(phone_norm);
        CREATE INDEX IF NOT EXISTS idx_subs_user        ON subscriptions(user_id, active);
        CREATE INDEX IF NOT EXISTS idx_complaints_lst   ON complaints(listing_id);
        """)
    print("✅ БД инициализирована")


# ── Listings ──────────────────────────────────────────────────────────────

def save_listing(data: dict) -> int | None:
    sql = """
        INSERT OR IGNORE INTO listings
            (source, external_id, url, title, description,
             price_usd, district, rooms, area_sqm, floor,
             phone, phone_norm, raw_json)
        VALUES
            (:source,:external_id,:url,:title,:description,
             :price_usd,:district,:rooms,:area_sqm,:floor,
             :phone,:phone_norm,:raw_json)
    """
    with get_conn() as conn:
        cur = conn.execute(sql, {
            "source":      data.get("source",""),
            "external_id": data.get("external_id",""),
            "url":         data.get("url",""),
            "title":       data.get("title",""),
            "description": data.get("description",""),
            "price_usd":   data.get("price_usd"),
            "district":    data.get("district",""),
            "rooms":       data.get("rooms"),
            "area_sqm":    data.get("area_sqm"),
            "floor":       data.get("floor",""),
            "phone":       data.get("phone",""),
            "phone_norm":  data.get("phone_norm",""),
            "raw_json":    json.dumps(data, ensure_ascii=False),
        })
        return cur.lastrowid if cur.rowcount else None


def update_classification(listing_id: int, is_owner: bool,
                          score: float, reason: str,
                          photo_hashes: list[str] | None = None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE listings
               SET is_owner=?, owner_score=?, owner_reason=?, photo_hashes=?
               WHERE id=?""",
            (1 if is_owner else 0, score, reason,
             json.dumps(photo_hashes or []), listing_id)
        )


def get_unclassified(limit: int = 30) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM listings WHERE owner_score IS NULL LIMIT ?", (limit,)
        ).fetchall()


def get_new_owner_listings() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM listings WHERE is_owner=1 AND notified=0 ORDER BY created_at DESC"
        ).fetchall()


def mark_notified(listing_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE listings SET notified=1 WHERE id=?", (listing_id,))


def pull_to_modqueue(listing_id: int, reason: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO modqueue(listing_id, reason) VALUES(?,?)",
            (listing_id, reason)
        )
        conn.execute("UPDATE listings SET is_owner=NULL WHERE id=?", (listing_id,))


def get_modqueue() -> list:
    with get_conn() as conn:
        return conn.execute(
            """SELECT m.*, l.title, l.description, l.phone, l.url, l.price_usd,
                      l.district, l.rooms, l.area_sqm, l.owner_score, l.owner_reason
               FROM modqueue m JOIN listings l ON m.listing_id=l.id
               ORDER BY m.created_at ASC LIMIT 20"""
        ).fetchall()


def approve_listing(listing_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE listings SET is_owner=1, owner_score=0.8 WHERE id=?", (listing_id,))
        conn.execute("DELETE FROM modqueue WHERE listing_id=?", (listing_id,))


def reject_listing(listing_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE listings SET is_owner=0 WHERE id=?", (listing_id,))
        conn.execute("DELETE FROM modqueue WHERE listing_id=?", (listing_id,))


# ── Пользователи ──────────────────────────────────────────────────────────

def save_user(user_id: int, username: str, first_name: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(id,username,first_name) VALUES(?,?,?)",
            (user_id, username, first_name)
        )


def is_user_banned(user_id: int) -> tuple[bool, str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT banned, ban_reason FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if row and row["banned"]:
            return True, row["ban_reason"] or ""
        return False, ""


def ban_user(user_id: int, reason: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET banned=1, ban_reason=? WHERE id=?", (reason, user_id)
        )


def get_user_listing_count(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE source='owner_direct' "
            "AND external_id LIKE 'tg_'||?||'_%'", (user_id,)
        ).fetchone()
        return row[0] if row else 0


# ── Телефонный реестр ─────────────────────────────────────────────────────

def check_phone_duplicate(phone_norm: str) -> int | None:
    """Возвращает listing_id первого объявления с этим телефоном или None."""
    if not phone_norm:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT listing_id FROM phone_registry WHERE phone_norm=?", (phone_norm,)
        ).fetchone()
        return row["listing_id"] if row else None


def register_phone(phone_norm: str, listing_id: int, user_id: int | None = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO phone_registry(phone_norm,listing_id,user_id) VALUES(?,?,?)",
            (phone_norm, listing_id, user_id)
        )


# ── pHash реестр ──────────────────────────────────────────────────────────

def find_similar_photos(hashes: list[str], threshold: int = 8) -> list[dict]:
    """
    Ищет похожие фото в базе.
    threshold: максимальное расстояние Хэмминга (0=идентично, >10=разные).
    """
    if not hashes:
        return []
    matches = []
    with get_conn() as conn:
        all_hashes = conn.execute(
            "SELECT hash, listing_id FROM photo_hashes"
        ).fetchall()
    for new_hash in hashes:
        for row in all_hashes:
            dist = hamming_distance(new_hash, row["hash"])
            if dist <= threshold:
                matches.append({
                    "existing_listing_id": row["listing_id"],
                    "distance": dist,
                    "new_hash": new_hash,
                    "existing_hash": row["hash"],
                })
    return matches


def register_photo_hashes(listing_id: int, hashes: list[str], source: str = ""):
    with get_conn() as conn:
        for h in hashes:
            conn.execute(
                "INSERT OR IGNORE INTO photo_hashes(hash,listing_id,source) VALUES(?,?,?)",
                (h, listing_id, source)
            )


def hamming_distance(h1: str, h2: str) -> int:
    """Расстояние Хэмминга между двумя hex-строками pHash."""
    try:
        n1, n2 = int(h1, 16), int(h2, 16)
        x = n1 ^ n2
        dist = 0
        while x:
            dist += x & 1
            x >>= 1
        return dist
    except (ValueError, TypeError):
        return 999


# ── Жалобы ────────────────────────────────────────────────────────────────

def add_complaint(listing_id: int, user_id: int) -> int:
    """Добавляет жалобу. Возвращает общее кол-во жалоб на объявление."""
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO complaints(listing_id,user_id) VALUES(?,?)",
                (listing_id, user_id)
            )
        except sqlite3.IntegrityError:
            pass  # уже жаловался
        count = conn.execute(
            "SELECT COUNT(*) FROM complaints WHERE listing_id=?", (listing_id,)
        ).fetchone()[0]
    return count


def get_complaint_count(listing_id: int) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM complaints WHERE listing_id=?", (listing_id,)
        ).fetchone()[0]


# ── Подписки ──────────────────────────────────────────────────────────────

def add_subscription(user_id: int, params: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO subscriptions
               (user_id,district,rooms_min,rooms_max,price_min,price_max)
               VALUES(?,?,?,?,?,?)""",
            (user_id, params.get("district"),
             params.get("rooms_min",1), params.get("rooms_max",10),
             params.get("price_min",0), params.get("price_max",9_999_999))
        )
        return cur.lastrowid


def get_active_subscriptions() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT s.*,u.id as uid FROM subscriptions s "
            "JOIN users u ON s.user_id=u.id WHERE s.active=1 AND u.banned=0"
        ).fetchall()


def get_user_subscriptions(user_id: int) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND active=1", (user_id,)
        ).fetchall()


def deactivate_subscription(sub_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET active=0 WHERE id=? AND user_id=?",
            (sub_id, user_id)
        )


def listing_matches_sub(listing, sub) -> bool:
    if sub["district"] and listing["district"]:
        if sub["district"].lower() not in listing["district"].lower():
            return False
    if listing["rooms"]:
        if not (sub["rooms_min"] <= listing["rooms"] <= sub["rooms_max"]):
            return False
    if listing["price_usd"]:
        if not (sub["price_min"] <= listing["price_usd"] <= sub["price_max"]):
            return False
    return True


def already_notified(user_id: int, listing_id: int) -> bool:
    with get_conn() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM notifications WHERE user_id=? AND listing_id=?",
            (user_id, listing_id)
        ).fetchone())


def record_notification(user_id: int, listing_id: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notifications(user_id,listing_id) VALUES(?,?)",
            (user_id, listing_id)
        )


# ── Статистика ────────────────────────────────────────────────────────────

def get_stats() -> dict:
    with get_conn() as conn:
        def q(sql): return conn.execute(sql).fetchone()[0]
        return {
            "total":        q("SELECT COUNT(*) FROM listings"),
            "owners":       q("SELECT COUNT(*) FROM listings WHERE is_owner=1"),
            "direct":       q("SELECT COUNT(*) FROM listings WHERE source='owner_direct' AND is_owner=1"),
            "realtors":     q("SELECT COUNT(*) FROM listings WHERE is_owner=0"),
            "unclassified": q("SELECT COUNT(*) FROM listings WHERE owner_score IS NULL"),
            "modqueue":     q("SELECT COUNT(*) FROM modqueue"),
            "users":        q("SELECT COUNT(*) FROM users WHERE active=1"),
            "subscriptions":q("SELECT COUNT(*) FROM subscriptions WHERE active=1"),
            "complaints":   q("SELECT COUNT(*) FROM complaints"),
            "photo_hashes": q("SELECT COUNT(*) FROM photo_hashes"),
        }
