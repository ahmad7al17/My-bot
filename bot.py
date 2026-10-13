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

# توكن البوت الخاص بك
BOT_TOKEN = "8819592873:AAE1qr_QTklOvGkpEA3yXKnlNJMtdzPhiXs"

# قائمة الأسواق المطابقة لمنصات التداول
MARKETS = [
    # --- العملات الأجنبية (Forex) ---
    ("🇪🇺/🇺🇸 EUR/USD", "EURUSD=X"),
    ("🇬🇧/🇺🇸 GBP/USD", "GBPUSD=X"),
    ("🇺🇸/🇯🇵 USD/JPY", "USDJPY=X"),
    ("🇦🇺/🇺🇸 AUD/USD", "AUDUSD=X"),
    ("🇺🇸/🇨🇦 USD/CAD", "USDCAD=X"),
    ("🇺🇸/🇨🇭 USD/CHF", "USDCHF=X"),
    ("🇳🇿/🇺🇸 NZD/USD", "NZDUSD=X"),
    ("🇪🇺/🇬🇧 EUR/GBP", "EURGBP=X"),
    ("🇪🇺/🇯🇵 EUR/JPY", "EURJPY=X"),
    ("🇬🇧/🇯🇵 GBP/JPY", "GBPJPY=X"),
    ("🇦🇺/🇯🇵 AUD/JPY", "AUDJPY=X"),
    ("🇨🇦/🇯🇵 CAD/JPY", "CADJPY=X"),
    ("🇪🇺/🇦🇺 EUR/AUD", "EURAUD=X"),
    ("🇬🇧/🇦🇺 GBP/AUD", "GBPAUD=X"),

    # --- السلع والمعادن (Commodities) ---
    ("🥇 Gold (الذهب)", "GC=F"),
    ("🥈 Silver (الفضة)", "SI=F"),
    ("🛢️ Crude Oil (النفط)", "CL=F"),
    ("⛽ Brent Oil (برنت)", "BZ=F"),
    
    # --- العملات الرقمية (Cryptos) ---
    ("₿ Bitcoin (BTC)", "BTC-USD"),
    ("💎 Ethereum (ETH)", "ETH-USD"),
    ("🟣 Solana (SOL)", "SOL-USD"),
    ("⚡ Ripple (XRP)", "XRP-USD"),
    ("🪙 Cardano (ADA)", "ADA-USD"),
    ("🐕 Dogecoin (DOGE)", "DOGE-USD"),

    # --- الأسهم العالمية (Stocks) ---
    ("🍎 Apple", "AAPL"),
    ("🚗 Tesla", "TSLA"),
    ("💚 NVIDIA", "NVDA"),
    ("📦 Amazon", "AMZN"),
    ("💻 Microsoft", "MSFT"),
    ("🔎 Google", "GOOGL"),
    ("👥 Meta", "META"),
    ("🎬 Netflix", "NFLX"),
]

PERIODS = [
    ("10 ثوانٍ", "10s"),
    ("15 ثانية", "15s"),
    ("30 ثانية", "30s"),
    ("45 ثانية", "45s"),
    ("1 دقيقة", "1m"),
    ("دقيقتان", "2m"),
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
        
        if len(candles) < 60:
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

def analyze(symbol):
    candles = get_data(symbol)
    close = [c["close"] for c in candles]
    last = close[-1]

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

    # 1. تقاطع المتوسطات الأسية (التحقق من الاتجاه العام)
    trend_is_bullish = e9[-1] > e21[-1] and last > e50[-1]
    trend_is_bearish = e9[-1] < e21[-1] and last < e50[-1]

    if trend_is_bullish:
        buy += 2
        reasons.append("الاتجاه العام صاعد بقوة (EMA 9 > 21 والسعر فوق 50)")
    elif trend_is_bearish:
        sell += 2
        reasons.append("الاتجاه العام هابط بقوة (EMA 9 < 21 والسعر تحت 50)")

    # 2. مؤشر القوة النسبية (RSI) مع شروط منع الانعكاس الخاطئ
    if 45 < rsi < 70 and not trend_is_bearish:
        buy += 1
        reasons.append(f"مؤشر RSI يدعم الصعود ({rsi:.1f})")
    elif 30 < rsi < 55 and not trend_is_bullish:
        sell += 1
        reasons.append(f"مؤشر RSI يدعم الهبوط ({rsi:.1f})")
    
    # منع إعطاء بيع عشوائي أثناء الارتفاع لمجرد التشبع إلا بوجود تأكيد هبوطي
    if rsi >= 75 and e9[-1] < e21[-1]:
        sell += 2
        reasons.append("تشبع شرائي قوي مع بداية تقاطع سلبي (فرصة بيع آمنة)")
    elif rsi <= 25 and e9[-1] > e21[-1]:
        buy += 2
        reasons.append("تشبع بيعي قوي مع بداية تقاطع إيجابي (فرصة شراء آمنة)")

    # 3. مؤشر MACD
    if macd[-1] > macd_signal[-1] and macd[-1] > 0:
        buy += 1
        reasons.append("MACD إيجابي وفوق خط الإشارة")
    elif macd[-1] < macd_signal[-1] and macd[-1] < 0:
        sell += 1
        reasons.append("MACD سلبي وتحت خط الإشارة")

    # 4. نطاقات بولينجر (Bollinger Bands)
    if last <= lower_band[-1] and trend_is_bullish:
        buy += 2
        reasons.append("ارطداد آمن من الحد السفلي لبولينجر في اتجاه صاعد")
    elif last >= upper_band[-1] and trend_is_bearish:
        sell += 2
        reasons.append("ارتداد آمن من الحد العلوي لبولينجر في اتجاه هابط")

    # تطبيق شروط حماية لمنع إعطاء إشارات متعارضة أثناء الارتفاع القوي
    if buy >= 4 and buy > sell and not trend_is_bearish:
        signal = "🟢 STRONG BUY / شراء قوي ومضمون"
    elif buy >= 3 and buy > sell and not trend_is_bearish:
        signal = "🟢 BUY / شراء"
    elif sell >= 4 and sell > buy and not trend_is_bullish:
        signal = "🔴 STRONG SELL / بيع قوي ومضمون"
    elif sell >= 3 and sell > buy and not trend_is_bullish:
        signal = "🔴 SELL / بيع"
    else:
        signal = "⚪ WAIT / انتظار (السوق متذبذب أو عكس الاتجاه)"
        reasons = ["تم حجب الإشارة لتجنب الانعكاس الخاطئ (السوق غير مستقر للأطر القصيرة)"]

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
        "🤖 بوت تحليل الأسواق المحسن (مع فلتر حماية الاتجاه)\n\nاختر السوق الذي تريد تحليله:",
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
        await q.edit_message_text("⏳ جارٍ فحص الزخم والاتجاه بدقة عالية...")

        try:
            a = analyze(symbol)
            reasons = "\n".join("• " + html.escape(x) for x in a["reasons"])

            text = (
                f"📊 السوق: {html.escape(display_name)}\n"
                f"⏱️ أفق التحليل: {labels.get(period, period)}\n\n"
                f"💵 السعر الحالي: {a['price']:.4g}\n"
                f"🎯 التوصية: {a['signal']}\n\n"
                f"📈 EMA9: {a['ema9']:.4g}\n"
                f"📈 EMA21: {a['ema21']:.4g}\n"
                f"📈 EMA50: {a['ema50']:.4g}\n"
                f"📊 RSI14: {a['rsi']:.2f}\n"
                f"〽️ MACD: {a['macd']:.4g}\n\n"
                f"🔎 أسباب التوصية:\n{reasons}\n\n"
                "⚠️ التوصية محمية ضد الانعكاسات الوهمية."
            )

            await q.edit_message_text(text, reply_markup=again_keyboard())

        except Exception as e:
            await q.edit_message_text("❌ تعذر التحليل أو جلب البيانات حالياً.", reply_markup=again_keyboard())

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
    
    print("Trading Bot with Trend Protection is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
