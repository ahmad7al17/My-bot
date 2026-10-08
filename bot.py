import telebot
from telebot import types

TOKEN = '8877036116:AAH9zil46fP5C-z9jHSvpGHXwYrtqAPKtY4'
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    
    btn1 = types.KeyboardButton('Football Index ⚽')
    btn2 = types.KeyboardButton('Smarty 🧠')
    btn3 = types.KeyboardButton('USD/NGN (OTC) 🇳🇬')
    btn4 = types.KeyboardButton('EUR/USD (OTC) 💶')
    btn5 = types.KeyboardButton('GBP/USD (OTC) 🇬🇧')
    btn6 = types.KeyboardButton('USD/JPY (OTC) 🇯🇵')
    btn7 = types.KeyboardButton('Gold 🪙')
    btn8 = types.KeyboardButton('Silver ⚪')
    btn9 = types.KeyboardButton('Bitcoin/USD ⚡')
    btn10 = types.KeyboardButton('Ethereum/USD 💎')
    btn11 = types.KeyboardButton('Apple 🍏')
    btn12 = types.KeyboardButton('Google 🔍 (Alphabet)')
    btn13 = types.KeyboardButton('Facebook 👤')
    btn14 = types.KeyboardButton('Amazon 📦')
    btn15 = types.KeyboardButton('Tesla 🚗')
    btn16 = types.KeyboardButton('Brent Oil 🛢️')
    btn17 = types.KeyboardButton('AUD/CAD 🇦🇺')
    
    markup.add(btn1, btn2, btn3, btn4, btn5, btn6, btn7, btn8, btn9, btn10, btn11, btn12, btn13, btn14, btn15, btn16, btn17)
    
    bot.send_message(
        message.chat.id, 
        "📊 اختر السوق المُراد تحليله:", 
        reply_markup=markup
    )

@bot.message_handler(func=lambda message: True)
def handle_market_selection(message):
    market_name = message.text
    
    inline_markup = types.InlineKeyboardMarkup()
    inline_btn = types.InlineKeyboardButton('🔄 خذ صفقة أخرى', callback_data='another_trade')
    inline_markup.add(inline_btn)
    
    response_text = (
        f"📊 السوق : {market_name}\n\n"
        "⏱ الوقت : 30 ثانية\n\n"
        "🔴 الصفقة : بيع / Sell\n\n"
        "🚨 - تحذير مهم : يجب دخول الصفقة فوراً\n\n"
        "⚠️ - لا تدخل الصفقة إذا مر أكثر من 10 ثواني على الرسالة\n\n"
        "🔗 القناة الرسمية : https://t.me/Fahad1485"
    )
    
    bot.send_message(message.chat.id, response_text, reply_markup=inline_markup)

@bot.callback_query_handler(func=lambda call: call.data == 'another_trade')
def callback_query(call):
    bot.answer_callback_query(call.id, "جلب صفقة جديدة...")
    bot.send_message(call.message.chat.id, "📊 الرجاء اختيار السوق من القائمة بالأسفل لتوليد صفقة جديدة:")

print("Bot is running...")
bot.infinity_polling()
