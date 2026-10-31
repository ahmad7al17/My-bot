import os
import time
import math
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes
)

# =========================================================
# 1) الإعدادات الآمنة
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("أضف BOT_TOKEN إلى Environment Variables في Render.")
if not API_KEY:
    raise RuntimeError("أضف TWELVE_DATA_API_KEY إلى Environment Variables في Render.")

BASE_URL = "https://api.twelvedata.com"
DB_PATH = os.getenv("DB_PATH", "trading_signals.db")
CACHE_TTL = 20
MIN_SCORE = 7
SAUDI_TZ = timezone(timedelta(hours=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("EO_StrictBot")

# =========================================================
# 2) خادم Render
# =========================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"EO Strict Signal Bot is running."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def run_server():
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


threading.Thread(target=run_server, daemon=True).start()

# =========================================================
# 3) الأسواق والفريمات
# =========================================================

MARKETS = [
    ("EUR/USD", "EUR/USD"),
    ("GBP/USD", "GBP/USD"),
    ("USD/JPY", "USD/JPY"),
    ("AUD/USD", "AUD/USD"),
    ("USD/CAD", "USD/CAD"),
    ("USD/CHF", "USD/CHF"),
    ("NZD/USD", "NZD/USD"),
    ("EUR/GBP", "EUR/GBP"),
    ("EUR/JPY", "EUR/JPY"),
    ("GBP/JPY", "GBP/JPY"),
    ("الذهب XAU/USD", "XAU/USD"),
    ("الفضة XAG/USD", "XAG/USD"),
    ("Bitcoin", "BTC/USD"),
    ("Ethereum", "ETH/USD"),
    ("Solana", "SOL/USD"),
]

# تقسيم الأسواق حسب الأزرار الجديدة
SCAN_GROUPS = {
    "MAIN": [
        ("EUR/USD", "EUR/USD"),
        ("GBP/USD", "GBP/USD"),
        ("USD/JPY", "USD/JPY"),
        ("AUD/USD", "AUD/USD"),
        ("USD/CAD", "USD/CAD"),
        ("USD/CHF", "USD/CHF"),
        ("NZD/USD", "NZD/USD"),
    ],
    "COMMODITIES": [
        ("EUR/GBP", "EUR/GBP"),
        ("EUR/JPY", "EUR/JPY"),
        ("GBP/JPY", "GBP/JPY"),
        ("الذهب XAU/USD", "XAU/USD"),
        ("الفضة XAG/USD", "XAG/USD"),
    ],
    "DIGITAL": [
        ("Bitcoin", "BTC/USD"),
        ("Ethereum", "ETH/USD"),
        ("Solana", "SOL/USD"),
    ],
}

SHORT_INTERVALS = (
    "1min", "2min", "3min", "4min", "5min"
)

LONG_INTERVALS = (
    "10min", "15min", "30min", "1h"
)

INTERVAL_LABELS = {
    "1min": "1د",
    "2min": "2د",
    "3min": "3د",
    "4min": "4د",
    "5min": "5د",
    "10min": "10د",
    "15min": "15د",
    "30min": "30د",
    "1h": "1 ساعة",
}

EXPIRIES = [
    ("دقيقة واحدة", "1min"),
    ("دقيقتان", "2min"),
    ("3 دقائق", "3min"),
    ("4 دقائق", "4min"),
    ("5 دقائق", "5min"),
    ("10 دقائق", "10min"),
    ("15 دقيقة", "15min"),
    ("30 دقيقة", "30min"),
    ("ساعة واحدة", "1h"),
]

SECONDS = {
    "1min": 60,
    "2min": 120,
    "3min": 180,
    "4min": 240,
    "5min": 300,
    "10min": 600,
    "15min": 900,
    "30min": 1800,
    "1h": 3600,
}

SOURCE = {
    "1min": ("1min", 60, 1),
    "2min": ("1min", 60, 2),
    "3min": ("1min", 60, 3),
    "4min": ("1min", 60, 4),
    "5min": ("5min", 300, 1),
    "10min": ("5min", 300, 2),
    "15min": ("15min", 900, 1),
    "30min": ("15min", 900, 2),
    "1h": ("15min", 900, 4),
}

CACHE = {}
CACHE_LOCK = threading.Lock()
API_LOCK = threading.Lock()
LAST_API_CALL = 0.0

# =========================================================
# 4) قاعدة البيانات وسجل الإشارات
# =========================================================

def db_connect():
    return sqlite3.connect(DB_PATH, timeout=20)


def init_db():
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                expiry_interval TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                score INTEGER NOT NULL,
                result TEXT DEFAULT 'PENDING',
                trade_started_at REAL,
                expiry_seconds INTEGER
            )
        """)

        cols = {
            row[1] for row in
            con.execute("PRAGMA table_info(signals)").fetchall()
        }

        if "trade_started_at" not in cols:
            con.execute(
                "ALTER TABLE signals ADD COLUMN trade_started_at REAL"
            )

        if "expiry_seconds" not in cols:
            con.execute(
                "ALTER TABLE signals ADD COLUMN expiry_seconds INTEGER"
            )

        con.commit()


def save_signal(symbol, expiry, direction, price, score):
    with db_connect() as con:
        cur = con.execute("""
            INSERT INTO signals
            (created_at, symbol, expiry_interval, direction,
             entry_price, score, result, expiry_seconds)
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            symbol,
            expiry,
            direction,
            price,
            score,
            SECONDS[expiry]
        ))
        con.commit()
        return cur.lastrowid


def start_trade(signal_id):
    with db_connect() as con:
        row = con.execute(
            "SELECT result, trade_started_at FROM signals WHERE id=?",
            (signal_id,)
        ).fetchone()

        if not row or row[0] != "PENDING" or row[1] is not None:
            return False

        con.execute(
            "UPDATE signals SET trade_started_at=? WHERE id=?",
            (time.time(), signal_id)
        )
        con.commit()
        return True


def set_result(signal_id, result):
    with db_connect() as con:
        cur = con.execute("""
            UPDATE signals SET result=?
            WHERE id=? AND result='PENDING'
        """, (result, signal_id))
        con.commit()
        return cur.rowcount > 0


def get_signal(signal_id):
    with db_connect() as con:
        return con.execute("""
            SELECT id, symbol, expiry_interval, direction,
                   entry_price, score, result, trade_started_at,
                   expiry_seconds
            FROM signals WHERE id=?
        """, (signal_id,)).fetchone()


def get_stats():
    with db_connect() as con:
        rows = con.execute(
            "SELECT result, COUNT(*) FROM signals GROUP BY result"
        ).fetchall()
    return {r: n for r, n in rows}


# =========================================================
# 5) API وبيانات الشموع
# =========================================================

def api_get(endpoint, params):
    global LAST_API_CALL

    with API_LOCK:
        wait = 1.0 - (time.time() - LAST_API_CALL)
        if wait > 0:
            time.sleep(wait)

        params = dict(params)
        params["apikey"] = API_KEY

        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=params,
            timeout=20
        )

        LAST_API_CALL = time.time()

    if response.status_code == 429:
        raise ValueError(
            "تجاوز حد طلبات Twelve Data؛ انتظر ثم أعد المحاولة."
        )

    response.raise_for_status()
    data = response.json()

    if data.get("status") == "error" or "code" in data:
        raise ValueError(
            data.get("message", "خطأ من مزود البيانات")
        )

    return data


def get_price(symbol):
    data = api_get("price", {"symbol": symbol})

    if "price" not in data:
        raise ValueError("لم يصل سعر صالح.")

    price = float(data["price"])

    if not math.isfinite(price) or price <= 0:
        raise ValueError("السعر المستلم غير صالح.")

    return price


def parse_datetime(value):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(
                value, fmt
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    raise ValueError(
        "تنسيق وقت الشمعة غير معروف: " + str(value)
    )


def aggregate_candles(candles, source_seconds, factor):
    target_seconds = source_seconds * factor
    groups = {}

    for c in candles:
        bucket = int(c["timestamp"] // target_seconds)
        groups.setdefault(bucket, []).append(c)

    result = []

    for group in groups.values():
        group = sorted(group, key=lambda x: x["timestamp"])

        if len(group) != factor:
            continue

        stamps = [int(x["timestamp"]) for x in group]

        if any(
            stamps[i] - stamps[i - 1] != source_seconds
            for i in range(1, len(stamps))
        ):
            continue

        result.append({
            "timestamp": group[0]["timestamp"],
            "open": group[0]["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": group[-1]["close"],
        })

    return sorted(result, key=lambda x: x["timestamp"])


def get_candles(symbol, interval, limit=250):
    if interval not in SOURCE:
        raise ValueError("الفريم غير مدعوم.")

    key = (symbol, interval, limit)
    now = time.time()

    with CACHE_LOCK:
        item = CACHE.get(key)
        if item and now - item["time"] < CACHE_TTL:
            return item["candles"]

    api_interval, source_seconds, factor = SOURCE[interval]
    outputsize = min(5000, max(limit * factor + 20, 150))

    data = api_get("time_series", {
        "symbol": symbol,
        "interval": api_interval,
        "outputsize": outputsize,
        "timezone": "UTC",
        "format": "JSON",
    })

    if "values" not in data or not data["values"]:
        raise ValueError("لا توجد شموع لهذا السوق.")

    now = time.time()
    candles = []

    for item in reversed(data["values"]):
        try:
            dt = parse_datetime(item["datetime"])

            c = {
                "timestamp": dt.timestamp(),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }

            values = [
                c["open"], c["high"], c["low"], c["close"]
            ]

            if not all(
                math.isfinite(v) and v > 0 for v in values
            ):
                continue

            if c["high"] < max(
                c["open"], c["close"], c["low"]
            ):
                continue

            if c["low"] > min(
                c["open"], c["close"], c["high"]
            ):
                continue

            # الشموع المكتملة فقط
            if c["timestamp"] + source_seconds <= now:
                candles.append(c)

        except (ValueError, TypeError, KeyError):
            continue

    if factor > 1:
        candles = aggregate_candles(
            candles, source_seconds, factor
        )

    candles = candles[-limit:]

    if len(candles) < 60:
        raise ValueError(
            f"جودة البيانات غير كافية للفريم {interval}: "
            f"وصلت {len(candles)} شمعة فقط."
        )

    age = now - (
        candles[-1]["timestamp"] + SECONDS[interval]
    )

    if age > max(SECONDS[interval] * 3, 900):
        raise ValueError(
            "آخر شمعة قديمة؛ قد يكون السوق مغلقًا أو البيانات متأخرة."
        )

    with CACHE_LOCK:
        CACHE[key] = {
            "time": now,
            "candles": candles
        }

    return candles
# =========================================================
# 6) المؤشرات الفنية
# =========================================================

def ema(values, period):
    if len(values) < period:
        raise ValueError("بيانات EMA غير كافية.")

    alpha = 2 / (period + 1)
    out = [sum(values[:period]) / period]

    for value in values[period:]:
        out.append(
            alpha * value + (1 - alpha) * out[-1]
        )

    return [None] * (period - 1) + out


def rsi(values, period=14):
    if len(values) <= period:
        return 50.0

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period

    if ag == 0 and al == 0:
        return 50.0
    if al == 0:
        return 100.0
    if ag == 0:
        return 0.0

    rs = ag / al
    return 100 - 100 / (1 + rs)


def atr(candles, period=14):
    trs = []

    for i in range(1, len(candles)):
        c = candles[i]
        prev = candles[i - 1]["close"]

        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - prev),
            abs(c["low"] - prev),
        ))

    if len(trs) < period:
        raise ValueError("بيانات ATR غير كافية.")

    val = sum(trs[:period]) / period

    for x in trs[period:]:
        val = (val * (period - 1) + x) / period

    return val


def adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return 0.0

    plus_dm = []
    minus_dm = []
    tr_list = []

    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]

        up = c["high"] - p["high"]
        down = p["low"] - c["low"]

        plus_dm.append(
            up if up > down and up > 0 else 0.0
        )
        minus_dm.append(
            down if down > up and down > 0 else 0.0
        )

        tr_list.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"]),
        ))

    dx_values = []

    for i in range(period - 1, len(tr_list)):
        start = i - period + 1
        tr_sum = sum(tr_list[start:i + 1])

        if tr_sum <= 0:
            continue

        plus_di = (
            100 * sum(plus_dm[start:i + 1]) / tr_sum
        )
        minus_di = (
            100 * sum(minus_dm[start:i + 1]) / tr_sum
        )

        denom = plus_di + minus_di

        dx_values.append(
            100 * abs(plus_di - minus_di) / denom
            if denom else 0
        )

    if len(dx_values) < period:
        return 0.0

    return (
        sum(dx_values[-period:]) /
        min(period, len(dx_values))
    )


# =========================================================
# 7) توقيت الشمعة
# =========================================================

def candle_clock(interval, now=None):
    now = time.time() if now is None else now
    sec = SECONDS[interval]

    start = int(now // sec) * sec
    elapsed = int(now - start)
    remaining = max(0, sec - elapsed)

    return start, elapsed, remaining


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)

    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"{m:02d}:{s:02d}"


def candle_timing_text(symbol, interval):
    now = time.time()

    start, elapsed, remaining = candle_clock(
        interval, now
    )

    now_utc = datetime.fromtimestamp(
        now, timezone.utc
    )
    now_sa = datetime.fromtimestamp(
        now, SAUDI_TZ
    )

    start_utc = datetime.fromtimestamp(
        start, timezone.utc
    )
    start_sa = datetime.fromtimestamp(
        start, SAUDI_TZ
    )

    close_utc = datetime.fromtimestamp(
        start + SECONDS[interval], timezone.utc
    )
    close_sa = datetime.fromtimestamp(
        start + SECONDS[interval], SAUDI_TZ
    )

    return (
        "🔄 تحديث توقيت الشمعة\n\n"
        f"السوق: {symbol}\n"
        f"الفريم: {INTERVAL_LABELS[interval]}\n\n"
        f"الوقت الحالي (السعودية): "
        f"{now_sa.strftime('%H:%M:%S')}\n"
        f"الوقت الحالي (UTC): "
        f"{now_utc.strftime('%H:%M:%S')}\n\n"
        f"افتتاح الشمعة (السعودية): "
        f"{start_sa.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"افتتاح الشمعة (UTC): "
        f"{start_utc.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"إغلاق الشمعة المتوقع (السعودية): "
        f"{close_sa.strftime('%H:%M:%S')}\n"
        f"إغلاق الشمعة المتوقع (UTC): "
        f"{close_utc.strftime('%H:%M:%S')}\n\n"
        f"⏳ المنقضي من الشمعة: "
        f"{fmt_duration(elapsed)}\n"
        f"⌛ المتبقي لإغلاق الشمعة: "
        f"{fmt_duration(remaining)}\n\n"
        "تنبيه: توقيت الشمعة محسوب زمنيًا، "
        "وقد تختلف حدود الشموع عن منصة EO."
    )


def trend_direction(candles):
    closes = [c["close"] for c in candles]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    if e20[-1] is None or e50[-1] is None:
        return "NEUTRAL"

    rising = (
        e20[-1] > e20[-4] and
        e50[-1] >= e50[-4]
    )

    falling = (
        e20[-1] < e20[-4] and
        e50[-1] <= e50[-4]
    )

    if closes[-1] > e20[-1] > e50[-1] and rising:
        return "UP"

    if closes[-1] < e20[-1] < e50[-1] and falling:
        return "DOWN"

    return "NEUTRAL"


# =========================================================
# 8) محرك التحليل الصارم
# =========================================================

def analyze(symbol, interval):
    candles = get_candles(symbol, interval)
    c5 = get_candles(symbol, "5min")
    c15 = get_candles(symbol, "15min")

    closes = [c["close"] for c in candles]
    current = candles[-1]

    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)

    r = rsi(closes)
    a = atr(candles)
    dx = adx(candles)

    t5 = trend_direction(c5)
    t15 = trend_direction(c15)

    candle_range = current["high"] - current["low"]

    if candle_range <= 0:
        raise ValueError("مدى الشمعة غير صالح.")

    body = abs(current["close"] - current["open"])
    body_ratio = body / candle_range

    upper_wick = (
        current["high"] -
        max(current["open"], current["close"])
    )

    lower_wick = (
        min(current["open"], current["close"]) -
        current["low"]
    )

    close_location = (
        current["close"] - current["low"]
    ) / candle_range

    buy = 0
    sell = 0
    reasons = []
    blockers = []

    if t5 == "UP" and t15 == "UP":
        buy += 3
        reasons.append("اتجاه 5 و15 دقيقة صاعد")

    elif t5 == "DOWN" and t15 == "DOWN":
        sell += 3
        reasons.append("اتجاه 5 و15 دقيقة هابط")

    else:
        blockers.append(
            "عدم توافق اتجاهي 5 و15 دقيقة"
        )

    if closes[-1] > e9[-1] > e21[-1] > e50[-1]:
        buy += 2
        reasons.append(
            "ترتيب المتوسطات يدعم الصعود"
        )

    if closes[-1] < e9[-1] < e21[-1] < e50[-1]:
        sell += 2
        reasons.append(
            "ترتيب المتوسطات يدعم الهبوط"
        )

    if dx >= 22:
        if t5 == "UP" and t15 == "UP":
            buy += 1
            reasons.append(
                f"قوة اتجاه مناسبة ADX={dx:.1f}"
            )

        elif t5 == "DOWN" and t15 == "DOWN":
            sell += 1
            reasons.append(
                f"قوة اتجاه مناسبة ADX={dx:.1f}"
            )

    else:
        blockers.append(
            f"الاتجاه ضعيف (ADX={dx:.1f})"
        )

    if 52 <= r <= 66:
        buy += 1
        reasons.append(
            f"زخم صاعد مناسب RSI={r:.1f}"
        )

    elif 34 <= r <= 48:
        sell += 1
        reasons.append(
            f"زخم هابط مناسب RSI={r:.1f}"
        )

    else:
        blockers.append(
            f"الزخم غير مناسب أو متطرف RSI={r:.1f}"
        )

    if (
        current["close"] > current["open"]
        and body_ratio >= 0.55
        and close_location >= 0.70
    ):
        buy += 1
        reasons.append(
            "إغلاق قوي قرب أعلى الشمعة"
        )

    elif (
        current["close"] < current["open"]
        and body_ratio >= 0.55
        and close_location <= 0.30
    ):
        sell += 1
        reasons.append(
            "إغلاق قوي قرب أدنى الشمعة"
        )

    else:
        blockers.append(
            "لا يوجد تأكيد قوي من جسم الشمعة وإغلاقها"
        )

    prior_high = max(
        x["high"] for x in candles[-6:-1]
    )
    prior_low = min(
        x["low"] for x in candles[-6:-1]
    )

    if (
        current["close"] > prior_high
        and current["close"] > current["open"]
    ):
        buy += 1
        reasons.append(
            "إغلاق فوق قمم آخر 5 شموع"
        )

    if (
        current["close"] < prior_low
        and current["close"] < current["open"]
    ):
        sell += 1
        reasons.append(
            "إغلاق دون قيعان آخر 5 شموع"
        )

    if (
        upper_wick > body * 1.25
        and upper_wick > candle_range * 0.35
    ):
        blockers.append(
            "ظل علوي قوي: احتمال رفض سعري"
        )

    if (
        lower_wick > body * 1.25
        and lower_wick > candle_range * 0.35
    ):
        blockers.append(
            "ظل سفلي قوي: احتمال ارتداد معاكس"
        )

    if candle_range > a * 2.0:
        blockers.append(
            "مدى الشمعة أكبر من المعتاد؛ خطر ملاحقة السعر"
        )

    if body_ratio < 0.35:
        blockers.append("جسم الشمعة ضعيف")

    move = abs(current["close"] - closes[-6])

    if a > 0 and move > a * 3.0:
        blockers.append(
            "السعر ابتعد كثيرًا خلال آخر 5 شموع"
        )

    direction = "WAIT"
    score = max(buy, sell)

    if (
        not blockers
        and t5 == "UP"
        and t15 == "UP"
        and buy >= MIN_SCORE
        and buy > sell
    ):
        direction = "UP"

    elif (
        not blockers
        and t5 == "DOWN"
        and t15 == "DOWN"
        and sell >= MIN_SCORE
        and sell > buy
    ):
        direction = "DOWN"

    live_price = get_price(symbol)

    deviation = (
        abs(live_price - current["close"]) / a
        if a else 0
    )

    if direction != "WAIT" and deviation > 0.8:
        direction = "WAIT"
        blockers.append(
            "السعر اللحظي ابتعد عن إغلاق الشمعة؛ "
            "الإشارة قد تكون متأخرة"
        )

    candle_start, elapsed, remaining = candle_clock(
        interval
    )

    return {
        "direction": direction,
        "price": live_price,
        "candle_close": current["close"],
        "rsi": r,
        "adx": dx,
        "atr": a,
        "trend5": t5,
        "trend15": t15,
        "buy_score": buy,
        "sell_score": sell,
        "score": score,
        "reasons": reasons,
        "blockers": blockers,
        "expiry": interval,
        "candle_start": candle_start,
        "elapsed": elapsed,
        "remaining": remaining,
    }


# =========================================================
# 9) الاختبار التاريخي التقريبي
# =========================================================

def backtest(symbol, interval):
    candles = get_candles(
        symbol, interval, limit=500
    )

    if len(candles) < 120:
        raise ValueError(
            "لا توجد بيانات كافية للاختبار التاريخي."
        )

    wins = losses = ties = trades = 0

    for i in range(100, len(candles) - 1):
        history = candles[:i + 1]
        close = [x["close"] for x in history]

        e9 = ema(close, 9)
        e21 = ema(close, 21)
        e50 = ema(close, 50)

        r = rsi(close)
        dx = adx(history)
        c = history[-1]

        rng = c["high"] - c["low"]

        if rng <= 0 or dx < 22:
            continue

        body = abs(c["close"] - c["open"])
        body_ratio = body / rng

        location = (
            c["close"] - c["low"]
        ) / rng

        upper = (
            c["high"] -
            max(c["open"], c["close"])
        )

        lower = (
            min(c["open"], c["close"]) -
            c["low"]
        )

        if body_ratio < 0.55:
            continue

        if upper > body * 1.25 or lower > body * 1.25:
            continue

        direction = None

        if (
            c["close"] > e9[-1] > e21[-1] > e50[-1]
            and 52 <= r <= 66
            and location >= 0.70
        ):
            direction = "UP"

        elif (
            c["close"] < e9[-1] < e21[-1] < e50[-1]
            and 34 <= r <= 48
            and location <= 0.30
        ):
            direction = "DOWN"

        if not direction:
            continue

        trades += 1
        nxt = candles[i + 1]["close"]

        if nxt == c["close"]:
            ties += 1

        elif (
            direction == "UP" and nxt > c["close"]
        ) or (
            direction == "DOWN" and nxt < c["close"]
        ):
            wins += 1

        else:
            losses += 1

    decided = wins + losses

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "winrate": (
            wins / decided * 100 if decided else 0
        ),
    }
# =========================================================
# 10) واجهة تيليجرام والأزرار
# =========================================================

def main_keyboard():
    rows = []

    # أزرار الأسواق الفردية
    for i in range(0, len(MARKETS), 2):
        row = [
            InlineKeyboardButton(
                MARKETS[i][0],
                callback_data=f"M|{MARKETS[i][1]}"
            )
        ]

        if i + 1 < len(MARKETS):
            row.append(
                InlineKeyboardButton(
                    MARKETS[i + 1][0],
                    callback_data=f"M|{MARKETS[i + 1][1]}"
                )
            )

        rows.append(row)

    # الفريمات القصيرة
    rows.extend([
        [InlineKeyboardButton(
            "🔍 رئيسية [1د، 2د، 3د، 4د، 5د]",
            callback_data="SCAN1S"
        )],
        [InlineKeyboardButton(
            "🔍 سلع ومتقاطعات [1د، 2د، 3د، 4د، 5د]",
            callback_data="SCAN2S"
        )],
        [InlineKeyboardButton(
            "🔍 رقمية [1د، 2د، 3د، 4د، 5د]",
            callback_data="SCAN3S"
        )],

        # الفريمات الطويلة
        [InlineKeyboardButton(
            "⭐ رئيسية [10د، 15د، 30د، 1 ساعة]",
            callback_data="SCAN1L"
        )],
        [InlineKeyboardButton(
            "⭐ سلع ومتقاطعات [10د، 15د، 30د، 1 ساعة]",
            callback_data="SCAN2L"
        )],
        [InlineKeyboardButton(
            "⭐ رقمية [10د، 15د، 30د، 1 ساعة]",
            callback_data="SCAN3L"
        )],

        [InlineKeyboardButton(
            "📊 الإحصائيات",
            callback_data="STATS"
        )],
    ])

    return InlineKeyboardMarkup(rows)


def expiry_keyboard(symbol):
    rows = [
        [InlineKeyboardButton(
            f"⏱️ {label}",
            callback_data=f"E|{symbol}|{interval}"
        )]
        for label, interval in EXPIRIES
    ]

    rows.append([
        InlineKeyboardButton(
            "⬅️ الأسواق",
            callback_data="BACK"
        )
    ])

    return InlineKeyboardMarkup(rows)


def signal_keyboard(signal_id, symbol, interval):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔄 تحديث توقيت الشمعة",
            callback_data=f"CLOCK|{symbol}|{interval}"
        )],
        [InlineKeyboardButton(
            "▶️ بدأت الصفقة الآن",
            callback_data=f"TRADE|{signal_id}"
        )],
        [InlineKeyboardButton(
            "⬅️ الأسواق",
            callback_data="BACK"
        )],
    ])


def clock_keyboard(symbol, interval):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔄 تحديث التوقيت مرة أخرى",
            callback_data=f"CLOCK|{symbol}|{interval}"
        )],
        [InlineKeyboardButton(
            "⬅️ الأسواق",
            callback_data="BACK"
        )],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🤖 بوت التحليل الصارم\n"
        "يرفض الفرص الضعيفة والمتضاربة.\n"
        "الإشارات احتمالية وليست ضمانًا للربح.\n\n"
        "الأوامر: /help",
        reply_markup=main_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start — القائمة\n"
        "/stats — الإحصائيات\n"
        "/result ID WIN — تسجيل فوز\n"
        "/result ID LOSS — تسجيل خسارة\n"
        "/result ID TIE — تسجيل تعادل\n"
        "/backtest EUR/USD 1min — اختبار تاريخي تقريبي\n"
        "/compare EUR/USD 1.08500 — مقارنة السعر مع EO\n"
        "/timer ID — الوقت المتبقي للصفقة\n\n"
        "بعد دخولك اليدوي في EO اضغط «بدأت الصفقة الآن» "
        "لبدء عداد الصفقة."
    )


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "الصيغة: /compare EUR/USD 1.08500"
        )
        return

    try:
        symbol = context.args[0].upper()
        eo = float(context.args[1])

        if eo <= 0:
            raise ValueError(
                "السعر يجب أن يكون أكبر من صفر."
            )

        market = await asyncio.to_thread(
            get_price, symbol
        )

        diff = market - eo
        pct = abs(diff) / eo * 100

        await update.effective_message.reply_text(
            f"السوق: {symbol}\n"
            f"سعر EO: {eo:.8g}\n"
            f"Twelve Data: {market:.8g}\n"
            f"الفرق: {diff:.8g}\n"
            f"الفرق النسبي: {pct:.5f}%\n\n"
            "هذه مقارنة لحظية واحدة وليست إثباتًا للتطابق المستمر."
        )

    except Exception as e:
        await update.effective_message.reply_text(
            f"تعذرت المقارنة: {str(e)[:250]}"
        )


async def result_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "الصيغة: /result 12 WIN\nWIN أو LOSS أو TIE"
        )
        return

    try:
        sid = int(context.args[0])
        result = context.args[1].upper()

        if result not in ("WIN", "LOSS", "TIE"):
            raise ValueError(
                "استخدم WIN أو LOSS أو TIE."
            )

        ok = await asyncio.to_thread(
            set_result, sid, result
        )

        await update.effective_message.reply_text(
            f"تم تسجيل الإشارة {sid}: {result}"
            if ok else
            "الإشارة غير موجودة أو سبق تسجيل نتيجتها."
        )

    except ValueError as e:
        await update.effective_message.reply_text(str(e))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await asyncio.to_thread(get_stats)

    wins = stats.get("WIN", 0)
    losses = stats.get("LOSS", 0)
    ties = stats.get("TIE", 0)
    pending = stats.get("PENDING", 0)

    decided = wins + losses
    rate = wins / decided * 100 if decided else 0

    await update.effective_message.reply_text(
        f"📊 سجل الإشارات\n\n"
        f"فوز: {wins}\n"
        f"خسارة: {losses}\n"
        f"تعادل: {ties}\n"
        f"معلقة: {pending}\n"
        f"نسبة الفوز بين الصفقات المحسومة: {rate:.1f}%"
    )


async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "الصيغة: /backtest EUR/USD 1min\n"
            "الفريمات: 1min, 2min, 3min, 4min, 5min, "
            "10min, 15min, 30min, 1h"
        )
        return

    symbol = context.args[0].upper()
    interval = context.args[1]

    if interval not in SECONDS:
        await update.effective_message.reply_text(
            "الفريم غير مدعوم."
        )
        return

    await update.effective_message.reply_text(
        "⏳ جاري الاختبار التاريخي..."
    )

    try:
        res = await asyncio.to_thread(
            backtest, symbol, interval
        )

        await update.effective_message.reply_text(
            f"📈 اختبار تاريخي تقريبي: "
            f"{symbol} [{INTERVAL_LABELS[interval]}]\n\n"
            f"الإشارات المختبرة: {res['trades']}\n"
            f"فوز: {res['wins']}\n"
            f"خسارة: {res['losses']}\n"
            f"تعادل: {res['ties']}\n"
            f"نسبة الفوز: {res['winrate']:.1f}%\n\n"
            "تنبيه: هذا اختبار مبسط، لا يحاكي بدقة "
            "انتهاء EO أو فروق الأسعار أو التأخير."
        )

    except Exception as e:
        await update.effective_message.reply_text(
            f"تعذر الاختبار: {str(e)[:250]}"
        )


# =========================================================
# 11) الفحص الجماعي الجديد
# =========================================================

async def execute_scan(query, markets, intervals, group_title):
    interval_text = "، ".join(
        INTERVAL_LABELS[x] for x in intervals
    )

    await query.edit_message_text(
        f"🔍 بدأ الفحص الصارم\n\n"
        f"المجموعة: {group_title}\n"
        f"الفريمات: {interval_text}\n\n"
        "⏳ يجري فحص الأسواق..."
    )

    found = []
    errors = 0

    for name, symbol in markets:
        for interval in intervals:
            try:
                res = await asyncio.to_thread(
                    analyze, symbol, interval
                )

                if res["direction"] not in ("UP", "DOWN"):
                    continue

                score = (
                    res["buy_score"]
                    if res["direction"] == "UP"
                    else res["sell_score"]
                )

                # حفظ كل إشارة برقم مستقل
                sid = await asyncio.to_thread(
                    save_signal,
                    symbol,
                    interval,
                    res["direction"],
                    res["price"],
                    score
                )

                direction_text = (
                    "🟢 إشارة صعود"
                    if res["direction"] == "UP"
                    else "🔴 إشارة هبوط"
                )

                message = (
                    f"{direction_text}\n"
                    f"السوق: {name}\n"
                    f"الفريم: {INTERVAL_LABELS[interval]}\n"
                    f"السعر: {res['price']:.8g}\n"
                    f"إغلاق آخر شمعة: "
                    f"{res['candle_close']:.8g}\n"
                    f"RSI: {res['rsi']:.1f} | "
                    f"ADX: {res['adx']:.1f}\n"
                    f"قوة الشروط: {score}\n"
                    f"اتجاه 5د: {res['trend5']} | "
                    f"15د: {res['trend15']}\n\n"
                    f"⏳ المنقضي من الشمعة: "
                    f"{fmt_duration(res['elapsed'])}\n"
                    f"⌛ المتبقي لإغلاق الشمعة: "
                    f"{fmt_duration(res['remaining'])}\n\n"
                    f"رقم الإشارة: {sid}\n"
                    f"مدة الصفقة: {INTERVAL_LABELS[interval]}\n\n"
                    "إذا دخلت الصفقة يدويًا في EO، "
                    "اضغط زر بدء العداد أدناه."
                )

                found.append({
                    "message": message,
                    "keyboard": signal_keyboard(
                        sid, symbol, interval
                    )
                })

            except Exception as e:
                errors += 1
                logger.warning(
                    "Scan failed %s %s: %s",
                    symbol, interval, e
                )

    summary = (
        "🔎 انتهى الفحص الصارم\n\n"
        f"المجموعة: {group_title}\n"
        f"عدد الأسواق: {len(markets)}\n"
        f"الفريمات: {interval_text}\n"
        f"عدد الإشارات المقبولة: {len(found)}\n"
        f"الأخطاء أو البيانات غير المتاحة: {errors}\n\n"
    )

    if found:
        summary += (
            "👇 ستجد تفاصيل كل إشارة في رسالة مستقلة "
            "مع زر بدء عداد الصفقة."
        )
    else:
        summary += (
            "لم تظهر فرص اجتازت جميع شروط التحليل الصارم."
        )

    await query.edit_message_text(
        summary,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "⬅️ الأسواق",
                callback_data="BACK"
            )
        ]])
    )

    # رسالة مستقلة لكل إشارة حتى لا يؤدي الضغط
    # على زر إشارة إلى إزالة أزرار الإشارات الأخرى
    for item in found:
        try:
            await query.message.reply_text(
                item["message"],
                reply_markup=item["keyboard"]
            )
        except Exception as e:
            logger.error(
                "Could not send signal message: %s", e
            )


# =========================================================
# 12) معالجة أزرار تيليجرام
# =========================================================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "BACK":
        await query.edit_message_text(
            "اختر السوق:",
            reply_markup=main_keyboard()
        )
        return

    # تحديث توقيت الشمعة
    if data.startswith("CLOCK|"):
        parts = data.split("|")

        if len(parts) != 3:
            await query.message.reply_text(
                "تعذر قراءة بيانات توقيت الشمعة."
            )
            return

        symbol, interval = parts[1], parts[2]

        if interval not in SECONDS:
            await query.message.reply_text(
                "الفريم غير مدعوم."
            )
            return

        try:
            message = candle_timing_text(
                symbol, interval
            )

            await query.message.reply_text(
                message,
                reply_markup=clock_keyboard(
                    symbol, interval
                )
            )

        except Exception as e:
            logger.exception("Clock update failed")
            await query.message.reply_text(
                f"تعذر تحديث التوقيت: {str(e)[:200]}"
            )

        return

    # الإحصائيات
    if data == "STATS":
        stats = await asyncio.to_thread(get_stats)

        wins = stats.get("WIN", 0)
        losses = stats.get("LOSS", 0)
        decided = wins + losses
        rate = wins / decided * 100 if decided else 0

        await query.edit_message_text(
            f"📊 النتائج\n"
            f"فوز: {wins}\n"
            f"خسارة: {losses}\n"
            f"تعادل: {stats.get('TIE', 0)}\n"
            f"معلقة: {stats.get('PENDING', 0)}\n"
            f"نسبة الفوز: {rate:.1f}%",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ الأسواق",
                    callback_data="BACK"
                )
            ]])
        )
        return

    # الأزرار الستة للفحص الجماعي
    scan_config = {
        "SCAN1S": (
            SCAN_GROUPS["MAIN"],
            SHORT_INTERVALS,
            "العملات الرئيسية - الفريمات القصيرة"
        ),
        "SCAN2S": (
            SCAN_GROUPS["COMMODITIES"],
            SHORT_INTERVALS,
            "السلع والمتقاطعات - الفريمات القصيرة"
        ),
        "SCAN3S": (
            SCAN_GROUPS["DIGITAL"],
            SHORT_INTERVALS,
            "العملات الرقمية - الفريمات القصيرة"
        ),
        "SCAN1L": (
            SCAN_GROUPS["MAIN"],
            LONG_INTERVALS,
            "العملات الرئيسية - الفريمات الطويلة"
        ),
        "SCAN2L": (
            SCAN_GROUPS["COMMODITIES"],
            LONG_INTERVALS,
            "السلع والمتقاطعات - الفريمات الطويلة"
        ),
        "SCAN3L": (
            SCAN_GROUPS["DIGITAL"],
            LONG_INTERVALS,
            "العملات الرقمية - الفريمات الطويلة"
        ),
    }

    if data in scan_config:
        markets, intervals, group_title = scan_config[data]

        await execute_scan(
            query,
            markets,
            intervals,
            group_title
        )
        return

    # بدء عداد الصفقة
    if data.startswith("TRADE|"):
        try:
            sid = int(data.split("|", 1)[1])
        except ValueError:
            await query.message.reply_text(
                "رقم الإشارة غير صحيح."
            )
            return

        ok = await asyncio.to_thread(
            start_trade, sid
        )

        if not ok:
            await query.message.reply_text(
                "لم يبدأ العداد: الإشارة غير موجودة "
                "أو بدأ عدادها سابقًا أو سُجلت نتيجتها."
            )
            return

        row = await asyncio.to_thread(
            get_signal, sid
        )

        if not row:
            return

        expiry_sec = row[8] or SECONDS[row[2]]
        now_sa = datetime.now(
            SAUDI_TZ
        ).strftime("%H:%M:%S")

        await query.message.reply_text(
            f"▶️ بدأ عداد الصفقة رقم {sid}\n"
            f"السوق: {row[1]}\n"
            f"المدة: {INTERVAL_LABELS[row[2]]}\n"
            f"وقت البداية (السعودية): {now_sa}\n"
            f"مدة الصفقة: {fmt_duration(expiry_sec)}\n\n"
            f"المتبقي: {fmt_duration(expiry_sec)}\n"
            f"استخدم /timer {sid} للتحقق من الوقت المتبقي."
        )
        return

    # اختيار سوق فردي
    if data.startswith("M|"):
        symbol = data.split("|", 1)[1]

        await query.edit_message_text(
            f"السوق: {symbol}\n"
            "اختر مدة التحليل/الصفقة:",
            reply_markup=expiry_keyboard(symbol)
        )
        return

    if not data.startswith("E|"):
        return

    _, symbol, interval = data.split("|")

    await query.edit_message_text(
        "⏳ يجري تحليل الشموع المكتملة..."
    )

    try:
        res = await asyncio.to_thread(
            analyze, symbol, interval
        )

        start_dt = datetime.fromtimestamp(
            res["candle_start"],
            timezone.utc
        ).strftime("%H:%M:%S UTC")

        if res["direction"] == "WAIT":
            message = (
                f"⚪ انتظار — لا دخول\n"
                f"السوق: {symbol}\n"
                f"الفريم: {INTERVAL_LABELS[interval]}\n"
                f"السعر: {res['price']:.8g}\n"
                f"اتجاه 5د: {res['trend5']} | "
                f"15د: {res['trend15']}\n"
                f"RSI: {res['rsi']:.1f} | "
                f"ADX: {res['adx']:.1f}\n\n"
                "أسباب الرفض:\n"
                + "\n".join(
                    "• " + x
                    for x in (
                        res["blockers"]
                        or ["الشروط الصارمة غير مكتملة"]
                    )
                )
                + f"\n\nافتتاح الشمعة وفق UTC: {start_dt}\n"
                f"المنقضي: {fmt_duration(res['elapsed'])}\n"
                f"المتبقي لإغلاق الشمعة: "
                f"{fmt_duration(res['remaining'])}"
            )

            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔄 تحديث توقيت الشمعة",
                    callback_data=f"CLOCK|{symbol}|{interval}"
                )],
                [InlineKeyboardButton(
                    "⬅️ الأسواق",
                    callback_data="BACK"
                )]
            ])

        else:
            score = (
                res["buy_score"]
                if res["direction"] == "UP"
                else res["sell_score"]
            )

            sid = await asyncio.to_thread(
                save_signal,
                symbol,
                interval,
                res["direction"],
                res["price"],
                score
            )

            message = (
                f"{'🟢 إشارة صعود' if res['direction'] == 'UP' else '🔴 إشارة هبوط'}\n"
                f"السوق: {symbol}\n"
                f"الفريم: {INTERVAL_LABELS[interval]}\n"
                f"السعر: {res['price']:.8g}\n"
                f"إغلاق آخر شمعة مكتملة: "
                f"{res['candle_close']:.8g}\n"
                f"RSI: {res['rsi']:.1f} | "
                f"ADX: {res['adx']:.1f}\n"
                f"ATR: {res['atr']:.8g}\n"
                f"اتجاه 5د: {res['trend5']} | "
                f"15د: {res['trend15']}\n"
                f"قوة الشروط: {score}\n\n"
                "أسباب القبول:\n"
                + "\n".join(
                    "• " + x for x in res["reasons"]
                )
                + f"\n\nافتتاح الشمعة UTC: {start_dt}\n"
                f"المنقضي: {fmt_duration(res['elapsed'])}\n"
                f"المتبقي لإغلاق الشمعة: "
                f"{fmt_duration(res['remaining'])}\n\n"
                f"رقم الإشارة: {sid}\n"
                "نفّذ الصفقة يدويًا في EO فقط إذا قررت ذلك، "
                "ثم اضغط زر بدء العداد."
            )

            markup = signal_keyboard(
                sid, symbol, interval
            )

        await query.edit_message_text(
            message,
            reply_markup=markup
        )

    except Exception as e:
        logger.exception("Analysis failed")

        await query.edit_message_text(
            f"❌ رفض التحليل بسبب خطأ أو ضعف البيانات:\n"
            f"{str(e)[:350]}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ الأسواق",
                    callback_data="BACK"
                )
            ]])
        )


# =========================================================
# 13) عداد انتهاء الصفقة
# =========================================================

async def timer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "الصيغة: /timer 12"
        )
        return

    try:
        sid = int(context.args[0])

        row = await asyncio.to_thread(
            get_signal, sid
        )

        if not row:
            await update.effective_message.reply_text(
                "رقم الإشارة غير موجود."
            )
            return

        started = row[7]
        duration = row[8]

        if started is None:
            await update.effective_message.reply_text(
                "لم يبدأ عداد الصفقة. "
                "اضغط «بدأت الصفقة الآن» بعد دخولك اليدوي."
            )
            return

        duration = duration or SECONDS[row[2]]

        remaining = max(
            0,
            int(duration - (time.time() - started))
        )

        await update.effective_message.reply_text(
            f"⏱️ الصفقة رقم {sid}\n"
            f"السوق: {row[1]}\n"
            f"المدة الأصلية: {fmt_duration(duration)}\n"
            f"المتبقي: {fmt_duration(remaining)}\n"
            + (
                "انتهت المدة المحددة."
                if remaining == 0
                else "العداد تقريبي."
            )
        )

    except ValueError:
        await update.effective_message.reply_text(
            "رقم الإشارة غير صحيح."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Telegram update error",
        exc_info=context.error
    )


# =========================================================
# 14) تشغيل البوت
# =========================================================

def main():
    init_db()

    # لا تشغّل نسختين من البوت بالتوكن نفسه
    app = ApplicationBuilder().token(
        BOT_TOKEN
    ).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(CommandHandler("timer", timer_cmd))

    app.add_handler(
        CallbackQueryHandler(buttons)
    )

    app.add_error_handler(error_handler)

    logger.info(
        "Strict EO Signal Bot started."
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
