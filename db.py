import aiosqlite
import sqlite3

DB_PATH = "bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            assistant_channel_id INTEGER
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            guild_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, day)
        )
        """)

        # حماية السيرفر
        await db.execute("""
        CREATE TABLE IF NOT EXISTS guild_protection (
            guild_id INTEGER PRIMARY KEY,
            log_channel_id INTEGER,
            links_enabled INTEGER NOT NULL DEFAULT 1,
            links_mode TEXT NOT NULL DEFAULT 'invites',      -- invites | all (Premium)
            spam_enabled INTEGER NOT NULL DEFAULT 1,
            spam_max INTEGER NOT NULL DEFAULT 6,
            spam_window INTEGER NOT NULL DEFAULT 10,
            words_enabled INTEGER NOT NULL DEFAULT 1,
            mention_enabled INTEGER NOT NULL DEFAULT 1,
            mention_limit INTEGER NOT NULL DEFAULT 6,
            timeout_seconds INTEGER NOT NULL DEFAULT 60,
            roles_enabled INTEGER NOT NULL DEFAULT 0         -- Premium enforcement
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS banned_words (
            guild_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            PRIMARY KEY (guild_id, word)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS allowed_domains (
            guild_id INTEGER NOT NULL,
            domain TEXT NOT NULL,
            PRIMARY KEY (guild_id, domain)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS bypass_roles (
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, role_id)
        )
        """)

        await db.commit()

# ====== مساعد AI ======
async def set_assistant_channel(guild_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO guild_config (guild_id, assistant_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET assistant_channel_id=excluded.assistant_channel_id
        """, (guild_id, channel_id))
        await db.commit()

async def get_assistant_channel(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT assistant_channel_id FROM guild_config WHERE guild_id=?",
            (guild_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

async def get_daily_usage(guild_id: int, day: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT count FROM daily_usage WHERE guild_id=? AND day=?",
            (guild_id, day)
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def increment_daily_usage(guild_id: int, day: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "INSERT INTO daily_usage (guild_id, day, count) VALUES (?, ?, 1)",
                (guild_id, day)
            )
        except sqlite3.IntegrityError:
            await db.execute(
                "UPDATE daily_usage SET count = count + 1 WHERE guild_id=? AND day=?",
                (guild_id, day)
            )

        cur = await db.execute(
            "SELECT count FROM daily_usage WHERE guild_id=? AND day=?",
            (guild_id, day)
        )
        row = await cur.fetchone()
        await db.commit()
        return int(row[0]) if row else 0

# ====== حماية السيرفر ======
async def ensure_protection_row(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO guild_protection (guild_id) VALUES (?)", (guild_id,))
        await db.commit()

async def get_protection_config(guild_id: int) -> dict:
    await ensure_protection_row(guild_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM guild_protection WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row)) if row else {}

async def update_protection_config(guild_id: int, **fields):
    await ensure_protection_row(guild_id)
    if not fields:
        return
    keys = list(fields.keys())
    values = [fields[k] for k in keys]
    set_sql = ", ".join([f"{k}=?" for k in keys])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE guild_protection SET {set_sql} WHERE guild_id=?", (*values, guild_id))
        await db.commit()

async def list_banned_words(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT word FROM banned_words WHERE guild_id=? ORDER BY word", (guild_id,))
        return [r[0] for r in await cur.fetchall()]

async def add_banned_word(guild_id: int, word: str):
    w = word.strip().lower()
    if not w:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)", (guild_id, w))
        await db.commit()

async def remove_banned_word(guild_id: int, word: str):
    w = word.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM banned_words WHERE guild_id=? AND word=?", (guild_id, w))
        await db.commit()

async def list_allowed_domains(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT domain FROM allowed_domains WHERE guild_id=? ORDER BY domain", (guild_id,))
        return [r[0] for r in await cur.fetchall()]

async def add_allowed_domain(guild_id: int, domain: str):
    d = domain.strip().lower().replace("https://", "").replace("http://", "")
    d = d.split("/")[0].replace("www.", "")
    if not d:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO allowed_domains (guild_id, domain) VALUES (?, ?)", (guild_id, d))
        await db.commit()

async def remove_allowed_domain(guild_id: int, domain: str):
    d = domain.strip().lower().replace("https://", "").replace("http://", "")
    d = d.split("/")[0].replace("www.", "")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM allowed_domains WHERE guild_id=? AND domain=?", (guild_id, d))
        await db.commit()

async def list_bypass_roles(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role_id FROM bypass_roles WHERE guild_id=? ORDER BY role_id", (guild_id,))
        return [int(r[0]) for r in await cur.fetchall()]

async def add_bypass_role(guild_id: int, role_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO bypass_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
        await db.commit()

async def remove_bypass_role(guild_id: int, role_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bypass_roles WHERE guild_id=? AND role_id=?", (guild_id, role_id))
        await db.commit()
