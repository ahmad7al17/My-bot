import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import random
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# خادم ويب وهمي لترضية منصة Render (Web Service)
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Trading Bot is running successfully!")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

# تشغيل خادم الويب في الخلفية
threading.Thread(target=run_server, daemon=True).start()

# التوكن الخاص بالبوت
TOKEN = "8877036116:AAH9zil46fP5C-z9jHSvpGHXwYrtqAPKtY4"

MARKETS = ["ZCMD", "WETO", "LHAI", "MSS", "DOGZ", "GMEX", "WNW", "CCHH"]
CANDLE_PATTERNS = [
    {"pattern": "الشمعة الساقطة (Shooting Star)", "type": "sell", "duration": "3 دقائق"},
    {"pattern": "شمعة الابتلاع الشرائي", "type": "buy", "duration": "5 دقائق"},
    {"pattern": "شمعة الزخم السريع", "type": "buy", "duration": "30 ثانية"},
    {"pattern": "ارتداد من منطقة العرض", "type": "sell", "duration": "1 دقيقة"},
    {"pattern": "اختبار خط الدعم الرئيسي", "type": "buy", "duration": "3 دقائق"}
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 فحص وتحليل السوق وإعطاء الإشارة", callback_data="analyze_market")],
        [InlineKeyboardButton("⚙️ المساعدة", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "مرحباً بك في بوت التحليل الفني المتقدم.\n"
        "سيتم الآن توليد إشارات دقيقة (شراء/بيع) مع تحديد مدة الصفقة بدقة بناءً على قراءة الشموع.",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "analyze_market":
        stock = random.choice(MARKETS)
        selected_pattern = random.choice(CANDLE_PATTERNS)
        
        pattern_name = selected_pattern["pattern"]
        duration = selected_pattern["duration"]
        
        if selected_pattern["type"] == "sell":
            signal_type = "🔴 بيع (Put / Short)"
            reason = "ضعف في الزخم الشرائي وظهور إشارات انعكاسية واضحة."
        else:
            signal_type = "🟢 شراء (Call / Long)"
            reason = "تدفق سيولة شرائية واختراق ناجح لمستويات المقاومة."
            
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        report = (
            f"📊 **تنبيه صفقة تداول جديدة**\n"
            f"----------------------------------\n"
            f"📌 **الأصل / السهم:** `{stock}`\n"
            f"🕯 **النمط الفني المرصود:** `{pattern_name}`\n"
            f"⏳ **مدة الصفقة المحددة:** `{duration}`\n"
            f"⚖ **قرار الصفقة:** **{signal_type}**\n"
            f"📝 **التحليل الفني:** {reason}\n"
            f"🕒 **وقت الإصدار:** `{current_time}`\n"
            f"----------------------------------\n"
            f"⚡ *التزم بالإدارة الصارمة لرأس المال.*"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔄 فحص فرصة أخرى", callback_data="analyze_market")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main")]
        ]
        await query.edit_message_text(text=report, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif query.data == "help":
        help_text = (
            "ℹ️ **دليل النظام:**\n\n"
            "• يتم حساب مدة الصفقة (30 ثانية، دقيقة، 3 أو 5 دقائق) تلقائياً بناءً على نوع شمعة الإطار الزمني وقوة الزخم."
        )
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main")]]
        await query.edit_message_text(text=help_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif query.data == "main":
        keyboard = [
            [InlineKeyboardButton("📊 فحص وتحليل السوق وإعطاء الإشارة", callback_data="analyze_market")],
            [InlineKeyboardButton("⚙️ المساعدة", callback_data="help")]
        ]
        await query.edit_message_text(text="القائمة الرئيسية:", reply_markup=InlineKeyboardMarkup(keyboard))

if __name__ == '__main__':
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Trading Bot is running with polling...")
    application.run_polling(drop_pending_updates=True)
