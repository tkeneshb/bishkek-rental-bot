# Bishkek Rental Bot

A Telegram bot that collects apartment listings from House.kg and Lalafo,
filters out the realtors, and sends buyers only the ones posted by actual
owners.

I built this because I got tired: every second listing on these sites is
from an agency, and the "owners only" filter the sites provide doesn't
actually work. I wanted to see if it was possible to pull the real owners
out of that flood, and also to try an LLM on a real task instead of a toy
one.

## What's inside

A parser runs every N minutes against House.kg and Lalafo, grabs new
listings, and stores them in SQLite. Each listing then goes through seven
layers of checks; some block immediately, some send it to a moderation
queue:

1. **Claude reads the text** and decides whether it was written by an
   owner or a realtor. Returns `is_owner`, `confidence`, `reason`. If
   confidence is ≥ 0.85 and it's flagged as a realtor, it's blocked.
   Between 0.5 and 0.85 it goes to the moderator, because falsely
   blocking a real owner is worse than letting a realtor through.
2. **Per-account limit**: max 2 active listings per Telegram user.
3. **24-hour cooldown** between listings from the same account.
4. **Phone deduplication**: one number = one listing. Numbers are
   normalised to the form `996XXXXXXXXX` before comparison.
5. **pHash on photos**: I implemented DCT-based hashing from scratch
   using Pillow. The image is downscaled to 32×32 grayscale, a 2D DCT
   is applied, the top-left 8×8 block is taken, giving a 64-bit
   fingerprint. Comparison via Hamming distance, threshold 8. Distance
   ≤ 3 is a hard block (clearly the same photos); 4 to 8 sends it to
   moderation. DCT is resistant to resize, compression, and brightness
   changes, so realtors can't just re-export the image and call it new.
6. **Buyer complaints**: a "this is a realtor" button under every
   listing. After 3 complaints it goes to the queue automatically.
7. **Manual moderation**: `/modqueue` for admins, where all uncertain
   cases sit.

Buyers subscribe to a district + price + room range, and the bot DMs
them new listings as soon as they pass all the checks.

## Stack

- Python 3.11, asyncio
- `python-telegram-bot` for the bot
- `anthropic`: Claude Haiku for classification (fast and cheap enough
  to run on every listing)
- `httpx` + `BeautifulSoup`: scraping
- `Pillow`: pHash (DCT written by hand, no `imagehash` library)
- SQLite with WAL mode

I deliberately avoided heavy dependencies like PyTorch or OpenCV;
wanted it to fit on a cheap VPS.

## Run

```bash
cp .env.example .env
# fill in your TELEGRAM_TOKEN and ANTHROPIC_API_KEY

pip install -r requirements.txt
python bot.py
```

The parser runs separately, not from inside the bot:

```bash
python -c "import asyncio; from parser import run_pipeline; asyncio.run(run_pipeline(pages=3))"
```

I usually run it from cron every 30 minutes.

## Structure

```
bot.py          : Telegram bot, all command handlers and dialog states
parser.py       : House.kg and Lalafo parsers + full pipeline
defense.py      : the seven layers of checks, including the Claude call
photo_guard.py  : pHash via Pillow and DCT, Hamming distance
database.py     : SQLite schema and all DB queries
```

## What doesn't work well

- The parser selectors rely on CSS classes. House.kg and Lalafo
  occasionally change their markup, and the parser silently starts
  returning 0 listings. I need a monitor that tracks "how many listings
  did we pull in the last hour" and alerts when it drops to zero.
- Claude sometimes tags real owners as realtors when they write in a
  dry, matter-of-fact style. I rewrote the prompt three times, it's
  still not perfect. Probably needs to be fine-tuned on collected
  examples, but that's a separate project.
- SQLite handles the current load fine, but at thousands of listings
  per day it'll hit write-lock issues. PostgreSQL is the next step.
- pHash catches identical photos well, but doesn't understand "this is
  the same apartment from a different angle". For that you'd need an
  actual computer vision model, not a hash.

## Why I did this

My Master's thesis is on fault-tolerant distributed task scheduling;
that's academic work, no one in production ever sees it. This bot is
the opposite: small, but real. Someone actually uses it, and you can
see the live failures. I wanted to feel the gap between "the system
works on tests" and "the system works for people". It turned out the
technical problems were the smaller half. The harder ones were
organisational: where to draw the line between a hard block and the
moderation queue, who owns a false positive, how to build enough trust
in an automated decision that users don't work around it. That's more
interesting than the algorithm itself.

## License

MIT. Do whatever you want with it.
