import os
import time
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================================
# 1. إعدادات التشغيل والتوكن
# =========================================================

BOT_TOKEN = "8382243160:AAHLLZ92-oLA5F61lEigu1XYEbZ9Ekubzf8"
API_KEY = "cbc8383f864f46f29849768f049f1b5d"

PRICE_MATCH_VERIFIED = True

BASE_URL = "https://api.twelvedata.com"
DB_PATH = "trading_signals.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("EO_SignalBot")

# =========================================================
# 2. خادم Render البسيط لإبقاء المنفذ مفتوحاً وإسكات التحذير
# =========================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"EO Signal Bot is running."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# تشغيل خادم الويب في خلفية النظام ليطابق متطلبات Render
threading.Thread(target=run_server, daemon=True).start()

# =========================================================
# 3. الأسواق والفريمات
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

EXPIRIES = [
    ("30 ثانية", 30),
    ("45 ثانية", 45),
    ("دقيقة", 60),
    ("دقيقتان", 120),
    ("3 دقائق", 180),
]

CACHE = {}
CACHE_LOCK = threading.Lock()
CACHE_TTL = 20

# =========================================================
# 4. قاعدة بيانات سجل الإشارات
# =========================================================

def db_connect():
    return sqlite3.connect(DB_PATH, timeout=15)


def init_db():
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                expiry_seconds INTEGER NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                score INTEGER NOT NULL,
                result TEXT DEFAULT 'PENDING'
            )
        """)
        con.commit()


def save_signal(symbol, expiry, direction, price, score):
    with db_connect() as con:
        cur = con.execute(
            """INSERT INTO signals
               (created_at, symbol, expiry_seconds, direction,
                entry_price, score, result)
               VALUES (?, ?, ?, ?, ?, ?, 'PENDING')""",
            (
                datetime.now(timezone.utc).isoformat(),
                symbol,
                expiry,
                direction,
                price,
                score,
            ),
        )
        con.commit()
        return cur.lastrowid


def set_result(signal_id, result):
    with db_connect() as con:
        cur = con.execute(
            """UPDATE signals SET result = ?
               WHERE id = ? AND result = 'PENDING'""",
            (result, signal_id),
        )
        con.commit()
        return cur.rowcount > 0


def get_stats():
    with db_connect() as con:
        rows = con.execute(
            """SELECT result, COUNT(*)
               FROM signals GROUP BY result"""
        ).fetchall()

    return {result: count for result, count in rows}


# =========================================================
# 5. بيانات Twelve Data
# =========================================================

def api_get(endpoint, params):
    params = dict(params)
    params["apikey"] = API_KEY

    response = requests.get(
        f"{BASE_URL}/{endpoint}",
        params=params,
        timeout=15,
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
        raise ValueError("لم يصل سعر لحظي صالح.")

    price = float(data["price"])

    if price <= 0:
        raise ValueError("السعر المستلم غير صالح.")

    return price


def get_candles(symbol, interval, limit=150):
    key = (symbol, interval, limit)
    now = time.time()

    with CACHE_LOCK:
        cached = CACHE.get(key)
        if cached and now - cached["time"] < CACHE_TTL:
            return cached["candles"]

    data = api_get(
        "time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": limit,
            "timezone": "UTC",
            "format": "JSON",
        },
    )

    if "values" not in data:
        raise ValueError("لا توجد شموع متاحة لهذا السوق.")

    seconds = {
        "1min": 60,
        "5min": 300,
        "15min": 900,
    }[interval]

    candles = []

    for item in reversed(data["values"]):
        try:
            dt = datetime.strptime(
                item["datetime"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)

            candle = {
                "timestamp": dt.timestamp(),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }

            if not all(
                candle[k] > 0
                for k in ("open", "high", "low", "close")
            ):
                continue

            if candle["high"] < max(
                candle["open"], candle["close"]
            ):
                continue

            if candle["low"] > min(
                candle["open"], candle["close"]
            ):
                continue

            if dt.timestamp() + seconds <= time.time():
                candles.append(candle)

        except (ValueError, TypeError, KeyError):
            continue

    if len(candles) < 60:
        raise ValueError(
            f"الشموع المكتملة غير كافية للفريم {interval}."
        )

    with CACHE_LOCK:
        CACHE[key] = {
            "time": now,
            "candles": candles,
        }

    return candles


# =========================================================
# 6. المؤشرات الفنية
# =========================================================

def ema(values, period):
    if len(values) < period:
        raise ValueError("بيانات EMA غير كافية.")

    alpha = 2 / (period + 1)
    result = [sum(values[:period]) / period]

    for value in values[period:]:
        result.append(
            alpha * value + (1 - alpha) * result[-1]
        )

    return [None] * (period - 1) + result


def rsi(values, period=14):
    if len(values) < period + 1:
        raise ValueError("بيانات RSI غير كافية.")

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(candles, period=14):
    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))

    if len(ranges) < period:
        raise ValueError("بيانات ATR غير كافية.")

    value = sum(ranges[:period]) / period

    for x in ranges[period:]:
        value = (value * (period - 1) + x) / period

    return value


def trend(candles):
    closes = [c["close"] for c in candles]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    last = closes[-1]

    if last > e20[-1] > e50[-1]:
        return "UP"
    if last < e20[-1] < e50[-1]:
        return "DOWN"

    return "NEUTRAL"


# =========================================================
# 7. محرك الإشارات
# =========================================================

def analyze(symbol, expiry):
    if expiry in (30, 45):
        return {
            "direction": "WAIT",
            "reason": (
                "فريم انتهاء الصفقة أقصر من دقة مصدر الشموع "
                "المتاح. يلزم مصدر بيانات لحظي أدق."
            ),
        }

    candles1 = get_candles(symbol, "1min")
    candles5 = get_candles(symbol, "5min")
    candles15 = get_candles(symbol, "15min")

    close = [c["close"] for c in candles1]
    current = candles1[-1]
    previous = candles1[-2]

    e9 = ema(close, 9)
    e21 = ema(close, 21)
    current_rsi = rsi(close)
    current_atr = atr(candles1)

    trend5 = trend(candles5)
    trend15 = trend(candles15)

    body = abs(current["close"] - current["open"])
    candle_range = current["high"] - current["low"]

    if candle_range <= 0:
        return {
            "direction": "WAIT",
            "reason": "مدى الشمعة غير صالح.",
        }

    body_ratio = body / candle_range

    buy_score = 0
    sell_score = 0
    reasons = []

    if trend5 == "UP" and trend15 == "UP":
        buy_score += 2
        reasons.append("اتجاه 5 و15 دقيقة صاعد")
    elif trend5 == "DOWN" and trend15 == "DOWN":
        sell_score += 2
        reasons.append("اتجاه 5 و15 دقيقة هابط")
    else:
        reasons.append("الاتجاهات الأكبر غير متوافقة")

    if current["close"] > e21[-1] and e9[-1] > e21[-1]:
        buy_score += 2
        reasons.append("المتوسطات تدعم الصعود")

    if current["close"] < e21[-1] and e9[-1] < e21[-1]:
        sell_score += 2
        reasons.append("المتوسطات تدعم الهبوط")

    if 50 < current_rsi < 68:
        buy_score += 1
        reasons.append("زخم إيجابي غير متطرف")

    if 32 < current_rsi < 50:
        sell_score += 1
        reasons.append("زخم سلبي غير متطرف")

    if current["close"] > current["open"] and body_ratio >= 0.50:
        buy_score += 1
        reasons.append("شمعة صاعدة ذات جسم واضح")

    if current["close"] < current["open"] and body_ratio >= 0.50:
        sell_score += 1
        reasons.append("شمعة هابطة ذات جسم واضح")

    if current["close"] > previous["high"]:
        buy_score += 1
        reasons.append("إغلاق فوق قمة الشمعة السابقة")

    if current["close"] < previous["low"]:
        sell_score += 1
        reasons.append("إغلاق دون قاع الشمعة السابقة")

    if (
        trend5 == "UP"
        and trend15 == "UP"
        and buy_score >= 5
        and buy_score > sell_score
        and current_rsi < 70
    ):
        direction = "UP"
    elif (
        trend5 == "DOWN"
        and trend15 == "DOWN"
        and sell_score >= 5
        and sell_score > buy_score
        and current_rsi > 30
    ):
        direction = "DOWN"
    else:
        direction = "WAIT"

    live_price = get_price(symbol)

    return {
        "direction": direction,
        "price": live_price,
        "candle_close": current["close"],
        "rsi": current_rsi,
        "atr": current_atr,
        "trend5": trend5,
        "trend15": trend15,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "reasons": reasons,
        "expiry": expiry,
    }


# =========================================================
# 8. واجهة تيليجرام
# =========================================================

def main_keyboard():
    rows = []

    for i in range(0, len(MARKETS), 2):
        row = [
            InlineKeyboardButton(
                MARKETS[i][0],
                callback_data=f"M|{MARKETS[i][1]}",
            )
        ]

        if i + 1 < len(MARKETS):
            row.append(
                InlineKeyboardButton(
                    MARKETS[i + 1][0],
                    callback_data=f"M|{MARKETS[i + 1][1]}",
                )
            )

        rows.append(row)

    rows.append([
        InlineKeyboardButton("📊 الإحصائيات", callback_data="STATS")
    ])

    return InlineKeyboardMarkup(rows)


def expiry_keyboard(symbol):
    rows = []

    for label, seconds in EXPIRIES:
        rows.append([
            InlineKeyboardButton(
                f"⏱️ {label}",
                callback_data=f"E|{symbol}|{seconds}",
            )
        ])

    rows.append([
        InlineKeyboardButton("⬅️ الأسواق", callback_data="BACK")
    ])

    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🤖 محلل إشارات EO\n\n"
        "تحليل فقط — لا ينفذ أي صفقة.\n\n"
        "اختر السوق:",
        reply_markup=main_keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "الأوامر:\n"
        "/start - القائمة الرئيسية\n"
        "/compare EUR/USD 1.08500 - مقارنة السعر مع EO\n"
        "/result 12 WIN - تسجيل فوز للإشارة رقم 12\n"
        "/result 12 LOSS - تسجيل خسارة\n"
        "/result 12 TIE - تسجيل تعادل\n"
        "/stats - إحصائيات النتائج"
    )


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "الصيغة:\n/compare EUR/USD 1.08500"
        )
        return

    symbol = context.args[0].upper()

    try:
        eo_price = float(context.args[1])
        data_price = await asyncio.to_thread(get_price, symbol)

        difference = data_price - eo_price
        pct = abs(difference) / eo_price * 100 if eo_price else 0

        await update.effective_message.reply_text(
            f"السوق: {symbol}\n"
            f"سعر EO: {eo_price:.8g}\n"
            f"سعر Twelve Data: {data_price:.8g}\n"
            f"الفرق: {difference:.8g}\n"
            f"الفرق النسبي: {pct:.5f}%"
        )

    except Exception as e:
        await update.effective_message.reply_text(
            f"تعذرت المقارنة: {str(e)[:250]}"
        )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await asyncio.to_thread(get_stats)

    wins = stats.get("WIN", 0)
    losses = stats.get("LOSS", 0)
    ties = stats.get("TIE", 0)
    pending = stats.get("PENDING", 0)

    decided = wins + losses
    win_rate = (wins / decided * 100) if decided else 0

    await update.effective_message.reply_text(
        "📊 سجل الإشارات\n\n"
        f"فوز: {wins}\n"
        f"خسارة: {losses}\n"
        f"تعادل: {ties}\n"
        f"بانتظار تسجيل النتيجة: {pending}\n"
        f"نسبة الفوز بين الصفقات المحسومة: {win_rate:.1f}%"
    )


async def result_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "الصيغة: /result 12 WIN\n"
            "النتائج المتاحة: WIN أو LOSS أو TIE"
        )
        return

    try:
        signal_id = int(context.args[0])
        result = context.args[1].upper()

        if result not in ("WIN", "LOSS", "TIE"):
            raise ValueError("النتيجة يجب أن تكون WIN أو LOSS أو TIE.")

        updated = await asyncio.to_thread(
            set_result, signal_id, result
        )

        if updated:
            await update.effective_message.reply_text(
                f"تم تسجيل نتيجة الإشارة {signal_id}: {result}"
            )
        else:
            await update.effective_message.reply_text(
                "لم يتم العثور على إشارة معلقة بهذا الرقم."
            )

    except ValueError as e:
        await update.effective_message.reply_text(str(e))


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "BACK":
        await query.edit_message_text(
            "اختر السوق:",
            reply_markup=main_keyboard(),
        )
        return

    if query.data == "STATS":
        stats = await asyncio.to_thread(get_stats)
        wins = stats.get("WIN", 0)
        losses = stats.get("LOSS", 0)
        ties = stats.get("TIE", 0)
        pending = stats.get("PENDING", 0)
        decided = wins + losses
        rate = wins / decided * 100 if decided else 0

        await query.edit_message_text(
            "📊 سجل النتائج\n\n"
            f"فوز: {wins}\nخسارة: {losses}\n"
            f"تعادل: {ties}\nمعلقة: {pending}\n"
            f"نسبة الفوز بين الصفقات المحسومة: {rate:.1f}%",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ الأسواق", callback_data="BACK")
            ]]),
        )
        return

    if query.data.startswith("M|"):
        symbol = query.data.split("|", 1)[1]
        await query.edit_message_text(
            f"السوق: {symbol}\nاختر مدة انتهاء الصفقة:",
            reply_markup=expiry_keyboard(symbol),
        )
        return

    if not query.data.startswith("E|"):
        return

    _, symbol, seconds_text = query.data.split("|")
    expiry = int(seconds_text)

    await query.edit_message_text(
        "⏳ جارٍ فحص البيانات والشموع المكتملة..."
    )

    try:
        res = await asyncio.to_thread(analyze, symbol, expiry)

        if res["direction"] == "WAIT":
            message = (
                f"⚪ لا توجد إشارة دخول\n"
                f"السوق: {symbol}\n"
                f"مدة الصفقة: {expiry} ثانية\n\n"
                f"السبب: {res['reason']}"
            )
        else:
            direction = res["direction"]
            score = (
                res["buy_score"]
                if direction == "UP"
                else res["sell_score"]
            )

            signal_id = await asyncio.to_thread(
                save_signal,
                symbol,
                expiry,
                direction,
                res["price"],
                score,
            )

            reasons = "\n".join(
                "• " + x for x in res["reasons"]
            )

            message = (
                f"{'🟢 إشارة صعود' if direction == 'UP' else '🔴 إشارة هبوط'}\n\n"
                f"السوق: {symbol}\n"
                f"مدة انتهاء الصفقة: {expiry} ثانية\n"
                f"السعر الحالي: {res['price']:.8g}\n"
                f"إغلاق آخر شمعة: {res['candle_close']:.8g}\n"
                f"RSI: {res['rsi']:.1f}\n"
                f"ATR: {res['atr']:.8g}\n"
                f"اتجاه 5 دقائق: {res['trend5']}\n"
                f"اتجاه 15 دقيقة: {res['trend15']}\n\n"
                f"أسباب التحليل:\n{reasons}\n\n"
                f"رقم الإشارة: {signal_id}\n"
                f"لتسجيل النتيجة لاحقاً:\n"
                f"/result {signal_id} WIN أو LOSS"
            )

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 سوق آخر", callback_data="BACK")
            ]]),
        )

    except Exception as e:
        logger.exception("Analysis failed")

        await query.edit_message_text(
            "❌ تعذر إكمال التحليل.\n"
            f"{str(e)[:350]}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ الأسواق", callback_data="BACK")
            ]]),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


# =========================================================
# 9. التشغيل النهائي
# =========================================================

def main():
    try:
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=10)
    except Exception:
        pass

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(buttons))

    app.add_error_handler(error_handler)

    logger.info("EO Signal Bot started successfully with web server.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
