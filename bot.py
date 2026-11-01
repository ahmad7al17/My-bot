
import os
import time
import math
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================================
# 1. الإعدادات الأساسية
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

DB_FILE = "signals.db"
API_URL = "https://api.twelvedata.com/time_series"

MIN_SCORE = 6
CACHE_SECONDS = 20
REQUEST_TIMEOUT = 15
MIN_CANDLES = 60

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# 2. الأسواق والفريمات
# =========================================================

MARKETS = {
    "EUR/USD": "EUR/USD",
    "GBP/USD": "GBP/USD",
    "USD/JPY": "USD/JPY",
    "AUD/USD": "AUD/USD",
    "USD/CAD": "USD/CAD",
    "USD/CHF": "USD/CHF",
    "NZD/USD": "NZD/USD",
    "EUR/GBP": "EUR/GBP",
    "EUR/JPY": "EUR/JPY",
    "GBP/JPY": "GBP/JPY",
    "XAU/USD": "XAU/USD",
    "XAG/USD": "XAG/USD",
    "BTC/USD": "BTC/USD",
    "ETH/USD": "ETH/USD",
    "SOL/USD": "SOL/USD",
}

INTERVALS = {
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

# الفريمات التي يدعمها مزود البيانات مباشرة،
# والفريمات الأخرى تُبنى من شموع أصغر.
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

# =========================================================
# 3. خادم ويب بسيط لاستضافة Render
# =========================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Trading bot is running")

    def log_message(self, format, *args):
        return


def start_web_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    logger.info("Web server started on port %s", port)
    server.serve_forever()


# =========================================================
# 4. قاعدة البيانات
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                direction TEXT NOT NULL,
                score INTEGER NOT NULL,
                entry_price REAL,
                status TEXT DEFAULT 'PENDING',
                result TEXT,
                trade_started_at REAL
            )
        """)
        conn.commit()


def save_signal(symbol, interval, direction, score, price):
    with db_connect() as conn:
        cur = conn.execute("""
            INSERT INTO signals
            (created_at, symbol, interval, direction, score,
             entry_price, status)
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
        """, (
            time.time(),
            symbol,
            interval,
            direction,
            int(score),
            float(price),
        ))
        conn.commit()
        return cur.lastrowid


def get_signal(signal_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM signals WHERE id = ?",
            (signal_id,),
        ).fetchone()


def record_result(signal_id, result):
    result = result.upper()
    if result not in ("WIN", "LOSS", "DRAW"):
        return False

    with db_connect() as conn:
        cur = conn.execute("""
            UPDATE signals
            SET result = ?, status = ?
            WHERE id = ? AND status = 'PENDING'
        """, (result, result, signal_id))
        conn.commit()
        return cur.rowcount > 0


def get_stats():
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT result, COUNT(*) AS n
            FROM signals
            WHERE result IS NOT NULL
            GROUP BY result
        """).fetchall()

    stats = {"WIN": 0, "LOSS": 0, "DRAW": 0}
    for row in rows:
        stats[row["result"]] = row["n"]
    return stats


# =========================================================
# 5. جلب البيانات مع التخزين المؤقت
# =========================================================

DATA_CACHE = {}
LAST_API_CALL = 0
API_LOCK = threading.Lock()


def api_get(params):
    global LAST_API_CALL

    if not API_KEY:
        raise RuntimeError(
            "مفتاح TWELVE_DATA_API_KEY غير موجود في متغيرات البيئة."
        )

    # تقليل الطلبات المتتابعة إلى مزود البيانات.
    with API_LOCK:
        wait = 1.1 - (time.time() - LAST_API_CALL)
        if wait > 0:
            time.sleep(wait)

        response = requests.get(
            API_URL,
            params={**params, "apikey": API_KEY},
            timeout=REQUEST_TIMEOUT,
        )
        LAST_API_CALL = time.time()

    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        message = data.get(
            "message",
            data.get("code", "استجابة غير صالحة من مزود البيانات"),
        )
        raise RuntimeError(str(message))

    return data


def parse_timestamp(value):
    value = str(value).strip()

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )

    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass

    raise ValueError(f"وقت شمعة غير مفهوم: {value}")


def fetch_source_candles(symbol, source_interval, outputsize=500):
    cache_key = (symbol, source_interval)
    now = time.time()

    cached = DATA_CACHE.get(cache_key)
    if cached and now - cached["time"] < CACHE_SECONDS:
        return cached["candles"]

    data = api_get({
        "symbol": symbol,
        "interval": source_interval,
        "outputsize": outputsize,
        "timezone": "UTC",
        "format": "JSON",
    })

    source_seconds = {
        "1min": 60,
        "5min": 300,
        "15min": 900,
    }[source_interval]

    candles = []

    for item in reversed(data["values"]):
        try:
            timestamp = parse_timestamp(item["datetime"])

            candle = {
                "timestamp": timestamp,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume") or 0),
            }

            # استخدام الشموع المكتملة فقط.
            if timestamp + source_seconds <= now:
                candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    if not candles:
        raise RuntimeError("لم تصل شموع مكتملة وصالحة.")

    DATA_CACHE[cache_key] = {
        "time": now,
        "candles": candles,
    }

    return candles


# =========================================================
# 6. تجميع الشموع للفريمات غير المدعومة مباشرة
# =========================================================

def aggregate_candles(candles, source_seconds, factor):
    if factor == 1:
        return candles[:]

    groups = {}

    for candle in candles:
        bucket = int(
            candle["timestamp"] // (source_seconds * factor)
        ) * (source_seconds * factor)

        groups.setdefault(bucket, []).append(candle)

    result = []

    for bucket in sorted(groups):
        group = sorted(
            groups[bucket],
            key=lambda c: c["timestamp"],
        )

        # لا نقبل مجموعة ناقصة.
        if len(group) != factor:
            continue

        expected = [
            bucket + i * source_seconds
            for i in range(factor)
        ]
        actual = [int(c["timestamp"]) for c in group]

        if actual != expected:
            continue

        result.append({
            "timestamp": float(bucket),
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
            "volume": sum(c["volume"] for c in group),
        })

    return result


def get_candles(symbol, interval, outputsize=500):
    if interval not in SOURCE:
        raise ValueError(f"الفريم غير مدعوم: {interval}")

    source_interval, source_seconds, factor = SOURCE[interval]

    candles = fetch_source_candles(
        symbol,
        source_interval,
        outputsize=max(outputsize * factor + 20, 200),
    )

    candles = aggregate_candles(
        candles,
        source_seconds,
        factor,
    )

    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"البيانات غير كافية: {len(candles)} شمعة فقط."
        )

    # رفض البيانات القديمة أو المتأخرة بشكل غير طبيعي.
    age = time.time() - (
        candles[-1]["timestamp"] + INTERVALS[interval]
    )

    if age > INTERVALS[interval] * 3:
        raise RuntimeError(
            f"آخر شمعة قديمة؛ التأخير التقريبي {int(age)} ثانية."
        )

    return candles[-outputsize:]


# =========================================================
# نهاية الجزء الأول
# لا تشغّل الملف بعد؛ انتظر الأجزاء التالية.
# =========================================================

# =========================================================
# 7. المؤشرات الفنية
# =========================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value * multiplier) + (
            result * (1 - multiplier)
        )

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1)) + gains[i]
        ) / period
        avg_loss = (
            (avg_loss * (period - 1)) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )
        ranges.append(true_range)

    value = sum(ranges[:period]) / period

    for current in ranges[period:]:
        value = (
            value * (period - 1) + current
        ) / period

    return value


def adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return None

    tr_values = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        plus_dm.append(
            up_move if up_move > down_move and up_move > 0 else 0
        )
        minus_dm.append(
            down_move if down_move > up_move and down_move > 0 else 0
        )

        tr_values.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))

    tr = sum(tr_values[:period])
    plus = sum(plus_dm[:period])
    minus = sum(minus_dm[:period])

    dx_values = []

    for i in range(period, len(tr_values)):
        tr = tr - (tr / period) + tr_values[i]
        plus = plus - (plus / period) + plus_dm[i]
        minus = minus - (minus / period) + minus_dm[i]

        if tr <= 0:
            continue

        plus_di = 100 * plus / tr
        minus_di = 100 * minus / tr

        denominator = plus_di + minus_di

        if denominator == 0:
            dx_values.append(0)
        else:
            dx_values.append(
                100 * abs(plus_di - minus_di) / denominator
            )

    if len(dx_values) < period:
        return None

    value = sum(dx_values[:period]) / period

    for current in dx_values[period:]:
        value = (
            value * (period - 1) + current
        ) / period

    return value


# =========================================================
# 8. اتجاه السوق
# =========================================================

def trend_direction(candles):
    closes = [c["close"] for c in candles]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    if e20 is None or e50 is None:
        return "NEUTRAL"

    if e20 > e50 and closes[-1] > e20:
        return "UP"

    if e20 < e50 and closes[-1] < e20:
        return "DOWN"

    return "NEUTRAL"


# =========================================================
# 9. تحليل الشمعة
# =========================================================

def candle_features(candle):
    body = abs(candle["close"] - candle["open"])
    full_range = candle["high"] - candle["low"]

    if full_range <= 0:
        return {
            "body_ratio": 0,
            "close_position": 0.5,
            "upper_wick": 0,
            "lower_wick": 0,
        }

    body_ratio = body / full_range

    close_position = (
        candle["close"] - candle["low"]
    ) / full_range

    upper_wick = (
        candle["high"] -
        max(candle["open"], candle["close"])
    ) / full_range

    lower_wick = (
        min(candle["open"], candle["close"]) -
        candle["low"]
    ) / full_range

    return {
        "body_ratio": body_ratio,
        "close_position": close_position,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
    }


# =========================================================
# 10. محرك التحليل والإشارات
# =========================================================

def analyze(symbol, interval):
    diagnostics = []

    try:
        candles = get_candles(symbol, interval)
        candles_5 = get_candles(symbol, "5min")
        candles_15 = get_candles(symbol, "15min")

        if min(
            len(candles),
            len(candles_5),
            len(candles_15),
        ) < MIN_CANDLES:
            return {
                "direction": "WAIT",
                "score": 0,
                "price": None,
                "reason": "عدد الشموع غير كافٍ",
                "diagnostics": ["بيانات غير كافية"],
            }

        closes = [c["close"] for c in candles]
        current = candles[-1]
        previous = candles[-2]

        price = current["close"]
        current_atr = atr(candles)

        if current_atr is None or current_atr <= 0:
            raise RuntimeError("تعذر حساب ATR")

        trend_5 = trend_direction(candles_5)
        trend_15 = trend_direction(candles_15)
        trend_main = trend_direction(candles)

        # شروط أساسية لا نتجاوزها.
        if trend_5 == "NEUTRAL" or trend_15 == "NEUTRAL":
            return {
                "direction": "WAIT",
                "score": 0,
                "price": price,
                "reason": "الاتجاه غير واضح في الفريمات الأكبر",
                "diagnostics": ["اتجاه غير واضح"],
            }

        if trend_5 != trend_15:
            return {
                "direction": "WAIT",
                "score": 0,
                "price": price,
                "reason": "تعارض اتجاه 5 دقائق و15 دقيقة",
                "diagnostics": ["تعارض الفريمات"],
            }

        direction = trend_5

        if trend_main not in (direction, "NEUTRAL"):
            return {
                "direction": "WAIT",
                "score": 0,
                "price": price,
                "reason": "الفريم الحالي يعاكس الاتجاه الأكبر",
                "diagnostics": ["تعارض الاتجاه"],
            }

        e9 = ema(closes, 9)
        e21 = ema(closes, 21)
        e50 = ema(closes, 50)
        current_rsi = rsi(closes)
        current_adx = adx(candles)

        if None in (e9, e21, e50, current_rsi, current_adx):
            raise RuntimeError("تعذر حساب أحد المؤشرات")

        features = candle_features(current)

        score = 0

        # 1. ترتيب المتوسطات.
        if direction == "UP":
            if e9 > e21 > e50:
                score += 2
            elif e9 > e21:
                score += 1

        else:
            if e9 < e21 < e50:
                score += 2
            elif e9 < e21:
                score += 1

        # 2. قوة الاتجاه.
        if current_adx >= 25:
            score += 2
        elif current_adx >= 20:
            score += 1
        else:
            diagnostics.append("قوة الاتجاه ضعيفة")

        # 3. مؤشر RSI.
        if direction == "UP":
            if 50 <= current_rsi <= 68:
                score += 2
            elif 46 <= current_rsi < 50:
                score += 1
            elif current_rsi > 75:
                diagnostics.append("تشبع شرائي")

        else:
            if 32 <= current_rsi <= 50:
                score += 2
            elif 50 < current_rsi <= 54:
                score += 1
            elif current_rsi < 25:
                diagnostics.append("تشبع بيعي")

        # 4. قوة جسم الشمعة وموقع الإغلاق.
        if direction == "UP":
            if (
                current["close"] > current["open"]
                and features["close_position"] >= 0.70
            ):
                score += 2
            elif (
                current["close"] > current["open"]
                and features["close_position"] >= 0.55
            ):
                score += 1

        else:
            if (
                current["close"] < current["open"]
                and features["close_position"] <= 0.30
            ):
                score += 2
            elif (
                current["close"] < current["open"]
                and features["close_position"] <= 0.45
            ):
                score += 1

        # 5. الاختراق.
        recent = candles[-6:-1]

        if direction == "UP":
            previous_high = max(c["high"] for c in recent)

            if current["close"] > previous_high:
                score += 2
            elif current["close"] > previous["close"]:
                score += 1

        else:
            previous_low = min(c["low"] for c in recent)

            if current["close"] < previous_low:
                score += 2
            elif current["close"] < previous["close"]:
                score += 1

        # 6. توافق اتجاه الفريم الحالي.
        if trend_main == direction:
            score += 1

        # =================================================
        # موانع الإشارة الخطرة
        # =================================================

        candle_range = current["high"] - current["low"]

        if candle_range > current_atr * 2.5:
            diagnostics.append("شمعة كبيرة وغير مناسبة للدخول")

        if direction == "UP" and features["upper_wick"] > 0.55:
            diagnostics.append("ذيل علوي قوي")

        if direction == "DOWN" and features["lower_wick"] > 0.55:
            diagnostics.append("ذيل سفلي قوي")

        if abs(price - e21) > current_atr * 2.5:
            diagnostics.append("السعر بعيد عن المتوسط")

        if current["close"] == current["open"]:
            diagnostics.append("شمعة بلا اتجاه واضح")

        # لا نسمح بإشارة عند وجود مانع خطر.
        hard_blockers = [
            "شمعة كبيرة وغير مناسبة للدخول",
            "ذيل علوي قوي",
            "ذيل سفلي قوي",
            "السعر بعيد عن المتوسط",
            "شمعة بلا اتجاه واضح",
        ]

        blocked = any(
            item in diagnostics for item in hard_blockers
        )

        if blocked:
            return {
                "direction": "WAIT",
                "score": score,
                "price": price,
                "reason": "تم منع الإشارة بسبب مخاطرة فنية",
                "diagnostics": diagnostics,
            }

        if score >= MIN_SCORE:
            return {
                "direction": direction,
                "score": score,
                "price": price,
                "reason": "توافق الاتجاه والمؤشرات",
                "diagnostics": diagnostics,
            }

        return {
            "direction": "WAIT",
            "score": score,
            "price": price,
            "reason": f"النقاط {score} أقل من الحد المطلوب {MIN_SCORE}",
            "diagnostics": diagnostics,
        }

    except Exception as exc:
        logger.exception("Analysis error: %s", exc)

        return {
            "direction": "WAIT",
            "score": 0,
            "price": None,
            "reason": str(exc),
            "diagnostics": ["خطأ في البيانات أو التحليل"],
        }


# =========================================================
# نهاية الجزء الثاني
# =========================================================

# =========================================================
# 11. أزرار تيليجرام
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 تحليل سوق", callback_data="MARKET"),
            InlineKeyboardButton("📊 فحص جماعي", callback_data="SCAN"),
        ],
        [
            InlineKeyboardButton("📈 الإحصائيات", callback_data="STATS"),
            InlineKeyboardButton("📋 المساعدة", callback_data="HELP"),
        ],
    ])


def market_keyboard():
    buttons = []

    names = list(MARKETS.keys())

    for i in range(0, len(names), 2):
        row = [
            InlineKeyboardButton(
                names[i],
                callback_data=f"MKT|{names[i]}",
            )
        ]

        if i + 1 < len(names):
            row.append(
                InlineKeyboardButton(
                    names[i + 1],
                    callback_data=f"MKT|{names[i + 1]}",
                )
            )

        buttons.append(row)

    buttons.append([
        InlineKeyboardButton("⬅️ رجوع", callback_data="HOME")
    ])

    return InlineKeyboardMarkup(buttons)


def interval_keyboard(symbol):
    buttons = []

    names = list(INTERVALS.keys())

    for i in range(0, len(names), 3):
        row = []

        for interval in names[i:i + 3]:
            row.append(
                InlineKeyboardButton(
                    interval,
                    callback_data=f"INT|{symbol}|{interval}",
                )
            )

        buttons.append(row)

    buttons.append([
        InlineKeyboardButton("⬅️ رجوع", callback_data="MARKET")
    ])

    return InlineKeyboardMarkup(buttons)


def signal_keyboard(signal_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶️ بدء متابعة الصفقة",
                callback_data=f"TRADE|{signal_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "✅ WIN",
                callback_data=f"RESULT|{signal_id}|WIN",
            ),
            InlineKeyboardButton(
                "❌ LOSS",
                callback_data=f"RESULT|{signal_id}|LOSS",
            ),
            InlineKeyboardButton(
                "➖ DRAW",
                callback_data=f"RESULT|{signal_id}|DRAW",
            ),
        ],
    ])


# =========================================================
# 12. تنسيق الإشارة
# =========================================================

def format_signal(symbol, interval, result):
    direction = result["direction"]

    if direction == "UP":
        title = "🟢 إشارة صعود UP"
    elif direction == "DOWN":
        title = "🔴 إشارة هبوط DOWN"
    else:
        title = "⏳ لا توجد إشارة WAIT"

    price = result.get("price")

    if price is None:
        price_text = "غير متوفر"
    else:
        price_text = f"{price:.6f}"

    lines = [
        title,
        "",
        f"السوق: {symbol}",
        f"الفريم: {interval}",
        f"السعر المرجعي: {price_text}",
        f"قوة التوافق: {result['score']}",
        f"السبب: {result['reason']}",
    ]

    diagnostics = result.get("diagnostics", [])

    if diagnostics:
        lines.append("")
        lines.append("ملاحظات التحليل:")
        for item in diagnostics[:5]:
            lines.append(f"• {item}")

    lines.extend([
        "",
        "⚠️ إشارة تحليلية وليست ضماناً للربح.",
        "تحقق من السعر الفعلي لدى وسيطك قبل اتخاذ القرار.",
    ])

    return "\n".join(lines)


# =========================================================
# 13. أوامر المستخدم
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً بك في بوت التحليل الفني.\n\n"
        "اختر السوق أو الفحص الجماعي من القائمة:",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "📋 أوامر البوت:\n\n"
        "/start - القائمة الرئيسية\n"
        "/help - المساعدة\n"
        "/stats - الإحصائيات\n"
        "/result ID WIN - تسجيل نتيجة رابحة\n"
        "/result ID LOSS - تسجيل نتيجة خاسرة\n"
        "/result ID DRAW - تسجيل تعادل\n"
        "/timer ID - معرفة الوقت منذ بدء المتابعة\n"
        "/backtest EUR/USD 1min - اختبار تاريخي مبسط\n\n"
        "البوت يعطي إشارات تحليلية فقط ولا ينفذ الصفقات."
    )

    await update.message.reply_text(message)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()

    total = sum(stats.values())
    wins = stats["WIN"]
    losses = stats["LOSS"]
    draws = stats["DRAW"]

    decided = wins + losses

    win_rate = (
        wins / decided * 100
        if decided > 0 else 0
    )

    await update.message.reply_text(
        "📊 إحصائيات الصفقات المسجلة\n\n"
        f"إجمالي النتائج: {total}\n"
        f"WIN: {wins}\n"
        f"LOSS: {losses}\n"
        f"DRAW: {draws}\n"
        f"نسبة الفوز من الصفقات المحسومة: {win_rate:.1f}%\n\n"
        "النسبة تعتمد على النتائج التي سجلتها يدوياً."
    )


async def result_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text(
            "استخدم الأمر بهذا الشكل:\n/result 15 WIN"
        )
        return

    try:
        signal_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("رقم الإشارة غير صحيح.")
        return

    result = context.args[1].upper()

    if not record_result(signal_id, result):
        await update.message.reply_text(
            "لم يتم تسجيل النتيجة. تأكد من رقم الإشارة "
            "وأنها لم تسجل سابقاً."
        )
        return

    await update.message.reply_text(
        f"تم تسجيل نتيجة الإشارة {signal_id}: {result}"
    )


async def timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text(
            "استخدم: /timer 15"
        )
        return

    try:
        signal_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("رقم الإشارة غير صحيح.")
        return

    row = get_signal(signal_id)

    if not row:
        await update.message.reply_text("الإشارة غير موجودة.")
        return

    started = row["trade_started_at"]

    if not started:
        await update.message.reply_text(
            "لم تبدأ متابعة هذه الإشارة بعد. اضغط زر بدء المتابعة."
        )
        return

    elapsed = int(time.time() - started)
    duration = INTERVALS.get(row["interval"], 60)
    remaining = max(0, duration - elapsed)

    await update.message.reply_text(
        f"⏱ متابعة الإشارة رقم {signal_id}\n"
        f"السوق: {row['symbol']}\n"
        f"الفريم: {row['interval']}\n"
        f"الوقت المنقضي: {elapsed} ثانية\n"
        f"الوقت المتبقي: {remaining} ثانية\n\n"
        "هذا عداد متابعة فقط، وليس تأكيداً لانتهاء صلاحية عقد الوسيط."
    )


# =========================================================
# 14. الاختبار التاريخي المبسط
# =========================================================

def backtest(symbol, interval, limit=100):
    candles = get_candles(symbol, interval)

    if len(candles) < 70:
        raise RuntimeError("عدد الشموع غير كافٍ للاختبار.")

    closes = [c["close"] for c in candles]

    wins = 0
    losses = 0
    skipped = 0

    # هذا اختبار مبسط لاتجاه الشمعة التالية.
    # لا يحاكي سعر تنفيذ وسيط EO أو العائد أو السبريد.
    for i in range(60, min(len(candles) - 1, 60 + limit)):
        sample = candles[:i + 1]

        direction = trend_direction(sample)

        if direction == "NEUTRAL":
            skipped += 1
            continue

        expected = direction
        next_candle = candles[i + 1]

        if next_candle["close"] > next_candle["open"]:
            actual = "UP"
        elif next_candle["close"] < next_candle["open"]:
            actual = "DOWN"
        else:
            skipped += 1
            continue

        if expected == actual:
            wins += 1
        else:
            losses += 1

    total = wins + losses

    accuracy = (
        wins / total * 100
        if total else 0
    )

    return {
        "wins": wins,
        "losses": losses,
        "skipped": skipped,
        "total": total,
        "accuracy": accuracy,
    }


async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text(
            "مثال:\n/backtest EUR/USD 1min"
        )
        return

    symbol = context.args[0].upper()
    interval = context.args[1]

    if symbol not in MARKETS or interval not in INTERVALS:
        await update.message.reply_text(
            "السوق أو الفريم غير صحيح."
        )
        return

    message = await update.message.reply_text(
        "⏳ جارٍ تنفيذ الاختبار..."
    )

    try:
        result = await asyncio.to_thread(
            backtest,
            symbol,
            interval,
        )

        await message.edit_text(
            f"📊 اختبار تاريخي مبسط\n\n"
            f"السوق: {symbol}\n"
            f"الفريم: {interval}\n"
            f"عدد الحالات: {result['total']}\n"
            f"WIN: {result['wins']}\n"
            f"LOSS: {result['losses']}\n"
            f"متجاهلة: {result['skipped']}\n"
            f"دقة الاتجاه: {result['accuracy']:.1f}%\n\n"
            "⚠️ لا تمثل هذه النتيجة نسبة ربح فعلية "
            "في وسيط EO Broker."
        )

    except Exception as exc:
        await message.edit_text(
            f"تعذر الاختبار:\n{exc}"
        )


# =========================================================
# 15. الفحص الجماعي
# =========================================================

SCAN_GROUPS = {
    "FOREX": [
        "EUR/USD", "GBP/USD", "USD/JPY",
        "AUD/USD", "USD/CAD", "USD/CHF",
        "NZD/USD", "EUR/GBP", "EUR/JPY",
        "GBP/JPY",
    ],
    "METALS": ["XAU/USD", "XAG/USD"],
    "CRYPTO": ["BTC/USD", "ETH/USD", "SOL/USD"],
}


async def execute_scan(update, context, group):
    query = update.callback_query

    markets = SCAN_GROUPS.get(group, [])

    if not markets:
        await query.message.reply_text("المجموعة غير موجودة.")
        return

    await query.message.reply_text(
        f"🔍 بدأ فحص مجموعة {group}\n"
        "سيتم فحص الفريمات المختارة..."
    )

    intervals = ["1min", "2min", "3min", "5min"]

    found = 0
    wait_count = 0
    errors = 0
    reasons = {}

    for symbol in markets:
        for interval in intervals:
            result = await asyncio.to_thread(
                analyze,
                symbol,
                interval,
            )

            if result["direction"] in ("UP", "DOWN"):
                found += 1
                signal_id = save_signal(
                    symbol,
                    interval,
                    result["direction"],
                    result["score"],
                    result["price"],
                )

                await query.message.reply_text(
                    format_signal(symbol, interval, result)
                    + f"\n\nرقم الإشارة: {signal_id}",
                    reply_markup=signal_keyboard(signal_id),
                )

            else:
                wait_count += 1
                reason = result.get("reason", "غير محدد")
                reasons[reason] = reasons.get(reason, 0) + 1

                if "خطأ" in reason or "مفتاح" in reason:
                    errors += 1

    summary = [
        f"📊 انتهى فحص {group}",
        "",
        f"الإشارات: {found}",
        f"حالات الانتظار: {wait_count}",
        f"أخطاء البيانات: {errors}",
    ]

    if reasons:
        summary.extend(["", "أسباب عدم ظهور الإشارات:"])

        for reason, count in sorted(
            reasons.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:6]:
            summary.append(f"• {reason}: {count}")

    await query.message.reply_text("\n".join(summary))


# =========================================================
# 16. التعامل مع الأزرار
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "HOME":
        await query.message.reply_text(
            "القائمة الرئيسية:",
            reply_markup=main_keyboard(),
        )
        return

    if data == "HELP":
        await help_command_from_button(query)
        return

    if data == "STATS":
        stats = get_stats()
        total = stats["WIN"] + stats["LOSS"] + stats["DRAW"]
        decided = stats["WIN"] + stats["LOSS"]

        rate = (
            stats["WIN"] / decided * 100
            if decided else 0
        )

        await query.message.reply_text(
            f"📊 النتائج المسجلة: {total}\n"
            f"WIN: {stats['WIN']}\n"
            f"LOSS: {stats['LOSS']}\n"
            f"DRAW: {stats['DRAW']}\n"
            f"نسبة الفوز: {rate:.1f}%"
        )
        return

    if data == "MARKET":
        await query.message.reply_text(
            "اختر السوق:",
            reply_markup=market_keyboard(),
        )
        return

    if data == "SCAN":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💱 Forex",
                    callback_data="GROUP|FOREX",
                )
            ],
            [
                InlineKeyboardButton(
                    "🥇 المعادن",
                    callback_data="GROUP|METALS",
                )
            ],
            [
                InlineKeyboardButton(
                    "₿ العملات الرقمية",
                    callback_data="GROUP|CRYPTO",
                )
            ],
        ])

        await query.message.reply_text(
            "اختر مجموعة الفحص:",
            reply_markup=keyboard,
        )
        return

    if data.startswith("MKT|"):
        symbol = data.split("|", 1)[1]

        await query.message.reply_text(
            f"اختر الفريم للسوق {symbol}:",
            reply_markup=interval_keyboard(symbol),
        )
        return

    if data.startswith("INT|"):
        _, symbol, interval = data.split("|")

        await query.message.reply_text(
            f"⏳ جارٍ تحليل {symbol} على {interval}..."
        )

        result = await asyncio.to_thread(
            analyze,
            symbol,
            interval,
        )

        if result["direction"] in ("UP", "DOWN"):
            signal_id = save_signal(
                symbol,
                interval,
                result["direction"],
                result["score"],
                result["price"],
            )

            await query.message.reply_text(
                format_signal(symbol, interval, result)
                + f"\n\nرقم الإشارة: {signal_id}",
                reply_markup=signal_keyboard(signal_id),
            )
        else:
            await query.message.reply_text(
                format_signal(symbol, interval, result)
            )

        return

    if data.startswith("GROUP|"):
        group = data.split("|", 1)[1]
        await execute_scan(update, context, group)
        return

    if data.startswith("TRADE|"):
        signal_id = int(data.split("|")[1])

        with db_connect() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE id = ?",
                (signal_id,),
            ).fetchone()

            if not row:
                await query.message.reply_text("الإشارة غير موجودة.")
                return

            if row["trade_started_at"] is None:
                conn.execute(
                    """
                    UPDATE signals
                    SET trade_started_at = ?
                    WHERE id = ?
                    """,
                    (time.time(), signal_id),
                )
                conn.commit()

        await query.message.reply_text(
            f"▶️ بدأت متابعة الإشارة {signal_id}.\n"
            f"استخدم /timer {signal_id} لمعرفة الوقت المنقضي."
        )
        return

    if data.startswith("RESULT|"):
        _, signal_id, result = data.split("|")
        success = record_result(int(signal_id), result)

        await query.message.reply_text(
            "تم تسجيل النتيجة." if success
            else "تعذر التسجيل؛ ربما سجلت النتيجة سابقاً."
        )
        return


async def help_command_from_button(query):
    await query.message.reply_text(
        "الأوامر:\n"
        "/start\n"
        "/help\n"
        "/stats\n"
        "/result ID WIN أو LOSS أو DRAW\n"
        "/timer ID\n"
        "/backtest EUR/USD 1min"
    )


# =========================================================
# 17. تشغيل البوت
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN غير موجود في متغيرات البيئة."
        )

    if not API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY غير موجود في متغيرات البيئة."
        )

    init_db()

    threading.Thread(
        target=start_web_server,
        daemon=True,
    ).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("result", result_command))
    app.add_handler(CommandHandler("timer", timer_command))
    app.add_handler(CommandHandler("backtest", backtest_command))

    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Trading bot started successfully.")

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()


# =========================================================
# نهاية الكود الكامل
# =========================================================
