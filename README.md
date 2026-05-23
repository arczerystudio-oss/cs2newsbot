# CS2 News Bot

A Discord bot that monitors RSS feeds and automatically posts Counter-Strike 2 news to a channel. Filters for Russian-language content and CS2 relevance, deduplicates with SQLite, and formats posts as clean embeds with images and source links.

## What it does

- Polls 4 RSS feeds every 3 minutes (Steam CS2, Goha.ru, Procyber.me)
- Filters entries for Cyrillic content and CS2 keywords (`cs2`, `counter-strike`, `кс2`, etc.)
- Extracts images from enclosures, media tags, or inline HTML
- Posts Discord embeds with title, summary, timestamp, and a clickable source button
- Tracks posted articles in SQLite so nothing gets reposted

## Setup

```bash
pip install -r requirements.txt
```

Set environment variables:

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your bot token from the Discord developer portal |
| `CHANNEL_ID` | ID of the channel to post news into |
| `POLL_SECONDS` | How often to check feeds (default: 180) |
| `DB_PATH` | Path to the SQLite database file (default: `posts.db`) |

## Run

```bash
python bot.py
```

## Stack

- Python
- discord.py
- feedparser
- SQLite
