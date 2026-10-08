import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import random
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# خادم الويب الوهمي لترضية ريندر
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

TOKEN = "8877036116:AAH9zil46fP5C-z9jHSvpGHXwYrtqAPKtY4"

MARKETS = [
    ("EUR/USD (OTC)", "🇪🇺🇺🇸"),
    ("Smarty", "🧠"),
    ("Gold", "🪙"),
    ("Silver", "⚪"),
    ("Bitcoin/USD", "⚡"),
    ("Ethereum/USD", "💎"),
    ("Apple", "🍎"),
    ("Tesla", "🚗"),
    ("Brent Oil", "🛢")
]

TIMINGS = ["30 ثانية", "1 دقيقة", "3 دقائق", "5 دقائق"]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for market, emoji in MARKETS:
        keyboard.append([InlineKeyboardButton(f"{market} {emoji}", callback_data=f"market_{market}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📊 <b>اختر السوق المراد تحليله:</b>",
        parse_mode="HTML",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("market_"):
        market_name = data.replace("market_", "")
        time_frame = random.choice(TIMINGS)
        signal_type = random.choice(["🟢 Buy / شراء", "🔴 Sell / بيع"])
        
        report = (
            f"📊 <b>السوق :</b> {market_name}\n\n"
            f"⏱ <b>الوقت :</b> {time_frame}\n\n"
            f"⚖ <b>الصفقة :</b> {signal_type}\n\n"
            f"🚨 <b>- تحذير مهم :</b> يجب دخول الصفقة فوراً\n\n"
            f"⚠️ <b>- لا تدخل الصفقة إذا مر أكثر من 10 ثواني على الرسالة</b>\n\n"
            f"🔗 <b>القناة الرسمية :</b> https://t.me/Fahad1485"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔄 خذ صفقة أخرى", callback_data="back_to_markets")]
        ]
        await query.edit_message_text(text=report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "back_to_markets":
        keyboard = []
        for market, emoji in MARKETS:
            keyboard.append([InlineKeyboardButton(f"{market} {emoji}", callback_data=f"market_{market}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📊 <b>اختر السوق المراد تحليله:</b>",
            parse_mode="HTML",
            reply_markup=reply_markup
        )

if __name__ == '__main__':
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Trading Bot is running with polling...")
    application.run_polling(drop_pending_updates=True)
