import os
import time
import threading
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# =========================================================
# 1. إعدادات التشغيل والتوكن
# =========================================================

BOT_TOKEN = "8382243160:AAHLLZ92-oLA5F61lEigu1XYEbZ9Ekubzf8"
TWELVE_DATA_API_KEY = "cbc8383f864f46f29849768f049f1b5d"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ProTradingBot")

API_URL = "https://api.twelvedata.com/time_series"
CACHE_SECONDS = 60
DATA_CACHE = {}
CACHE_LOCK = threading.Lock()

# =========================================================
# 2. خادم Render
# =========================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Pro Trading Bot with Reversal Detection is running."
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
    ("4 دقائق", "4min"),
    ("5 دقائق", "5min"),
    ("15 دقيقة", "15min"),
    ("30 دقيقة", "30min"),
    ("ساعة", "1h"),
]

UNSUPPORTED_PERIODS = {"30sec", "45sec"}

PERIOD_MINUTES_MAP = {
    "1min": 1,
    "2min": 2,
    "3min": 3,
    "4min": 4,
    "5min": 5,
    "15min": 15,
    "30min": 30,
    "1h": 60,
}

# =========================================================
# 4. جلب وتجميع البيانات
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

    time.sleep(1.2)

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
    
    if response.status_code == 429:
        raise ValueError("تم تجاوز حد الطلبات المسموح به (429). يرجى الانتظار قليلاً.")

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
            if all(candle[k] > 0 for k in ("open", "high", "low", "close")):
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
    if minutes not in (2, 3, 4):
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
        result.append({
            "datetime": group[0]["datetime"],
            "timestamp": group[0]["timestamp"],
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
        })

    if len(result) < 40:
        raise ValueError("لا توجد شموع مجمعة كافية.")
    return result


# =========================================================
# 5. المؤشرات المتقدمة وفلترة الشموع الانعكاسية
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
        return 50.0
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.001
    true_ranges = []
    for i in range(1, len(candles)):
        c = candles[i]
        pc = candles[i - 1]["close"]
        tr = max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc))
        true_ranges.append(tr)
    atr = sum(true_ranges[:period]) / period
    for val in true_ranges[period:]:
        atr = (atr * (period - 1) + val) / period
    return atr


def calc_bollinger_bands(values, period=20, num_std=2.0):
    if len(values) < period:
        return values[-1], values[-1], values[-1]
    sma = sum(values[-period:]) / period
    variance = sum((x - sma) ** 2 for x in values[-period:]) / period
    std_dev = variance ** 0.5
    return sma + (num_std * std_dev), sma, sma - (num_std * std_dev)


def calc_adx(candles, period=14):
    if len(candles) < period * 2:
        return 25.0
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l, ph, pl, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        up = h - ph
        down = pl - l
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    atr = sum(tr_list[-period:]) / period
    p_di = 100 * (sum(plus_dm[-period:]) / period) / (atr if atr > 0 else 1)
    m_di = 100 * (sum(minus_dm[-period:]) / period) / (atr if atr > 0 else 1)
    dx_den = (p_di + m_di)
    return 100 * (abs(p_di - m_di) / dx_den) if dx_den > 0 else 0.0


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
# 6. محرك التحليل فائق الدقة (مع رصد الشموع الانعكاسية)
# =========================================================

def analyze(symbol, period):
    if period in UNSUPPORTED_PERIODS:
        raise ValueError("فريم غير مدعوم.")

    base = get_live_data(symbol, "1min")

    if period == "1min":
        candles = base
    elif period in ("2min", "3min", "4min"):
        candles = aggregate_candles(base, PERIOD_MINUTES_MAP[period])
    else:
        candles = get_live_data(symbol, period)

    close = [c["close"] for c in candles]
    last = close[-1]
    previous = close[-2]

    curr_candle = candles[-1]
    prev_candle = candles[-2]

    body_size = abs(curr_candle["close"] - curr_candle["open"])
    total_range = curr_candle["high"] - curr_candle["low"]
    upper_wick = curr_candle["high"] - max(curr_candle["open"], curr_candle["close"])
    lower_wick = min(curr_candle["open"], curr_candle["close"]) - curr_candle["low"]

    is_valid_body = total_range > 0 and (body_size / total_range) >= 0.35
    has_strong_lower_wick = total_range > 0 and (lower_wick / total_range) >= 0.45
    has_strong_upper_wick = total_range > 0 and (upper_wick / total_range) >= 0.45

    is_bullish_engulfing = (curr_candle["close"] > curr_candle["open"]) and (prev_candle["close"] < prev_candle["open"]) and (curr_candle["close"] >= prev_candle["open"])
    is_bearish_engulfing = (curr_candle["close"] < curr_candle["open"]) and (prev_candle["close"] > prev_candle["open"]) and (curr_candle["close"] <= prev_candle["open"])

    e9 = ema(close, 9)
    e21 = ema(close, 21)
    e50 = ema(close, 50)
    rsi = calc_rsi(close)
    atr = calc_atr(candles)
    upper_bb, middle_bb, lower_bb = calc_bollinger_bands(close)
    adx = calc_adx(candles)

    e12 = ema(close, 12)
    e26 = ema(close, 26)
    macd_line = [(v12 - v26) if v12 and v26 else 0.0 for v12, v26 in zip(e12, e26)]
    macd_signal = ema(macd_line, 9)
    macd_hist = macd_line[-1] - macd_signal[-1] if macd_signal[-1] is not None else 0.0

    trend5 = market_trend(get_live_data(symbol, "5min"))
    trend15 = market_trend(get_live_data(symbol, "15min"))

    buy_score = 0
    sell_score = 0
    reasons = []

    if adx >= 22:
        reasons.append(f"قوة اتجاه قوية (ADX: {adx:.1f})")
        if trend5 == "صاعد" and trend15 == "صاعد":
            buy_score += 3
            reasons.append("توافق الاتجاه العام صعوداً")
        elif trend5 == "هابط" and trend15 == "هابط":
            sell_score += 3
            reasons.append("توافق الاتجاه العام هبوطاً")
    else:
        reasons.append(f"تذبذب عرضي ضعيف (ADX: {adx:.1f})")

    if last > e21[-1] and e9[-1] > e21[-1]:
        buy_score += 2
        reasons.append("دعم إيجابي من المتوسطات")
    if last < e21[-1] and e9[-1] < e21[-1]:
        sell_score += 2
        reasons.append("دعم سلبي من المتوسطات")

    if last <= lower_bb * 1.002 or has_strong_lower_wick or is_bullish_engulfing:
        if is_valid_body or has_strong_lower_wick or is_bullish_engulfing:
            buy_score += 2
            reasons.append("رصد شمعة انعكاسية صاعدة / رفض من الدعم")

    if last >= upper_bb * 0.998 or has_strong_upper_wick or is_bearish_engulfing:
        if is_valid_body or has_strong_upper_wick or is_bearish_engulfing:
            sell_score += 2
            reasons.append("رصد شمعة انعكاسية هابطة / رفض من المقاومة")

    if macd_hist > 0 and 48 <= rsi <= 68:
        buy_score += 1
        reasons.append("زخم إيجابي مستقر")
    if macd_hist < 0 and 32 <= rsi <= 52:
        sell_score += 1
        reasons.append("زخم سلبي مستقر")

    buy_allowed = buy_score >= 6 and buy_score > sell_score and last > previous and is_valid_body
    sell_allowed = sell_score >= 6 and sell_score > buy_score and last < previous and is_valid_body

    if buy_allowed:
        signal = "🟢 شراء فائق الدقة — مؤكد بالشموع الانعكاسية"
        signal_type = "BUY"
        stop_reference = last - (1.5 * atr)
        target1 = last + (1.8 * atr)
        target2 = last + (2.8 * atr)
    elif sell_allowed:
        signal = "🔴 بيع فائق الدقة — مؤكد بالشموع الانعكاسية"
        signal_type = "SELL"
        stop_reference = last + (1.5 * atr)
        target1 = last - (1.8 * atr)
        target2 = last - (2.8 * atr)
    else:
        signal = "⚪ انتظار — شروط الدقة العالية غير متوفرة"
        signal_type = "WAIT"
        stop_reference = None
        target1 = None
        target2 = None

    now_local = datetime.now(timezone.utc) + timedelta(hours=3)
    duration_min = PERIOD_MINUTES_MAP.get(period, 5)
    candle_start_time = now_local.replace(minute=(now_local.minute // duration_min) * duration_min, second=0, microsecond=0)
    candle_end_time = candle_start_time + timedelta(minutes=duration_min)
    time_rem = max(0, int((candle_end_time - now_local).total_seconds()))
    rem_min, rem_sec = divmod(time_rem, 60)
    ela_sec = (duration_min * 60) - time_rem
    ela_min, ela_sec = divmod(ela_sec, 60)

    def fmt_t(dt):
        return f"{dt.strftime('%I:%M').lstrip('0')} {'ص' if dt.strftime('%p')=='AM' else 'م'}"

    return {
        "price": last, "rsi": rsi, "adx": adx, "ema9": e9[-1], "ema21": e21[-1], "macd": macd_hist, "atr": atr,
        "upper_bb": upper_bb, "lower_bb": lower_bb, "buy_score": buy_score, "sell_score": sell_score,
        "signal": signal, "signal_type": signal_type, "entry_time": fmt_t(candle_start_time),
        "close_time": fmt_t(candle_end_time), "time_remaining": f"{rem_min} د و {rem_sec} ث" if rem_min > 0 else f"{rem_sec} ث",
        "time_elapsed": f"{ela_min} د و {ela_sec} ث" if ela_min > 0 else f"{ela_sec} ث",
        "reasons": reasons, "stop_reference": stop_reference, "target1": target1, "target2": target2,
    }


# =========================================================
# 7. واجهة تيليجرام
# =========================================================

def markets_keyboard():
    rows = []
    for i in range(0, len(MARKETS), 2):
        row = [InlineKeyboardButton(MARKETS[i][0], callback_data="M|" + MARKETS[i][1])]
        if i + 1 < len(MARKETS):
            row.append(InlineKeyboardButton(MARKETS[i + 1][0], callback_data="M|" + MARKETS[i + 1][1]))
        rows.append(row)

    rows.append([InlineKeyboardButton("🔍 رئيسية [1د، 2د، 3د، 4د]", callback_data="SCAN_GRP1_1234")])
    rows.append([InlineKeyboardButton("🔍 سلع ومتقاطعات [1د، 2د، 3د، 4د]", callback_data="SCAN_GRP2_1234")])
    rows.append([InlineKeyboardButton("🔍 رقمية [1د، 2د، 3د، 4د]", callback_data="SCAN_GRP3_1234")])

    rows.append([InlineKeyboardButton("⭐ رئيسية [5د، 15د، 30د، 1ساعة]", callback_data="SCAN_GRP1_BIG")])
    rows.append([InlineKeyboardButton("⭐ سلع ومتقاطعات [5د، 15د، 30د، 1ساعة]", callback_data="SCAN_GRP2_BIG")])
    rows.append([InlineKeyboardButton("⭐ رقمية [5د، 15د، 30د، 1ساعة]", callback_data="SCAN_GRP3_BIG")])

    return InlineKeyboardMarkup(rows)


def period_keyboard(symbol):
    rows = [[InlineKeyboardButton("⏱️ " + label, callback_data=f"P|{symbol}|{period}")] for label, period in PERIODS]
    rows.append([InlineKeyboardButton("⬅️ الأسواق", callback_data="BACK")])
    return InlineKeyboardMarkup(rows)


def again_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحليل سوق آخر", callback_data="BACK")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 **Pro Ultra-Precision Trading Bot**\n\nتم تفعيل فلاتر رصد الشموع الانعكاسية وحركة السعر بدقة عالية لتفادي الإشارات الوهمية.\nاختر السوق:",
        reply_markup=markets_keyboard(),
        parse_mode="Markdown"
    )


async def force_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 **Pro Ultra-Precision Trading Bot**\n\nاختر السوق أو استخدم أزرار الفحص الشامل:",
        reply_markup=markets_keyboard(),
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start - القائمة الرئيسية\n/help - المساعدة")


async def execute_group_scan(query, markets_list, group_name, scan_periods):
    await query.edit_message_text(f"🔍 جاري فحص ({group_name}) بدقة متناهية...")
    found_signals = []
    for name, sym in markets_list:
        for per in scan_periods:
            try:
                res = await asyncio.to_thread(analyze, sym, per)
                if res["signal_type"] in ("BUY", "SELL"):
                    icon = "🟢🟢🟢 شراء" if res["signal_type"] == "BUY" else "🔴🔴🔴 بيع"
                    found_signals.append(
                        f"{icon}\n📊 {name} [{per}]\n"
                        f"⏰ بدء: {res['entry_time']} | ⏳ المتبقي: {res['time_remaining']}\n"
                        f"🛑 وقف: {res['stop_reference']:.5g} | 🎯 هدف: {res['target1']:.5g}"
                    )
                time.sleep(1.0)
            except Exception:
                continue

    msg = f"🟢 **الفرص المؤكدة في ({group_name}):**\n\n" + ("\n─────────────────\n".join(found_signals) if found_signals else "⚪ لا توجد فرص عالية الدقة مطابقة للشروط حالياً.")
    await query.edit_message_text(msg, reply_markup=again_keyboard())


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "BACK":
        await query.edit_message_text("📊 اختر السوق:", reply_markup=markets_keyboard())
        return

    scan_1234 = ["1min", "2min", "3min", "4min"]
    big_p = ["5min", "15min", "30min", "1h"]

    if query.data == "SCAN_GRP1_1234":
        await execute_group_scan(query, [("EUR/USD", "EUR/USD"), ("GBP/USD", "GBP/USD"), ("USD/JPY", "USD/JPY")], "العملات الرئيسية (1، 2، 3، 4 د)", scan_1234)
        return
    if query.data == "SCAN_GRP2_1234":
        await execute_group_scan(query, [("AUD/USD", "AUD/USD"), ("USD/CAD", "USD/CAD"), ("Gold - الذهب", "XAU/USD"), ("Silver - الفضة", "XAG/USD")], "المتقاطعات والسلع (1، 2، 3، 4 د)", scan_1234)
        return
    if query.data == "SCAN_GRP3_1234":
        await execute_group_scan(query, [("Bitcoin", "BTC/USD"), ("Ethereum", "ETH/USD"), ("Solana", "SOL/USD")], "العملات الرقمية (1، 2، 3، 4 د)", scan_1234)
        return

    if query.data == "SCAN_GRP1_BIG":
        await execute_group_scan(query, [("EUR/USD", "EUR/USD"), ("GBP/USD", "GBP/USD"), ("USD/JPY", "USD/JPY")], "العملات الرئيسية (الفريمات الكبرى)", big_p)
        return
    if query.data == "SCAN_GRP2_BIG":
        await execute_group_scan(query, [("AUD/USD", "AUD/USD"), ("USD/CAD", "USD/CAD"), ("Gold - الذهب", "XAU/USD"), ("Silver - الفضة", "XAG/USD")], "المتقاطعات والسلع (الفريمات الكبرى)", big_p)
        return
    if query.data == "SCAN_GRP3_BIG":
        await execute_group_scan(query, [("Bitcoin", "BTC/USD"), ("Ethereum", "ETH/USD"), ("Solana", "SOL/USD")], "العملات الرقمية (الفريمات الكبرى)", big_p)
        return

    if query.data.startswith("M|"):
        symbol = query.data.split("|", 1)[1]
        await query.edit_message_text(f"السوق: {symbol}\nاختر فريم التحليل:", reply_markup=period_keyboard(symbol))
        return

    if not query.data.startswith("P|"):
        return

    _, symbol, period = query.data.split("|")
    labels = dict((v, l) for l, v in PERIODS)
    await query.edit_message_text("⏳ جارٍ رصد الشموع الانعكاسية وفلترة الإشارات...")

    try:
        res = await asyncio.to_thread(analyze, symbol, period)
        reasons_txt = "\n".join("• " + r for r in res["reasons"])
        stop_txt = f"{res['stop_reference']:.5g}" if res["stop_reference"] else "انتظار"
        t1_txt = f"{res['target1']:.5g}" if res["target1"] else "انتظار"
        t2_txt = f"{res['target2']:.5g}" if res["target2"] else "انتظار"

        header = ""
        if res["signal_type"] in ("BUY", "SELL"):
            header = (
                f"{'🟢🟢🟢 شراء ⬆️' if res['signal_type']=='BUY' else '🔴🔴🔴 بيع ⬇️'}\n"
                f"⏰ بدء: {res['entry_time']} | ⌛ إغلاق: {res['close_time']}\n"
                f"⏳ المتبقي: {res['time_remaining']} | ⏱️ المنقضي: {res['time_elapsed']}\n"
                f"─────────────────\n"
            )

        message = (
            f"{header}"
            f"📊 السوق: {symbol} | الفريم: {labels.get(period, period)}\n\n"
            f"💵 سعر الإغلاق: {res['price']:.5g}\n"
            f"🎯 التوصية: {res['signal']}\n\n"
            f"📈 قوة الاتجاه ADX: {res['adx']:.1f}\n"
            f"📊 RSI: {res['rsi']:.1f} | ATR: {res['atr']:.5g}\n"
            f"📉 بولنجر (عالي: {res['upper_bb']:.5g} | سفلي: {res['lower_bb']:.5g})\n\n"
            f"🛑 وقف الخسارة: {stop_txt}\n"
            f"🎯 الهدف الأول: {t1_txt}\n"
            f"🎯 الهدف الثاني: {t2_txt}\n\n"
            f"🔎 الفلاتر الفنية والشموع الانعكاسية:\n{reasons_txt}\n\n"
            "⚠️ التوصيات تحليلية تقنية وليست نصيحة مالية."
        )

        await query.edit_message_text(message, reply_markup=again_keyboard())

    except Exception as e:
        logger.exception("Analysis error")
        await query.edit_message_text(f"❌ تعذر إكمال التحليل: {str(e)[:400]}", reply_markup=again_keyboard())


# =========================================================
# 8. التشغيل
# =========================================================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, force_start_handler))

    logger.info("Pro Ultra-Precision Trading Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
