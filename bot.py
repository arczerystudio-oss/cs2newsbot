import os
import re
import sqlite3
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import discord
from discord.ext import commands, tasks

import feedparser
import httpx
from dotenv import load_dotenv


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))
DB_PATH = os.getenv("DB_PATH", "rss_posts.sqlite3")

USER_AGENT = "CS2RU-RSS-Bot/1.3"

RSS_FEEDS = [
    "https://store.steampowered.com/news/app/730/?l=russian&format=rss",
    "https://www.goha.ru/rss/news",
    "https://procyber.me/feed/",
    "https://store.steampowered.com/feeds/news/app/730/?l=russian"
]

CS2_KEYWORDS = [
    "cs2", "cs 2", "counter-strike 2", "counter strike 2",
    "counter-strike", "counter strike", "cs:go", "csgo", "valve",
    "кс2", "кс 2", "контр-страйк", "контр страйк", "ксго", "вальв",
]

STEAM_730_HEADER = "https://cdn.cloudflare.steamstatic.com/steam/apps/730/header.jpg"

_cyr_re = re.compile(r"[А-Яа-яЁё]")
_letters_re = re.compile(r"[A-Za-zА-Яа-яЁё]")
_img_re = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_html(s: str) -> str:
    s = s or ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clip(s: str, limit: int) -> str:
    s = normalize_text(s)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def is_russian_text(text: str) -> bool:
    text = normalize_text(text)
    if not text:
        return False
    sample = text[:500]
    letters = len(_letters_re.findall(sample))
    if letters < 20:
        return bool(_cyr_re.search(sample))
    cyr = len(_cyr_re.findall(sample))
    return (cyr / max(1, letters)) >= 0.35


def is_cs2_related(text: str) -> bool:
    text = normalize_text(text).lower()
    if not text:
        return False
    return any(k in text for k in CS2_KEYWORDS)


def entry_best_url(entry) -> str:
    if getattr(entry, "link", None):
        return str(entry.link)
    links = getattr(entry, "links", None) or []
    if links and isinstance(links, list) and isinstance(links[0], dict) and "href" in links[0]:
        return str(links[0]["href"])
    return ""


def entry_uid(entry) -> str:
    uid = ""
    if getattr(entry, "id", None):
        uid = str(entry.id)
    if not uid and getattr(entry, "guid", None):
        uid = str(entry.guid)
    if not uid:
        uid = entry_best_url(entry)
    return normalize_text(uid)


def entry_published(entry) -> str:
    if getattr(entry, "published", None):
        return str(entry.published)
    if getattr(entry, "updated", None):
        return str(entry.updated)
    return ""


def entry_html(entry) -> str:
    if getattr(entry, "summary", None):
        return str(entry.summary)
    if getattr(entry, "description", None):
        return str(entry.description)
    return ""


def parse_pubdate(pub: str):
    pub = normalize_text(pub)
    if not pub:
        return None
    try:
        dt = parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _normalize_maybe_relative(u: str, base: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if u.startswith("steam://"):
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return urljoin(base, u)
    return u


def image_from_enclosures(entry) -> str:
    enclosures = getattr(entry, "enclosures", None) or []
    for enc in enclosures:
        if isinstance(enc, dict):
            u = enc.get("href") or enc.get("url") or ""
            t = (enc.get("type") or "").lower()
            if u and ("image" in t or u.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))):
                return str(u)

    media_content = getattr(entry, "media_content", None) or []
    for m in media_content:
        if isinstance(m, dict) and m.get("url"):
            return str(m["url"])

    media_thumbnail = getattr(entry, "media_thumbnail", None) or []
    for m in media_thumbnail:
        if isinstance(m, dict) and m.get("url"):
            return str(m["url"])

    return ""


def image_from_html(html: str, base_url: str) -> str:
    html = html or ""
    m = _img_re.search(html)
    if not m:
        return ""
    src = _normalize_maybe_relative(m.group(1), base_url)
    return src


class PostStore:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _init_db(self) -> None:
        con = sqlite3.connect(self.path)
        try:
            cur = con.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS posts ("
                "uid TEXT PRIMARY KEY,"
                "url TEXT NOT NULL,"
                "created_at TEXT NOT NULL"
                ")"
            )
            con.commit()
        finally:
            con.close()

    def seen(self, uid: str) -> bool:
        con = sqlite3.connect(self.path)
        try:
            cur = con.cursor()
            cur.execute("SELECT 1 FROM posts WHERE uid = ?", (uid,))
            return cur.fetchone() is not None
        finally:
            con.close()

    def add(self, uid: str, url: str) -> None:
        con = sqlite3.connect(self.path)
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO posts (uid, url, created_at) VALUES (?, ?, ?)",
                (uid, url, now_utc_iso()),
            )
            con.commit()
        finally:
            con.close()


class SourceView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Источник", url=url))


class RSSBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.store = PostStore(DB_PATH)

    async def setup_hook(self) -> None:
        self.rss_loop.start()

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} | {now_utc_iso()}")

    def get_target_channel(self):
        return self.get_channel(CHANNEL_ID)

    async def fetch_feed(self, url: str) -> feedparser.FeedParserDict:
        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            return feedparser.parse(r.text)

    async def post_entry(
        self,
        feed_title: str,
        source_url: str,
        title: str,
        url: str,
        published: str,
        summary: str,
        image_url: str,
    ):
        channel = self.get_target_channel()
        if channel is None:
            return

        embed = discord.Embed(
            title=clip(title, 256),
            description=clip(summary, 1800) if summary else None,
        )

        embed.set_author(name=clip(feed_title, 256))

        dt = parse_pubdate(published)
        if dt:
            embed.timestamp = dt

        if image_url:
            embed.set_image(url=image_url)

        view = SourceView(url)
        await channel.send(embed=embed, view=view)

    @tasks.loop(seconds=POLL_SECONDS)
    async def rss_loop(self) -> None:
        for feed_url in RSS_FEEDS:
            try:
                parsed = await self.fetch_feed(feed_url)
                feed_title = str(getattr(parsed.feed, "title", "") or feed_url)

                entries = getattr(parsed, "entries", []) or []
                entries = entries[:40]

                for entry in reversed(entries):
                    uid = entry_uid(entry)
                    url = normalize_text(entry_best_url(entry))
                    title = normalize_text(getattr(entry, "title", "") or "")
                    published = normalize_text(entry_published(entry))

                    html = entry_html(entry)
                    summary = strip_html(html)

                    if not uid or not url or not title:
                        continue

                    combined = (title + " " + summary).strip()

                    if not is_russian_text(combined):
                        continue

                    if not is_cs2_related(combined):
                        continue

                    if self.store.seen(uid):
                        continue

                    image_url = image_from_enclosures(entry)
                    if not image_url:
                        image_url = image_from_html(html, url)

                    image_url = _normalize_maybe_relative(image_url, url)

                    is_steam_730 = ("store.steampowered.com/news/app/730" in feed_url) or ("/news/app/730" in url)
                    if is_steam_730 and not image_url:
                        image_url = STEAM_730_HEADER

                    self.store.add(uid, url)

                    await self.post_entry(
                        feed_title=feed_title,
                        source_url=feed_url,
                        title=title,
                        url=url,
                        published=published,
                        summary=summary,
                        image_url=image_url,
                    )

                    await asyncio.sleep(1.0)

            except Exception as e:
                print(f"[{now_utc_iso()}] RSS error url={feed_url} err={e}")

    @rss_loop.before_loop
    async def before_rss_loop(self) -> None:
        await self.wait_until_ready()


bot = RSSBot()


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send("pong")


def validate_env() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    if CHANNEL_ID == 0:
        raise SystemExit("Missing CHANNEL_ID in .env")


if __name__ == "__main__":
    validate_env()
    bot.run(DISCORD_TOKEN)
