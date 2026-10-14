import os
import html
import requests
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)

# خادم ويب وهمي لترضية منصة Render
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Advanced Trading Bot is running successfully!")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# توكن البوت الخاص بك
BOT_TOKEN = "8819592873:AAE1qr_QTklOvGkpEA3yXKnlNJMtdzPhiXs"

# مفتاح Twelve Data API الخاص بك
TWELVE_DATA_API_KEY = "11ecbf4bff0a4629ba9908c436325c77"

# قائمة الأسواق المتوافقة مع رموز Twelve Data الحقيقية
MARKETS = [
    # --- العملات الأجنبية (Forex) ---
    ("🇪🇺/🇺🇸 EUR/USD", "EUR/USD"),
    ("🇬🇧/🇺🇸 GBP/USD", "GBP/USD"),
    ("🇺🇸/🇯🇵 USD/JPY", "USD/JPY"),
    ("🇦🇺/🇺🇸 AUD/USD", "AUD/USD"),
    ("🇺🇸/🇨🇦 USD/CAD", "USD/CAD"),
    ("🇺🇸/🇨🇭 USD/CHF", "USD/CHF"),
    ("🇳🇿/🇺🇸 NZD/USD", "NZD/USD"),
    ("🇪🇺/🇬🇧 EUR/GBP", "EUR/GBP"),
    ("🇪🇺/🇯🇵 EUR/JPY", "EUR/JPY"),
    ("🇬🇧/🇯🇵 GBP/JPY", "GBP/JPY"),

    # --- السلع والمعادن (Commodities) ---
    ("🥇 Gold (الذهب)", "XAU/USD"),
    ("🥈 Silver (الفضة)", "XAG/USD"),
    ("🛢️ Crude Oil (النفط)", "BRENT"),
    
    # --- العملات الرقمية (Cryptos) ---
    ("₿ Bitcoin (BTC)", "BTC/USD"),
    ("💎 Ethereum (ETH)", "ETH/USD"),
    ("🟣 Solana (SOL)", "SOL/USD"),
]

PERIODS = [
    ("5 دقائق", "5min"),
    ("15 دقيقة", "15min"),
    ("30 دقيقة", "30min"),
    ("1 ساعة", "1h"),
    ("1 يوم", "1day"),
]

def get_live_data(symbol, interval):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 60,
        "apikey": TWELVE_DATA_API_KEY
    }
    
    r = requests.get(url, params=params, timeout=20)
    data = r.json()

    if "code" in data and data["code"] != 200:
        raise ValueError(data.get("message", "خطأ في جلب البيانات"))

    values = data.get("values", [])
    if not values:
        raise ValueError("البيانات غير متاحة حالياً")
    
    # ترتيب الشموع من القديم إلى الجديد
    candles = [{"close": float(v["close"])} for v in reversed(values)]
    return candles

def ema(values, period):
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result

def sma(values, period):
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(sum(values[:i+1]) / (i + 1))
        else:
            result.append(sum(values[i-period+1:i+1]) / period)
    return result

def calc_rsi(values, period=14):
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        gain = (gain * (period - 1) + gains[i]) / period
        loss = (loss * (period - 1) + losses[i]) / period

    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)

def calc_bollinger_bands(values, period=20, num_std=2):
    middle = sma(values, period)
    std = []
    for i in range(len(values)):
        if i < period - 1:
            chunk = values[:i+1]
        else:
            chunk = values[i-period+1:i+1]
        mean = sum(chunk) / len(chunk)
        variance = sum((x - mean) ** 2 for x in chunk) / len(chunk)
        std.append(variance ** 0.5)
    
    upper = [m + (num_std * s) for m, s in zip(middle, std)]
    lower = [m - (num_std * s) for m, s in zip(middle, std)]
    return upper, middle, lower

def analyze(symbol, period):
    candles = get_live_data(symbol, period)
    close = [c["close"] for c in candles]
    last = close[-1]
    prev_close = close[-2]

    e9 = ema(close, 9)
    e21 = ema(close, 21)
    e50 = ema(close, 50)
    e12 = ema(close, 12)
    e26 = ema(close, 26)

    macd = [a - b for a, b in zip(e12, e26)]
    macd_signal = ema(macd, 9)
    rsi = calc_rsi(close)
    upper_band, middle_band, lower_band = calc_bollinger_bands(close)

    buy = 0
    sell = 0
    reasons = []

    is_falling_fast = last < prev_close and e9[-1] < e21[-1]
    is_rising_fast = last > prev_close and e9[-1] > e21[-1]

    trend_is_bullish = e9[-1] > e21[-1] and last > e50[-1]
    trend_is_bearish = e9[-1] < e21[-1] and last < e50[-1]

    if trend_is_bullish:
        buy += 2
        reasons.append("الاتجاه العام صاعد بدقة عالية")
    elif trend_is_bearish:
        sell += 2
        reasons.append("الاتجاه العام هابط بدقة عالية")

    if 45 < rsi < 70 and not is_falling_fast:
        buy += 1
        reasons.append(f"مؤشر RSI إيجابي ({rsi:.1f})")
    elif 30 < rsi < 55 and not is_rising_fast:
        sell += 1
        reasons.append(f"مؤشر RSI سلبي ({rsi:.1f})")

    if macd[-1] > macd_signal[-1] and not is_falling_fast:
        buy += 1
        reasons.append("زخم MACD يدعم الصعود")
    elif macd[-1] < macd_signal[-1] and not is_rising_fast:
        sell += 1
        reasons.append("زخم MACD يدعم الهبوط")

    if last <= lower_band[-1] and not is_falling_fast:
        buy += 2
        reasons.append("ارتداد مدعوم من بولينجر السفلي")
    elif last >= upper_band[-1] and not is_rising_fast:
        sell += 2
        reasons.append("ارتداد مدعوم من بولينجر العلوي")

    if is_falling_fast:
        buy = 0
        reasons = ["تم حظر الشراء: السوق يمر بهبوط حاد متسارع"]
    
    if is_rising_fast:
        sell = 0
        reasons = ["تم حظر البيع: السوق يمر بارتفاع حاد متسارع"]

    if buy >= 3 and buy > sell and not is_falling_fast:
        signal = "🟢 STRONG BUY / شراء مؤكد"
    elif sell >= 3 and sell > buy and not is_rising_fast:
        signal = "🔴 STRONG SELL / بيع مؤكد"
    else:
        signal = "⚪ WAIT / انتظار (حماية كاملة)"
        reasons = ["السوق غير مستقر، تم حجب الإشارة لمنع الخسارة"]

    return {
        "price": last,
        "rsi": rsi,
        "ema9": e9[-1],
        "ema21": e21[-1],
        "ema50": e50[-1],
        "macd": macd[-1] - macd_signal[-1],
        "signal": signal,
        "reasons": reasons,
    }

def markets_keyboard():
    rows = []
    for i in range(0, len(MARKETS), 2):
        row = [InlineKeyboardButton(MARKETS[i][0], callback_data="M|" + MARKETS[i][1])]
        if i + 1 < len(MARKETS):
            row.append(InlineKeyboardButton(MARKETS[i+1][0], callback_data="M|" + MARKETS[i+1][1]))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def period_keyboard(symbol):
    rows = [[InlineKeyboardButton("⏱️ " + label, callback_data=f"P|{symbol}|{period}")] for label, period in PERIODS]
    rows.append([InlineKeyboardButton("⬅️ الأسواق", callback_data="BACK")])
    return InlineKeyboardMarkup(rows)

def again_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحليل سوق آخر", callback_data="BACK")]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 بوت التحليل المتقدم (مدعوم ببيانات Twelve Data الحية)\n\nاختر السوق:",
        reply_markup=markets_keyboard()
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "BACK":
        await q.edit_message_text("📊 اختر السوق:", reply_markup=markets_keyboard())
        return

    if q.data.startswith("M|"):
        symbol = q.data.split("|", 1)[1]
        display_name = next((m[0] for m in MARKETS if m[1] == symbol), symbol)
        await q.edit_message_text(f"السوق: {display_name}\nاختر أفق التحليل:", reply_markup=period_keyboard(symbol))
        return

    if q.data.startswith("P|"):
        _, symbol, period = q.data.split("|")
        labels = dict((v, k) for k, v in PERIODS)
        display_name = next((m[0] for m in MARKETS if m[1] == symbol), symbol)
        await q.edit_message_text("⏳ جارٍ جلب البيانات الحية والتحليل المتقدم...")

        try:
            a = analyze(symbol, period)
            reasons = "\n".join("• " + html.escape(x) for x in a["reasons"])

            text = (
                f"📊 السوق: {html.escape(display_name)}\n"
                f"⏱️ أفق التحليل: {labels.get(period, period)}\n\n"
                f"💵 السعر اللحظي: {a['price']:.4g}\n"
                f"🎯 التوصية: {a['signal']}\n\n"
                f"📈 EMA9: {a['ema9']:.4g}\n"
                f"📈 EMA21: {a['ema21']:.4g}\n"
                f"📈 EMA50: {a['ema50']:.4g}\n"
                f"📊 RSI14: {a['rsi']:.2f}\n"
                f"〽️ MACD: {a['macd']:.4g}\n\n"
                f"🔎 أسباب التوصية:\n{reasons}\n\n"
                "🛡️ بيانات حقيقية ومحمية."
            )

            await q.edit_message_text(text, reply_markup=again_keyboard())

        except Exception as e:
            await q.edit_message_text(f"❌ خطأ في جلب البيانات: {str(e)}", reply_markup=again_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start لعرض الأسواق\n/help للمساعدة")

def main():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(buttons))
    
    print("Advanced Twelve Data Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
