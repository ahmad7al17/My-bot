import os
import time
import threading
import asyncio
import logging
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
# 1. إعدادات التشغيل والأمان
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()

# معرف المحادثة لإرسال التنبيهات التلقائية
SUBSCRIBED_CHAT_IDS = set()

if not BOT_TOKEN or not TWELVE_DATA_API_KEY:
    raise RuntimeError(
        "اضبط BOT_TOKEN و TWELVE_DATA_API_KEY في Environment Variables قبل التشغيل."
    )

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("PureStableBot")

API_URL = "https://api.twelvedata.com/time_series"
CACHE_SECONDS = 15
DATA_CACHE = {}
CACHE_LOCK = threading.Lock()

# سجِل لمنع تكرار التنبيه لنفس الإشارة بنفس الشمعة
SENT_ALERTS = {}

# =========================================================
# 2. خادم Render
# =========================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Pure Stable Trading Bot is running."
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
    ("Gold - الذهب", "XAU/USD"),
    ("Silver - الفضة", "XAG/USD"),
    ("Bitcoin", "BTC/USD"),
    ("Ethereum", "ETH/USD"),
    ("Solana", "SOL/USD"),
]

PERIODS = [
    ("30 ثانية", "30sec"),
    ("45 ثانية", "45sec"),
    ("دقيقة", "1min"),
    ("دقيقتان", "2min"),
    ("3 دقائق", "3min"),
    ("5 دقائق", "5min"),
    ("15 دقيقة", "15min"),
    ("30 دقيقة", "30min"),
    ("ساعة", "1h"),
]

UNSUPPORTED_PERIODS = {"30sec", "45sec"}

# الأسواق المخصصة للفحص الدائم بالتنبيهات التلقائية لتجنب الحظر
AUTO_SCAN_MARKETS = [
    ("EUR/USD", "EUR/USD"),
    ("GBP/USD", "GBP/USD"),
    ("Gold - الذهب", "XAU/USD"),
    ("Bitcoin", "BTC/USD"),
]

# =========================================================
# 4. جلب البيانات والتحقق منها
# =========================================================

def parse_datetime(value):
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def get_live_data(symbol, interval, outputsize=300):
    cache_key = (symbol, interval, outputsize)
    now = time.time()

    with CACHE_LOCK:
        cached = DATA_CACHE.get(cache_key)
        if cached and now - cached["time"] < CACHE_SECONDS:
            return cached["candles"]

    response = requests.get(
        API_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVE_DATA_API_KEY,
            "timezone": "UTC",
            "format": "JSON",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        message = data.get("message", "لم يرجع مزود البيانات شموعًا صالحة.")
        raise ValueError(str(message))

    candles = []

    for item in reversed(data["values"]):
        try:
            candle = {
                "datetime": item["datetime"],
                "timestamp": parse_datetime(item["datetime"]).timestamp(),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }

            if not all(candle[k] > 0 for k in ("open", "high", "low", "close")):
                continue

            if candle["high"] < max(candle["open"], candle["close"], candle["low"]):
                continue

            if candle["low"] > min(candle["open"], candle["close"], candle["high"]):
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    if candles:
        candles = candles[:-1]

    if len(candles) < 50:
        raise ValueError("البيانات المكتملة غير كافية.")

    with CACHE_LOCK:
        DATA_CACHE[cache_key] = {"time": now, "candles": candles}

    return candles


def aggregate_candles(candles, minutes):
    if minutes not in (2, 3):
        raise ValueError("فريم التجميع غير مدعوم.")

    groups = {}
    for candle in candles:
        minute_epoch = int(candle["timestamp"] // 60)
        bucket = minute_epoch // minutes
        groups.setdefault(bucket, []).append(candle)

    result = []
    for bucket in sorted(groups):
        group = sorted(groups[bucket], key=lambda c: c["timestamp"])
        if len(group) != minutes:
            continue

        minute_ids = [int(c["timestamp"] // 60) for c in group]
        if any(minute_ids[i] != minute_ids[i - 1] + 1 for i in range(1, len(minute_ids))):
            continue

        result.append({
            "datetime": group[0]["datetime"],
            "timestamp": group[0]["timestamp"],
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
        })

    if len(result) < 45:
        raise ValueError("لا توجد شموع مجمعة كافية.")

    return result


# =========================================================
# 5. المؤشرات الفنية
# =========================================================

def ema(values, period):
    if len(values) < period:
        raise ValueError("بيانات EMA غير كافية.")
    result = [None] * (period - 1)
    seed = sum(values[:period]) / period
    result.append(seed)
    alpha = 2.0 / (period + 1)
    for value in values[period:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def calc_rsi(values, period=14):
    if len(values) < period + 1:
        raise ValueError("بيانات RSI غير كافية.")
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        raise ValueError("بيانات ATR غير كافية.")
    true_ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]
        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        )
        true_ranges.append(true_range)
    atr = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def market_trend(candles):
    close = [c["close"] for c in candles]
    e20 = ema(close, 20)
    e50 = ema(close, 50)
    last = close[-1]
    if last > e20[-1] > e50[-1]:
        return "صاعد"
    if last < e20[-1] < e50[-1]:
        return "هابط"
    return "محايد"


# =========================================================
# 6. تحليل الإشارة
# =========================================================

def analyze(symbol, period):
    if period in UNSUPPORTED_PERIODS:
        raise ValueError("فريم غير مدعوم.")

    base = get_live_data(symbol, "1min")

    if period == "1min":
        candles = base
    elif period == "2min":
        candles = aggregate_candles(base, 2)
    elif period == "3min":
        candles = aggregate_candles(base, 3)
    elif period in ("5min", "15min", "30min", "1h"):
        candles = get_live_data(symbol, period)
    else:
        raise ValueError("الفريم غير معروف.")

    if not candles:
        raise ValueError("بيانات غير كافية.")

    close = [c["close"] for c in candles]
    last = close[-1]
    previous = close[-2]

    e9 = ema(close, 9)
    e21 = ema(close, 21)
    e50 = ema(close, 50)

    rsi = calc_rsi(close)
    atr = calc_atr(candles)

    e12 = ema(close, 12)
    e26 = ema(close, 26)
    macd_line = [(v12 - v26) if v12 and v26 else 0.0 for v12, v26 in zip(e12, e26)]
    macd_signal = ema(macd_line, 9)
    macd_hist = macd_line[-1] - macd_signal[-1] if macd_signal[-1] is not None else 0.0

    candles5 = get_live_data(symbol, "5min")
    candles15 = get_live_data(symbol, "15min")
    trend5 = market_trend(candles5)
    trend15 = market_trend(candles15)

    buy_score = 0
    sell_score = 0
    reasons = []

    if trend5 == "صاعد" and trend15 == "صاعد":
        buy_score += 3
        reasons.append("اتجاه 5 و15 دقيقة صاعد")
    elif trend5 == "هابط" and trend15 == "هابط":
        sell_score += 3
        reasons.append("اتجاه 5 و15 دقيقة هابط")

    if last > e21[-1] and e9[-1] > e21[-1]:
        buy_score += 2
        reasons.append("السعر والمتوسطات يدعمان الصعود")

    if last < e21[-1] and e9[-1] < e21[-1]:
        sell_score += 2
        reasons.append("السعر والمتوسطات يدعمان الهبوط")

    if macd_hist > 0 and rsi >= 52:
        buy_score += 1
        reasons.append("زخم إيجابي")

    if macd_hist < 0 and rsi <= 48:
        sell_score += 1
        reasons.append("زخم سلبي")

    candle = candles[-1]
    body = candle["close"] - candle["open"]
    candle_range = candle["high"] - candle["low"]

    if candle_range > 0:
        body_ratio = abs(body) / candle_range
        lower_wick = min(candle["open"], candle["close"]) - candle["low"]
        upper_wick = candle["high"] - max(candle["open"], candle["close"])

        if body > 0 and body_ratio >= 0.55:
            buy_score += 1
            reasons.append("شمعة صاعدة قوية نسبيًا")

        if body < 0 and body_ratio >= 0.55:
            sell_score += 1
            reasons.append("شمعة هابطة قوية نسبيًا")

        if body > 0 and lower_wick / candle_range >= 0.45:
            buy_score += 1
            reasons.append("رفض سعري من الأسفل")

        if body < 0 and upper_wick / candle_range >= 0.45:
            sell_score += 1
            reasons.append("رفض سعري من الأعلى")

    extension = abs(last - e21[-1])
    not_overextended = extension <= 1.5 * atr

    buy_allowed = (
        trend5 == "صاعد"
        and trend15 == "صاعد"
        and last > e21[-1]
        and e9[-1] > e21[-1]
        and macd_hist > 0
        and 52 <= rsi <= 68
        and buy_score >= 6
        and buy_score > sell_score
        and not_overextended
        and last > previous
    )

    sell_allowed = (
        trend5 == "هابط"
        and trend15 == "هابط"
        and last < e21[-1]
        and e9[-1] < e21[-1]
        and macd_hist < 0
        and 32 <= rsi <= 48
        and sell_score >= 6
        and sell_score > buy_score
        and not_overextended
        and last < previous
    )

    if buy_allowed:
        signal = "🟢 شراء محتمل — بعد التحقق"
    elif sell_allowed:
        signal = "🔴 بيع محتمل — بعد التحقق"
    else:
        signal = "⚪ انتظار — لا يوجد توافق كافٍ"

    if buy_allowed:
        stop_reference = last - 1.5 * atr
        target_reference = last + 2.0 * atr
    elif sell_allowed:
        stop_reference = last + 1.5 * atr
        target_reference = last - 2.0 * atr
    else:
        stop_reference = None
        target_reference = None

    return {
        "price": last,
        "rsi": rsi,
        "ema9": e9[-1],
        "ema21": e21[-1],
        "ema50": e50[-1],
        "macd": macd_hist,
        "atr": atr,
        "trend5": trend5,
        "trend15": trend15,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "signal": signal,
        "reasons": reasons,
        "stop_reference": stop_reference,
        "target_reference": target_reference,
        "time": candle["datetime"],
    }


# =========================================================
# 7. وظيفة التنبيهات التلقائية في الخلفية
# =========================================================

async def auto_market_scanner(app):
    while True:
        try:
            await asyncio.sleep(300) # فحص تلقائي كل 5 دقائق
            if not SUBSCRIBED_CHAT_IDS:
                continue

            for name, sym in AUTO_SCAN_MARKETS:
                for period in ["5min"]:
                    try:
                        res = await asyncio.to_thread(analyze, sym, period)
                        if "شراء" in res["signal"] or "بيع" in res["signal"]:
                            alert_id = f"{sym}_{period}_{res['time']}"
                            
                            # تجنب تكرار التنبيه لنفس الشمعة
                            if SENT_ALERTS.get(alert_id):
                                continue
                            
                            SENT_ALERTS[alert_id] = True

                            reasons_txt = "\n".join("• " + r for r in res["reasons"])
                            msg = (
                                f"🚨 **تنبيه فرصة جديدة تلقائية!** 🚨\n\n"
                                f"📊 السوق: {name} ({sym})\n"
                                f"⏱️ الفريم: 5 دقائق\n"
                                f"🎯 الإشارة: {res['signal']}\n"
                                f"💵 السعر الحالي: {res['price']:.6g}\n\n"
                                f"🛑 وقف مرجعي: {res['stop_reference']:.6g}\n"
                                f"🎯 هدف مرجعي: {res['target_reference']:.6g}\n\n"
                                f"🔎 الأسباب:\n{reasons_txt}\n\n"
                                f"⚠️ تحقق من شارت المنصة قبل التنفيذ."
                            )

                            for chat_id in list(SUBSCRIBED_CHAT_IDS):
                                try:
                                    await app.bot.send_message(chat_id=chat_id, text=msg)
                                except Exception:
                                    pass

                        await asyncio.sleep(2) # مهلة بين الأسواق
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Error in auto scanner: {e}")
            await asyncio.sleep(30)


# =========================================================
# 8. واجهة تيليجرام
# =========================================================

def markets_keyboard():
    rows = []
    for i in range(0, len(MARKETS), 2):
        row = [InlineKeyboardButton(MARKETS[i][0], callback_data="M|" + MARKETS[i][1])]
        if i + 1 < len(MARKETS):
            row.append(InlineKeyboardButton(MARKETS[i + 1][0], callback_data="M|" + MARKETS[i + 1][1]))
        rows.append(row)

    rows.append([InlineKeyboardButton("🔍 رئيسية [1د، 2د، 5د]", callback_data="SCAN_GRP1_125")])
    rows.append([InlineKeyboardButton("🔍 سلع ومتقاطعات [1د، 2د، 5د]", callback_data="SCAN_GRP2_125")])
    rows.append([InlineKeyboardButton("🔍 رقمية [1د، 2د، 5د]", callback_data="SCAN_GRP3_125")])

    rows.append([InlineKeyboardButton("⭐ رئيسية [3د، 15د، 30د، 1ساعة]", callback_data="SCAN_GRP1_BIG")])
    rows.append([InlineKeyboardButton("⭐ سلع ومتقاطعات [3د، 15د، 30د، 1ساعة]", callback_data="SCAN_GRP2_BIG")])
    rows.append([InlineKeyboardButton("⭐ رقمية [3د، 15د، 30د، 1ساعة]", callback_data="SCAN_GRP3_BIG")])

    return InlineKeyboardMarkup(rows)


def period_keyboard(symbol):
    rows = [[InlineKeyboardButton("⏱️ " + label, callback_data=f"P|{symbol}|{period}")] for label, period in PERIODS]
    rows.append([InlineKeyboardButton("⬅️ الأسواق", callback_data="BACK")])
    return InlineKeyboardMarkup(rows)


def again_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحليل سوق آخر", callback_data="BACK")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    SUBSCRIBED_CHAT_IDS.add(chat_id)

    await update.message.reply_text(
        "🤖 Pure Stable Trading Bot\n\n"
        "اختر السوق لتحليل الشموع والمؤشرات أو استخدم أزرار الفحص الذكي المنسقة أدناه.\n"
        "🔔 **ميزة التنبيهات التلقائية مفعّلة لك الآن!** سأنبهك فور اكتشاف فرصة مؤكدة.\n"
        "الإشارات احتمالية وليست ضمانًا للربح.",
        reply_markup=markets_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - عرض الأسواق وتشغيل التنبيهات\n"
        "/help - المساعدة\n\n"
        "فريمَا 30 و45 ثانية غير مفعّلين مع مصدر البيانات الحالي."
    )


async def execute_group_scan(query, markets_list, group_name, scan_periods):
    await query.edit_message_text(f"🔍 جاري فحص ({group_name})... يرجى الانتظار قليلاً لمعالجة الشموع.")
    found_signals = []
    for name, sym in markets_list:
        for per in scan_periods:
            try:
                res = await asyncio.to_thread(analyze, sym, per)
                if "شراء" in res["signal"] or "بيع" in res["signal"]:
                    found_signals.append(f"• {name} [{per}]: {res['signal']}")
                time.sleep(0.4)
            except Exception:
                continue

    if found_signals:
        msg = f"🟢 **الفرص المتاحة في ({group_name}):**\n\n" + "\n".join(found_signals)
    else:
        msg = f"⚪ **لا توجد فرص مطابقة للشروط الصارمة حالياً في ({group_name}).**"

    await query.edit_message_text(msg, reply_markup=again_keyboard())


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "BACK":
        await query.edit_message_text("📊 اختر السوق:", reply_markup=markets_keyboard())
        return

    if query.data == "SCAN_GRP1_125":
        await execute_group_scan(query, [("EUR/USD", "EUR/USD"), ("GBP/USD", "GBP/USD"), ("USD/JPY", "USD/JPY")], "العملات الرئيسية (1، 2، 5 د)", ["1min", "2min", "5min"])
        return
    if query.data == "SCAN_GRP2_125":
        await execute_group_scan(query, [("AUD/USD", "AUD/USD"), ("USD/CAD", "USD/CAD"), ("Gold - الذهب", "XAU/USD"), ("Silver - الفضة", "XAG/USD")], "المتقاطعات والسلع (1، 2، 5 د)", ["1min", "2min", "5min"])
        return
    if query.data == "SCAN_GRP3_125":
        await execute_group_scan(query, [("Bitcoin", "BTC/USD"), ("Ethereum", "ETH/USD"), ("Solana", "SOL/USD")], "العملات الرقمية (1، 2، 5 د)", ["1min", "2min", "5min"])
        return

    big_periods = ["3min", "15min", "30min", "1h"]
    if query.data == "SCAN_GRP1_BIG":
        await execute_group_scan(query, [("EUR/USD", "EUR/USD"), ("GBP/USD", "GBP/USD"), ("USD/JPY", "USD/JPY")], "العملات الرئيسية (الفريمات الكبرى)", big_periods)
        return
    if query.data == "SCAN_GRP2_BIG":
        await execute_group_scan(query, [("AUD/USD", "AUD/USD"), ("USD/CAD", "USD/CAD"), ("Gold - الذهب", "XAU/USD"), ("Silver - الفضة", "XAG/USD")], "المتقاطعات والسلع (الفريمات الكبرى)", big_periods)
        return
    if query.data == "SCAN_GRP3_BIG":
        await execute_group_scan(query, [("Bitcoin", "BTC/USD"), ("Ethereum", "ETH/USD"), ("Solana", "SOL/USD")], "العملات الرقمية (الفريمات الكبرى)", big_periods)
        return

    if query.data.startswith("M|"):
        symbol = query.data.split("|", 1)[1]
        await query.edit_message_text(f"السوق: {symbol}\nاختر فريم التحليل:", reply_markup=period_keyboard(symbol))
        return

    if not query.data.startswith("P|"):
        return

    _, symbol, period = query.data.split("|")
    labels = dict((value, label) for label, value in PERIODS)

    await query.edit_message_text("⏳ جارٍ تحليل البيانات المكتملة...")

    try:
        result = await asyncio.to_thread(analyze, symbol, period)
        reasons = "\n".join("• " + reason for reason in result["reasons"])

        stop_text = f"{result['stop_reference']:.6g}" if result["stop_reference"] is not None else "لا يوجد — انتظار"
        target_text = f"{result['target_reference']:.6g}" if result["target_reference"] is not None else "لا يوجد — انتظار"

        message = (
            f"📊 السوق: {symbol}\n"
            f"⏱️ الفريم: {labels.get(period, period)}\n\n"
            f"💵 آخر إغلاق مكتمل: {result['price']:.6g}\n"
            f"🎯 الإشارة: {result['signal']}\n\n"
            f"📈 EMA9: {result['ema9']:.6g}\n"
            f"📈 EMA21: {result['ema21']:.6g}\n"
            f"📈 EMA50: {result['ema50']:.6g}\n"
            f"📊 RSI14: {result['rsi']:.2f}\n"
            f"〽️ MACD Histogram: {result['macd']:.6g}\n"
            f"📏 ATR14: {result['atr']:.6g}\n\n"
            f"🧭 اتجاه 5 دقائق: {result['trend5']}\n"
            f"🧭 اتجاه 15 دقيقة: {result['trend15']}\n"
            f"🟢 نقاط الشراء: {result['buy_score']}\n"
            f"🔴 نقاط البيع: {result['sell_score']}\n\n"
            f"🛑 مستوى وقف مرجعي: {stop_text}\n"
            f"🎯 هدف مرجعي: {target_text}\n\n"
            f"🔎 أسباب التحليل:\n{reasons}\n\n"
            "⚠️ المستويات تقديرية وليست أوامر تنفيذ.\n"
            "قد يختلف السعر عن منصة الوسيط.\n"
            "لا توجد إشارة مضمونة الربح."
        )

        await query.edit_message_text(message, reply_markup=again_keyboard())

    except Exception as error:
        logger.exception("Analysis failed")
        await query.edit_message_text(
            "❌ تعذر إكمال التحليل.\n\n"
            f"السبب: {str(error)[:700]}\n\n"
            "تحقق من رمز السوق، وحدود API، وتوفر البيانات لهذا الفريم.",
            reply_markup=again_keyboard(),
        )


# =========================================================
# 9. التشغيل
# =========================================================

async def on_startup(application):
    """بدء مهمة الخلفية للتنبيهات التلقائية"""
    asyncio.create_task(auto_market_scanner(application))
    logger.info("Auto market scanner task started successfully.")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(buttons))

    # ربط دالة البدء بالطريقة الصحيحة لتجنب انهيار مهام الخلفية
    app.post_init = on_startup

    logger.info("Pure Stable Trading Bot with Auto Scanner started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
