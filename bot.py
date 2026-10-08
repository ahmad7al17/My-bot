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
        self.wfile.write(b"Trading Bot is running successfully!")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

BOT_TOKEN = "8819592873:AAE1qr_QTklOvGkpEA3yXKnlNJMtdzPhiXs"

MARKETS = [
    ("🇪🇺 EUR/USD", "EURUSD=X"),
    ("🇬🇧 GBP/USD", "GBPUSD=X"),
    ("🇯🇵 USD/JPY", "USDJPY=X"),
    ("🇦🇺 AUD/USD", "AUDUSD=X"),
    ("🪙 Gold", "GC=F"),
    ("⚪ Silver", "SI=F"),
    ("₿ Bitcoin", "BTC-USD"),
    ("💎 Ethereum", "ETH-USD"),
    ("🍎 Apple", "AAPL"),
    ("🚗 Tesla", "TSLA"),
    ("📦 Amazon", "AMZN"),
    ("💻 Microsoft", "MSFT"),
    ("🔎 Google", "GOOGL"),
    ("👥 Meta", "META"),
]

PERIODS = [
    ("1 دقيقة", "1m"),
    ("5 دقائق", "5m"),
    ("15 دقيقة", "15m"),
    ("1 ساعة", "1h"),
    ("1 يوم", "1d"),
]

def get_data(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    params = {'range': '5d', 'interval': '1m'}
    
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    try:
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        closes = quote['close']
        
        candles = []
        for t, c in zip(timestamps, closes):
            if c is not None:
                candles.append({"close": float(c)})
        
        if len(candles) < 55:
            raise ValueError("الشموع المتاحة غير كافية")
            
        return candles
    except Exception as e:
        raise ValueError("تعذر جلب بيانات السوق المتاحة")

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
    close = [c["close"] for c in candles]
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
        display_name = next((m[0] for m in MARKETS if m[1] == symbol), symbol)
        await q.edit_message_text(
            f"السوق: {display_name}\nاختر أفق التحليل:",
            reply_markup=period_keyboard(symbol)
        )
        return

    if q.data.startswith("P|"):
        _, symbol, period = q.data.split("|")
        labels = dict((v, k) for k, v in PERIODS)
        display_name = next((m[0] for m in MARKETS if m[1] == symbol), symbol)
        await q.edit_message_text("⏳ جارٍ تحليل بيانات السوق عبر المؤشرات الفنية...")

        try:
            a = analyze(symbol)
            reasons = "\n".join(
                "• " + html.escape(x) for x in a["reasons"]
            )

            text = (
                f"📊 السوق: {html.escape(display_name)}\n"
                f"⏱️ أفق التحليل: {labels[period]}\n\n"
                f"💵 آخر إغلاق: {a['price']:.4g}\n"
                f"🎯 الإشارة: {a['signal']}\n\n"
                f"📈 EMA9: {a['ema9']:.4g}\n"
                f"📈 EMA21: {a['ema21']:.4g}\n"
                f"📈 EMA50: {a['ema50']:.4g}\n"
                f"📊 RSI14: {a['rsi']:.2f}\n"
                f"〽️ MACD: {a['macd']:.4g}\n\n"
                f"🔎 أسباب التحليل:\n{reasons}\n\n"
                "⚠️ البيانات تعتمد على تحليل مؤشرات الزخم والشموع الفنية."
            )

            await q.edit_message_text(
                text,
                reply_markup=again_keyboard()
            )

        except Exception as e:
            await q.edit_message_text(
                "❌ تعذر التحليل أو جلب البيانات حالياً.",
                reply_markup=again_keyboard()
            )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start لعرض الأسواق\n"
        "/help للمساعدة\n\n"
        "البوت يعتمد على التحليل الفني ولا ينفذ صفقات مالية."
    )

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
    
    print("Trading Bot is running successfully with Yahoo Finance source...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
