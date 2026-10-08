import os
import html
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# خادم ويب وهمي لترضية منصة Render
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Trading Bot is running successfully!")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

BOT_TOKEN = "8819592873:AAE1qr_QTklOvGkpEA3yXKnlNJMtdzPhiXs"
DATA_KEY = "demo"
API = "https://api.twelvedata.com"

MARKETS = [
    ("🇪🇺 EUR/USD", "EUR/USD"),
    ("🇬🇧 GBP/USD", "GBP/USD"),
    ("🇯🇵 USD/JPY", "USD/JPY"),
    ("🇦🇺 AUD/USD", "AUD/USD"),
    ("🪙 Gold", "XAU/USD"),
    ("⚪ Silver", "XAG/USD"),
    ("₿ Bitcoin", "BTC/USD"),
    ("💎 Ethereum", "ETH/USD"),
    ("🍎 Apple", "AAPL"),
    ("🚗 Tesla", "TSLA"),
    ("📦 Amazon", "AMZN"),
    ("💻 Microsoft", "MSFT"),
    ("🔎 Google", "GOOGL"),
    ("👥 Meta", "META"),
]

PERIODS = [
    ("30 ثانية", "30s"),
    ("45 ثانية", "45s"),
    ("1 دقيقة", "1m"),
    ("2 دقيقتان", "2m"),
    ("3 دقائق", "3m"),
]

def get_data(symbol):
    r = requests.get(
        API + "/time_series",
        params={
            "symbol": symbol,
            "interval": "1min",
            "outputsize": 100,
            "apikey": DATA_KEY,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("status") == "error" or "values" not in data:
        raise ValueError(data.get("message", "تعذر جلب البيانات"))

    candles = list(reversed(data["values"]))
    if len(candles) < 55:
        raise ValueError("الشموع المتاحة غير كافية")

    return candles

def ema(values, period):
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
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

def analyze(symbol):
    candles = get_data(symbol)
    close = [float(c["close"]) for c in candles]
    last = close[-1]

    e9 = ema(close, 9)
    e21 = ema(close, 21)
    e12 = ema(close, 12)
    e26 = ema(close, 26)

    macd = [a - b for a, b in zip(e12, e26)]
    macd_signal = ema(macd, 9)
    rsi = calc_rsi(close)

    buy = 0
    sell = 0
    reasons = []

    if e9[-1] > e21[-1]:
        buy += 1
        reasons.append("EMA9 أعلى من EMA21")
    elif e9[-1] < e21[-1]:
        sell += 1
        reasons.append("EMA9 أسفل من EMA21")

    if last > ema(close, 50)[-1]:
        buy += 1
        reasons.append("السعر فوق EMA50")
    else:
        sell += 1
        reasons.append("السعر تحت EMA50")

    if macd[-1] > macd_signal[-1]:
        buy += 1
        reasons.append("MACD إيجابي")
    else:
        sell += 1
        reasons.append("MACD سلبي")

    if 52 <= rsi < 68:
        buy += 1
        reasons.append("RSI يدعم الزخم الصاعد")
    elif 32 < rsi <= 48:
        sell += 1
        reasons.append("RSI يدعم الزخم الهابط")

    if buy >= 3 and buy > sell:
        signal = "🟢 BUY / شراء"
    elif sell >= 3 and sell > buy:
        signal = "🔴 SELL / بيع"
    else:
        signal = "⚪ WAIT / انتظار"
        reasons = ["المؤشرات غير متوافقة بما يكفي"]

    return {
        "price": last,
        "rsi": rsi,
        "ema9": e9[-1],
        "ema21": e21[-1],
        "ema50": ema(close, 50)[-1],
        "macd": macd[-1] - macd_signal[-1],
        "signal": signal,
        "reasons": reasons,
        "time": candles[-1]["datetime"],
    }

def markets_keyboard():
    rows = []
    for i in range(0, len(MARKETS), 2):
        row = [
            InlineKeyboardButton(
                MARKETS[i][0],
                callback_data="M|" + MARKETS[i][1]
            )
        ]
        if i + 1 < len(MARKETS):
            row.append(
                InlineKeyboardButton(
                    MARKETS[i+1][0],
                    callback_data="M|" + MARKETS[i+1][1]
                )
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def period_keyboard(symbol):
    rows = [
        [InlineKeyboardButton(
            "⏱️ " + label,
            callback_data=f"P|{symbol}|{period}"
        )]
        for label, period in PERIODS
    ]
    rows.append([
        InlineKeyboardButton("⬅️ الأسواق", callback_data="BACK")
    ])
    return InlineKeyboardMarkup(rows)

def again_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 تحليل سوق آخر", callback_data="BACK")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 بوت تحليل الأسواق الفني المتقدم\n\n"
        "اختر السوق الذي تريد تحليله:",
        reply_markup=markets_keyboard()
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "BACK":
        await q.edit_message_text(
            "📊 اختر السوق:",
            reply_markup=markets_keyboard()
        )
        return

    if q.data.startswith("M|"):
        symbol = q.data.split("|", 1)[1]
        await q.edit_message_text(
            f"السوق: {symbol}\nاختر أفق التحليل:",
            reply_markup=period_keyboard(symbol)
        )
        return

    if q.data.startswith("P|"):
        _, symbol, period = q.data.split("|")
        labels = dict((v, k) for k, v in PERIODS)
        await q.edit_message_text("⏳ جارٍ تحليل بيانات السوق عبر المؤشرات الفنية...")

        try:
            a = analyze(symbol)
            reasons = "\n".join(
                "• " + html.escape(x) for x in a["reasons"]
            )

            text = (
                f"📊 السوق: {html.escape(symbol)}\n"
                f"⏱️ أفق التحليل: {labels[period]}\n"
                f"🕐 آخر شمعة: {html.escape(a['time'])}\n\n"
                f"💵 آخر إغلاق: {a['price']:.8g}\n"
                f"🎯 الإشارة: {a['signal']}\n\n"
                f"📈 EMA9: {a['ema9']:.8g}\n"
                f"📈 EMA21: {a['ema21']:.8g}\n"
                f"📈 EMA50: {a['ema50']:.8g}\n"
                f"📊 RSI14: {a['rsi']:.2f}\n"
                f"〽️ MACD: {a['macd']:.8g}\n\n"
                f"🔎 أسباب التحليل:\n{reasons}\n\n"
                "⚠️ البيانات تعتمد على تحليل مؤشرات الزخم والشموع الفنية."
            )

            await q.edit_message_text(
                text,
                reply_markup=again_keyboard()
            )

        except Exception as e:
            await q.edit_message_text(
                "❌ تعذر التحليل:\n"
                + html.escape(str(e)),
                reply_markup=again_keyboard()
            )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start لعرض الأسواق\n"
        "/help للمساعدة\n\n"
        "البوت يعتمد على التحليل الفني ولا ينفذ صفقات مالية."
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(buttons))
    print("Trading Bot is running perfectly...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
