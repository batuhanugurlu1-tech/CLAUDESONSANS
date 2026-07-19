"""
POLYMARKET 5m UP/DOWN QUOTE TOPLAYICI - H3 icin (Coinbase collector'in kardesi)

Ne yapar:
  - Her an aktif olan BTC/ETH 5m updown market'ini kesfeder
    (slug = "{asset}-updown-5m-{epoch}", epoch 300sn'ye hizali)
  - Gamma API'den Up token id'sini cozer (pencere basina 1 kez, cache'li)
  - CLOB /book'tan Up token best bid/ask ceker (~1.5sn/urun)
  - Ayni Postgres'e poly_quotes tablosuna yazar (Coinbase collector'la ayni DB)

Mevcut Coinbase collector'a DOKUNMAZ - ayri servis, ayri advisory lock.
Salt-okunur, hicbir emir/hesap baglantisi yok.

Env: DATABASE_URL (Railway Postgres - Coinbase collector'la AYNI degisken),
     PORT (health icin), POLL_INTERVAL_SEC (ops., default 1.5)
Deploy: ayni Railway projesine IKINCI servis olarak ekle, ayni Postgres'i bagla.
"""

import os
import sys
import time
import json
import signal
import logging
import threading
from datetime import datetime, timezone

import requests
import psycopg2
import uvicorn
from fastapi import FastAPI

ASSETS = ["btc", "eth"]
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WINDOW_SEC = 300
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "1.5"))
DATABASE_URL = os.environ.get("DATABASE_URL")
ADVISORY_LOCK_KEY = 872341002  # Coinbase collector'inkinden FARKLI

if not DATABASE_URL:
    print("FATAL: DATABASE_URL yok.", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s UTC [%(levelname)s] %(message)s")
logging.Formatter.converter = time.gmtime
log = logging.getLogger("poly")

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "poly-updown-quote-collector/1.0"})

SHUTDOWN = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: SHUTDOWN.set())
signal.signal(signal.SIGINT, lambda *a: SHUTDOWN.set())

STATS = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "quotes": {a: 0 for a in ASSETS},
    "windows_resolved": {a: 0 for a in ASSETS},
    "discovery_failures": 0,
    "last_quote_ts": {a: None for a in ASSETS},
}


class RateLimiter:
    def __init__(self, rate_per_sec, burst):
        self.rate = rate_per_sec
        self.tokens = float(burst)
        self.burst = float(burst)
        self.last = time.monotonic()

    def wait(self):
        while True:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            time.sleep((1 - self.tokens) / self.rate)


RL = RateLimiter(rate_per_sec=3.0, burst=6)

DB_CONN = None
LOCK_CONN = None


def db_connect():
    c = psycopg2.connect(DATABASE_URL)
    c.autocommit = True
    return c


def acquire_lock():
    global LOCK_CONN
    LOCK_CONN = db_connect()
    cur = LOCK_CONN.cursor()
    for i in range(10):
        cur.execute("SELECT pg_try_advisory_lock(%s);", (ADVISORY_LOCK_KEY,))
        if cur.fetchone()[0]:
            log.info("Advisory lock alindi.")
            return
        time.sleep(5)
    log.error("Lock alinamadi - baska instance calisiyor. Cikis.")
    sys.exit(1)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS poly_quotes (
                id BIGSERIAL PRIMARY KEY,
                ts_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
                asset TEXT NOT NULL,
                window_start BIGINT NOT NULL,
                slug TEXT NOT NULL,
                up_token TEXT NOT NULL,
                best_bid NUMERIC,
                best_ask NUMERIC,
                bid_size NUMERIC,
                ask_size NUMERIC
            );
            CREATE INDEX IF NOT EXISTS idx_polyq_asset_ts ON poly_quotes (asset, ts_utc);
            CREATE INDEX IF NOT EXISTS idx_polyq_window ON poly_quotes (asset, window_start);
            CREATE TABLE IF NOT EXISTS poly_events (
                id BIGSERIAL PRIMARY KEY,
                ts_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
                asset TEXT,
                event_type TEXT NOT NULL,
                detail TEXT
            );
        """)
    log.info("Schema hazir (poly_quotes, poly_events).")


def log_event(conn, asset, etype, detail=""):
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO poly_events (asset, event_type, detail) VALUES (%s,%s,%s);",
                        (asset, etype, detail))
    except Exception as e:
        log.error(f"poly_events yazilamadi: {e}")


def http_json(url, params=None, retries=4, base_delay=0.5):
    delay = base_delay
    for _ in range(retries):
        if SHUTDOWN.is_set():
            return None
        RL.wait()
        try:
            r = HTTP.get(url, params=params, timeout=8)
            if r.status_code == 200:
                return r.json()
            log.warning(f"HTTP {r.status_code}: {url}")
        except requests.exceptions.RequestException as e:
            log.warning(f"Req hatasi: {url} - {e}")
        time.sleep(delay)
        delay = min(delay * 2, 15)
    return None


# --- market kesfi ---------------------------------------------------------
# cache: {asset: (window_start, up_token, slug)}
CURRENT = {}


def current_window_start(now=None):
    t = int(now if now is not None else time.time())
    return (t // WINDOW_SEC) * WINDOW_SEC


def discover(asset, wstart, conn):
    """Slug'dan Up token id'sini cozer. Basarisizsa None."""
    slug = f"{asset}-updown-5m-{wstart}"
    data = http_json(f"{GAMMA}/markets", params={"slug": slug})
    if not data:
        STATS["discovery_failures"] += 1
        return None
    m = data[0] if isinstance(data, list) and data else None
    if not m:
        STATS["discovery_failures"] += 1
        return None
    try:
        token_ids = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
        outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
        # "Up" outcome'un indexini bul; bulunamazsa index 0 varsay + logla
        idx = 0
        if outcomes:
            for i, o in enumerate(outcomes):
                if str(o).lower() == "up":
                    idx = i
                    break
            else:
                log_event(conn, asset, "outcome_assumption", f"'Up' bulunamadi, outcomes={outcomes}, idx=0 varsayildi")
        up_token = token_ids[idx]
        log.info(f"[{asset}] pencere {wstart}: slug={slug} up_token={str(up_token)[:16]}...")
        return (wstart, str(up_token), slug)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        log.warning(f"[{asset}] kesif parse hatasi: {e}")
        STATS["discovery_failures"] += 1
        return None


def get_book_top(token_id):
    """CLOB /book -> (best_bid, best_ask, bid_sz, ask_sz). Yoksa None'lar."""
    data = http_json(f"{CLOB}/book", params={"token_id": token_id})
    if not data:
        return None
    try:
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        # CLOB book sirasi garantili degil -> en iyi seviyeyi KENDIMIZ secelim
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        bid_sz = next((float(b["size"]) for b in bids if float(b["price"]) == best_bid), None) if best_bid is not None else None
        ask_sz = next((float(a["size"]) for a in asks if float(a["price"]) == best_ask), None) if best_ask is not None else None
        return (best_bid, best_ask, bid_sz, ask_sz)
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"book parse hatasi: {e}")
        return None


def insert_quote(conn, asset, wstart, slug, token, top):
    best_bid, best_ask, bid_sz, ask_sz = top
    if best_bid is not None and best_ask is not None and best_ask < best_bid:
        log.warning(f"[{asset}] crossed book atlandi: bid={best_bid} ask={best_ask}")
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO poly_quotes (asset, window_start, slug, up_token, best_bid, best_ask, bid_size, ask_size)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s);""",
            (asset, wstart, slug, token, best_bid, best_ask, bid_sz, ask_sz))
    STATS["quotes"][asset] += 1
    STATS["last_quote_ts"][asset] = datetime.now(timezone.utc).isoformat()


def with_db(fn, *a):
    global DB_CONN
    for i in range(3):
        try:
            return fn(DB_CONN, *a)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            log.error(f"DB hatasi: {e} - reconnect {i+1}/3")
            try:
                DB_CONN.close()
            except Exception:
                pass
            time.sleep(2 * (i + 1))
            try:
                DB_CONN = db_connect()
            except Exception as e2:
                log.error(f"reconnect basarisiz: {e2}")
    return None


app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok", **STATS}


def run_health():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")


def main():
    global DB_CONN
    log.info("POLYMARKET 5m UPDOWN QUOTE TOPLAYICI v1")
    acquire_lock()
    DB_CONN = db_connect()
    ensure_schema(DB_CONN)
    threading.Thread(target=run_health, daemon=True).start()

    last_hb = time.time()
    while not SHUTDOWN.is_set():
        wstart = current_window_start()
        for asset in ASSETS:
            if SHUTDOWN.is_set():
                break
            cur = CURRENT.get(asset)
            if cur is None or cur[0] != wstart:
                found = discover(asset, wstart, DB_CONN)
                if found:
                    if cur is not None:
                        STATS["windows_resolved"][asset] += 1
                    CURRENT[asset] = found
                    with_db(log_event, asset, "window_start", found[2])
                else:
                    # kesif basarisiz: eski pencereyi cekmeye devam etme (bitti),
                    # bir sonraki dongude tekrar dene
                    CURRENT.pop(asset, None)
                    time.sleep(POLL_INTERVAL_SEC / len(ASSETS))
                    continue
            wstart_a, token, slug = CURRENT[asset]
            top = get_book_top(token)
            if top:
                with_db(insert_quote, asset, wstart_a, slug, token, top)
            time.sleep(POLL_INTERVAL_SEC / len(ASSETS))

        if time.time() - last_hb > 120:
            log.info(f"HEARTBEAT quotes={STATS['quotes']} windows={STATS['windows_resolved']} disc_fail={STATS['discovery_failures']}")
            last_hb = time.time()

    log.info("Kapaniyor.")
    try:
        DB_CONN.close()
        LOCK_CONN.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
