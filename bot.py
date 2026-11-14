import os, time, math, sqlite3, logging, asyncio, threading
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN=os.getenv("BOT_TOKEN","").strip()
API_KEY=os.getenv("TWELVE_DATA_API_KEY","").strip()
if not BOT_TOKEN: raise RuntimeError("أضف BOT_TOKEN إلى Environment Variables.")
if not API_KEY: raise RuntimeError("أضف TWELVE_DATA_API_KEY إلى Environment Variables.")
BASE_URL="https://api.twelvedata.com"
DB_PATH=os.getenv("DB_PATH","trading_signals.db")
SCAN_EVERY=int(os.getenv("SCAN_EVERY_SECONDS","900"))
MAX_OPEN=int(os.getenv("MAX_OPEN_TRADES","20"))
MAX_NEW=int(os.getenv("MAX_NEW_TRADES_PER_CYCLE","12"))
COOLDOWN=int(os.getenv("COOLDOWN_SECONDS","900"))
MIN_SCORE=6
SAUDI_TZ=timezone(timedelta(hours=3))
logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("PaperBot")
TARGET_CHAT_ID=None
CHAT_LOCK=threading.Lock()
API_LOCK=threading.Lock()
CACHE_LOCK=threading.Lock()
LAST_API=0
CANDLES_CACHE={}
PRICE_CACHE={}

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        b=b"Paper Trading Bot running"
        self.send_response(200); self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass

def run_server():
    ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","10000"))),Health).serve_forever()
threading.Thread(target=run_server,daemon=True).start()

MARKETS=[
("EUR/USD","EUR/USD"),("GBP/USD","GBP/USD"),("USD/JPY","USD/JPY"),
("AUD/USD","AUD/USD"),("USD/CAD","USD/CAD"),("USD/CHF","USD/CHF"),
("NZD/USD","NZD/USD"),("EUR/GBP","EUR/GBP"),("EUR/JPY","EUR/JPY"),
("GBP/JPY","GBP/JPY"),("الذهب XAU/USD","XAU/USD"),("الفضة XAG/USD","XAG/USD"),
("Bitcoin","BTC/USD"),("Ethereum","ETH/USD"),("Solana","SOL/USD")]
INTERVALS=("1min","2min","3min","4min","5min","10min","15min","30min","1h")
LABEL={"1min":"1د","2min":"2د","3min":"3د","4min":"4د","5min":"5د","10min":"10د","15min":"15د","30min":"30د","1h":"1 ساعة"}
SECS={"1min":60,"2min":120,"3min":180,"4min":240,"5min":300,"10min":600,"15min":900,"30min":1800,"1h":3600}
SOURCE={"1min":("1min",60,1),"2min":("1min",60,2),"3min":("1min",60,3),"4min":("1min",60,4),"5min":("5min",300,1),"10min":("5min",300,2),"15min":("15min",900,1),"30min":("15min",900,2),"1h":("15min",900,4)}

# ---------- Database ----------
def db():
    return sqlite3.connect(DB_PATH,timeout=30)

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS paper_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
        symbol TEXT NOT NULL, market TEXT NOT NULL, interval TEXT NOT NULL,
        direction TEXT NOT NULL, entry REAL NOT NULL, exit REAL,
        score INTEGER, rsi REAL, adx REAL, atr REAL, due REAL NOT NULL,
        status TEXT DEFAULT 'OPEN', result TEXT, best REAL DEFAULT 0,
        worst REAL DEFAULT 0, reason TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS paper_status_due ON paper_trades(status,due)")
        c.execute("CREATE INDEX IF NOT EXISTS paper_dedupe ON paper_trades(symbol,interval,created)")
        c.commit()

def open_count():
    with db() as c: return c.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0]

def recent_trade(symbol,interval):
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM paper_trades WHERE symbol=? AND interval=? AND created>?",
                         (symbol,interval,time.time()-COOLDOWN)).fetchone()[0]>0

def add_trade(market,symbol,interval,r):
    now=time.time()
    with db() as c:
        cur=c.execute("""INSERT INTO paper_trades
        (created,symbol,market,interval,direction,entry,score,rsi,adx,atr,due)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (now,symbol,market,interval,r["direction"],r["price"],r["score"],r["rsi"],r["adx"],r["atr"],now+SECS[interval]))
        c.commit(); return cur.lastrowid

def open_trades():
    with db() as c:
        return c.execute("""SELECT id,symbol,market,interval,direction,entry,due,best,worst
        FROM paper_trades WHERE status='OPEN' ORDER BY due""").fetchall()

def update_excursion(tid,best,worst):
    with db() as c:
        c.execute("UPDATE paper_trades SET best=MAX(best,?),worst=MAX(worst,?) WHERE id=? AND status='OPEN'",(best,worst,tid))
        c.commit()

def close_trade(tid,price,result,reason):
    with db() as c:
        cur=c.execute("""UPDATE paper_trades SET exit=?,result=?,status='CLOSED',reason=?
        WHERE id=? AND status='OPEN'""",(price,result,reason,tid))
        c.commit(); return cur.rowcount>0

def stats():
    with db() as c:
        counts=dict(c.execute("SELECT COALESCE(result,'OPEN'),COUNT(*) FROM paper_trades GROUP BY COALESCE(result,'OPEN')").fetchall())
        intervals=c.execute("""SELECT interval,
        SUM(result='WIN'),SUM(result='LOSS'),SUM(result='TIE')
        FROM paper_trades WHERE status='CLOSED' GROUP BY interval""").fetchall()
        latest=c.execute("SELECT id,symbol,interval,direction,entry,exit,result,score FROM paper_trades ORDER BY id DESC LIMIT 8").fetchall()
    return counts,intervals,latest

# ---------- Market data ----------
def api_get(endpoint,params):
    global LAST_API
    with API_LOCK:
        delay=1.1-(time.time()-LAST_API)
        if delay>0: time.sleep(delay)
        p=dict(params); p["apikey"]=API_KEY
        response=requests.get(BASE_URL+"/"+endpoint,params=p,timeout=20)
        LAST_API=time.time()
    if response.status_code==429: raise ValueError("تجاوز حد طلبات Twelve Data (429).")
    response.raise_for_status(); data=response.json()
    if data.get("status")=="error" or "code" in data: raise ValueError(data.get("message","خطأ بيانات"))
    return data

def get_price(symbol):
    now=time.time()
    with CACHE_LOCK:
        x=PRICE_CACHE.get(symbol)
        if x and now-x[0]<8: return x[1]
    data=api_get("price",{"symbol":symbol})
    p=float(data["price"])
    if not math.isfinite(p) or p<=0: raise ValueError("سعر غير صالح")
    with CACHE_LOCK: PRICE_CACHE[symbol]=(now,p)
    return p

def parse_dt(s):
    for f in ("%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M"):
        try: return datetime.strptime(s,f).replace(tzinfo=timezone.utc)
        except ValueError: pass
    raise ValueError("تنسيق تاريخ غير معروف")

def aggregate(rows,seconds,factor):
    buckets={}
    size=seconds*factor
    for x in rows: buckets.setdefault(int(x["ts"]//size),[]).append(x)
    out=[]
    for group in buckets.values():
        group.sort(key=lambda x:x["ts"])
        if len(group)!=factor: continue
        out.append({"ts":group[0]["ts"],"open":group[0]["open"],"high":max(x["high"] for x in group),
                    "low":min(x["low"] for x in group),"close":group[-1]["close"]})
    return sorted(out,key=lambda x:x["ts"])

def get_candles(symbol,interval,limit=180):
    key=(symbol,interval,limit); now=time.time()
    with CACHE_LOCK:
        x=CANDLES_CACHE.get(key)
        if x and now-x[0]<25: return x[1]
    api_interval,seconds,factor=SOURCE[interval]
    data=api_get("time_series",{"symbol":symbol,"interval":api_interval,
        "outputsize":min(5000,max(limit*factor+30,160)),"timezone":"UTC","format":"JSON"})
    if not data.get("values"): raise ValueError("لا توجد شموع")
    rows=[]; now=time.time()
    for v in reversed(data["values"]):
        try:
            ts=parse_dt(v["datetime"]).timestamp()
            z={"ts":ts,"open":float(v["open"]),"high":float(v["high"]),
               "low":float(v["low"]),"close":float(v["close"])}
            if all(math.isfinite(n) and n>0 for n in z.values() if isinstance(n,(int,float))) and ts+seconds<=now:
                rows.append(z)
        except (ValueError,TypeError,KeyError): continue
    if factor>1: rows=aggregate(rows,seconds,factor)
    rows=rows[-limit:]
    if len(rows)<60: raise ValueError(f"شموع غير كافية: {len(rows)}")
    with CACHE_LOCK: CANDLES_CACHE[key]=(now,rows)
    return rows

# ---------- Indicators ----------
def ema(v,n):
    if len(v)<n: return [None]*len(v)
    a=2/(n+1); out=[sum(v[:n])/n]
    for x in v[n:]: out.append(a*x+(1-a)*out[-1])
    return [None]*(n-1)+out

def rsi(v,n=14):
    if len(v)<=n: return 50.
    d=[v[i]-v[i-1] for i in range(1,len(v))]
    gains=[max(x,0) for x in d]; losses=[max(-x,0) for x in d]
    ag=sum(gains[:n])/n; al=sum(losses[:n])/n
    for i in range(n,len(d)):
        ag=(ag*(n-1)+gains[i])/n; al=(al*(n-1)+losses[i])/n
    if al==0: return 100. if ag else 50.
    if ag==0: return 0.
    return 100-100/(1+ag/al)

def atr(c,n=14):
    tr=[]
    for i in range(1,len(c)):
        x,p=c[i],c[i-1]
        tr.append(max(x["high"]-x["low"],abs(x["high"]-p["close"]),abs(x["low"]-p["close"])))
    if len(tr)<n: return 0.
    z=sum(tr[:n])/n
    for x in tr[n:]: z=(z*(n-1)+x)/n
    return z

def adx(c,n=14):
    if len(c)<2*n+2: return 0.
    plus=[]; minus=[]; tr=[]
    for i in range(1,len(c)):
        x,p=c[i],c[i-1]; up=x["high"]-p["high"]; down=p["low"]-x["low"]
        plus.append(up if up>down and up>0 else 0.)
        minus.append(down if down>up and down>0 else 0.)
        tr.append(max(x["high"]-x["low"],abs(x["high"]-p["close"]),abs(x["low"]-p["close"])))
    dx=[]
    for i in range(n-1,len(tr)):
        s=i-n+1; total=sum(tr[s:i+1])
        if total<=0: continue
        pdi=100*sum(plus[s:i+1])/total; mdi=100*sum(minus[s:i+1])/total
        dx.append(100*abs(pdi-mdi)/(pdi+mdi) if pdi+mdi else 0)
    return sum(dx[-n:])/len(dx[-n:]) if len(dx)>=n else 0.

def trend(c):
    v=[x["close"] for x in c]; e20=ema(v,20); e50=ema(v,50)
    if e20[-1] is None or e50[-1] is None: return "NEUTRAL"
    slope=e20[-1]-e20[-4]
    if v[-1]>e20[-1]>e50[-1] and slope>0: return "UP"
    if v[-1]<e20[-1]<e50[-1] and slope<0: return "DOWN"
    return "NEUTRAL"

def analyze(symbol,interval):
    c=get_candles(symbol,interval); t5=trend(get_candles(symbol,"5min")); t15=trend(get_candles(symbol,"15min"))
    v=[x["close"] for x in c]; cur,prev=c[-1],c[-2]
    e9,e21,e50=ema(v,9),ema(v,21),ema(v,50)
    rv, av, dx=rsi(v),atr(c),adx(c)
    if av<=0: raise ValueError("ATR غير صالح")
    rng=cur["high"]-cur["low"]
    if rng<=0: raise ValueError("مدى الشمعة غير صالح")
    body=abs(cur["close"]-cur["open"])/rng
    upper=(cur["high"]-max(cur["open"],cur["close"]))/rng
    lower=(min(cur["open"],cur["close"])-cur["low"])/rng
    buy=sell=0; reasons=[]; blockers=[]
    if t5==t15=="UP": buy+=2; reasons.append("اتجاه 5 و15 دقيقة صاعد")
    elif t5==t15=="DOWN": sell+=2; reasons.append("اتجاه 5 و15 دقيقة هابط")
    else: blockers.append("تعارض اتجاه الفريمات الأكبر")
    if e9[-1]>e21[-1]>e50[-1]: buy+=2; reasons.append("المتوسطات مرتبة صعوداً")
    elif e9[-1]<e21[-1]<e50[-1]: sell+=2; reasons.append("المتوسطات مرتبة هبوطاً")
    else: blockers.append("المتوسطات غير متوافقة")
    if 52<=rv<=68: buy+=1
    elif 32<=rv<=48: sell+=1
    else: blockers.append(f"RSI غير مناسب: {rv:.1f}")
    if dx>=18: buy+=1; sell+=1
    else: blockers.append(f"ADX ضعيف: {dx:.1f}")
    if cur["close"]>cur["open"] and body>=.40 and upper<=.35: buy+=1; reasons.append("شمعة صاعدة قوية")
    elif cur["close"]<cur["open"] and body>=.40 and lower<=.35: sell+=1; reasons.append("شمعة هابطة قوية")
    else: blockers.append("الشمعة لا تؤكد الاتجاه")
    if rng>av*1.8: blockers.append("مدى الشمعة كبير مقارنة بـ ATR")
    if abs(cur["close"]-e9[-1])/av>1: blockers.append("السعر بعيد عن EMA9")
    if upper>=.5 and cur["close"]<cur["open"]: blockers.append("رفض سعري من الأعلى")
    if lower>=.5 and cur["close"]>cur["open"]: blockers.append("رفض سعري من الأسفل")
    if cur["close"]>cur["open"] and prev["close"]<prev["open"] and cur["close"]<prev["open"]: blockers.append("لم تستعد افتتاح الشمعة السابقة")
    if cur["close"]<cur["open"] and prev["close"]>prev["open"] and cur["close"]>prev["open"]: blockers.append("لم تكسر افتتاح الشمعة السابقة")
    live=get_price(symbol); distance=(live-cur["close"])/av
    if abs(distance)>.45: blockers.append("السعر اللحظي ابتعد عن إغلاق الشمعة")
    # Corrected: block only movement against the candidate direction.
    if buy>sell and distance<-.15: blockers.append("السعر اللحظي يتحرك عكس الصعود")
    if sell>buy and distance>.15: blockers.append("السعر اللحظي يتحرك عكس الهبوط")
    direction="WAIT"
    if buy>=MIN_SCORE and buy>sell and t5==t15=="UP" and not blockers: direction="UP"
    elif sell>=MIN_SCORE and sell>buy and t5==t15=="DOWN" and not blockers: direction="DOWN"
    return {"direction":direction,"price":live,"score":max(buy,sell),"rsi":rv,"adx":dx,"atr":av,
            "reasons":reasons,"blockers":blockers}

# ---------- Paper trade lifecycle ----------
def settle_result(direction,entry,exitp):
    tol=max(abs(entry)*1e-6,1e-10)
    if abs(exitp-entry)<=tol: return "TIE","السعر النهائي قريب من سعر الدخول"
    if (direction=="UP" and exitp>entry) or (direction=="DOWN" and exitp<entry):
        return "WIN","السعر النهائي تحرك مع اتجاه الصفقة"
    return "LOSS","السعر النهائي تحرك عكس اتجاه الصفقة"

async def send_user(app,text):
    with CHAT_LOCK: chat=TARGET_CHAT_ID
    if chat:
        try: await app.bot.send_message(chat_id=chat,text=text)
        except Exception: log.exception("تعذر إرسال رسالة تيليجرام")

async def monitor_trades(app):
    rows=await asyncio.to_thread(open_trades); prices={}
    for tid,symbol,market,interval,direction,entry,due,best_old,worst_old in rows:
        try:
            if symbol not in prices: prices[symbol]=await asyncio.to_thread(get_price,symbol)
            p=prices[symbol]; move=(p-entry)/entry*100
            best=max(move,0) if direction=="UP" else max(-move,0)
            worst=max(-move,0) if direction=="UP" else max(move,0)
            await asyncio.to_thread(update_excursion,tid,best,worst)
            if time.time()<due: continue
            result,reason=settle_result(direction,entry,p)
            if await asyncio.to_thread(close_trade,tid,p,result,reason):
                icon={"WIN":"🟢","LOSS":"🔴","TIE":"⚪"}[result]
                await send_user(app,f"{icon} نتيجة الصفقة الافتراضية #{tid}\n{market} ({symbol}) | {LABEL[interval]}\nالاتجاه: {direction}\nالدخول: {entry:.8g}\nالخروج: {p:.8g}\nالنتيجة: {result}\nأفضل حركة لصالحها: {best:.4f}%\nأكبر حركة ضدها: {worst:.4f}%\nالسبب: {reason}\n\nالمحاكاة حسب Twelve Data وليست تنفيذ EO.")
        except Exception as e: log.warning("monitor trade %s: %s",tid,e)

async def scan_all(app):
    with CHAT_LOCK: chat=TARGET_CHAT_ID
    if not chat: return
    capacity=max(0,MAX_OPEN-(await asyncio.to_thread(open_count)))
    if capacity<=0:
        await send_user(app,"⏸️ لم تُفتح صفقات جديدة: وصلت إلى الحد الأقصى للصفقات الافتراضية المفتوحة.")
        return
    made=checked=errors=0; found=[]
    for market,symbol in MARKETS:
        for interval in INTERVALS:
            if made>=min(capacity,MAX_NEW): break
            checked+=1
            try:
                if await asyncio.to_thread(recent_trade,symbol,interval): continue
                r=await asyncio.to_thread(analyze,symbol,interval)
                if r["direction"]=="WAIT": continue
                if await asyncio.to_thread(open_count)>=MAX_OPEN: break
                tid=await asyncio.to_thread(add_trade,market,symbol,interval,r)
                made+=1
                found.append(f"#{tid} {market} | {LABEL[interval]} | {r['direction']} | دخول {r['price']:.8g} | نقاط {r['score']}")
            except Exception as e:
                errors+=1; log.warning("scan %s %s: %s",symbol,interval,e)
        if made>=min(capacity,MAX_NEW): break
    await send_user(app,f"🔎 انتهت دورة المراقبة\nتم المرور على: {checked}\nصفقات افتراضية جديدة: {made}\nأخطاء بيانات: {errors}\nالمفتوحة الآن: {await asyncio.to_thread(open_count)}\nالدورة التالية بعد {SCAN_EVERY//60} دقيقة.")
    for line in found: await send_user(app,"🧪 فتح صفقة افتراضية تلقائياً\n"+line)

async def background_worker(app):
    last_scan=0
    while True:
        try:
            await monitor_trades(app)
            if time.time()-last_scan>=SCAN_EVERY:
                await scan_all(app); last_scan=time.time()
        except asyncio.CancelledError: raise
        except Exception: log.exception("background worker")
        await asyncio.sleep(20)

# ---------- Telegram ----------
def keyboard():
    rows=[]
    for i in range(0,len(MARKETS),2):
        row=[InlineKeyboardButton(MARKETS[i][0],callback_data="M|"+MARKETS[i][1])]
        if i+1<len(MARKETS): row.append(InlineKeyboardButton(MARKETS[i+1][0],callback_data="M|"+MARKETS[i+1][1]))
        rows.append(row)
    rows += [[InlineKeyboardButton("📊 إحصائيات Paper",callback_data="STATS")],
             [InlineKeyboardButton("🔍 فحص جميع الأسواق الآن",callback_data="SCAN")]]
    return InlineKeyboardMarkup(rows)

def interval_keyboard(symbol):
    return InlineKeyboardMarkup([[InlineKeyboardButton(LABEL[x],callback_data=f"A|{symbol}|{x}")] for x in INTERVALS]+
                                [[InlineKeyboardButton("⬅️ الأسواق",callback_data="BACK")]])

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    with CHAT_LOCK: TARGET_CHAT_ID=update.effective_chat.id
    await update.effective_message.reply_text(
        "🤖 بوت Paper Trading\nمراقبة تلقائية لجميع الأسواق الـ15 والفريمات التسعة.\n"
        "عند ظهور فرصة يفتح صفقة افتراضية ويتابعها حتى الإغلاق ويسجل النتيجة.\n"
        "لا يفتح صفقات مالية حقيقية.\n\nالأوامر: /stats /paper /scan /help",reply_markup=keyboard())

async def help_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start القائمة\n/stats إحصائيات الصفقات الافتراضية\n/paper الصفقات المفتوحة\n/scan فحص فوري\n"
        "الفريمات: 1min,2min,3min,4min,5min,10min,15min,30min,1h\n"
        "تعمل المحاكاة على أسعار Twelve Data وقد تختلف عن EO.")

async def stats_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    counts,intervals,latest=await asyncio.to_thread(stats)
    w,l,t,o=(counts.get("WIN",0),counts.get("LOSS",0),counts.get("TIE",0),counts.get("OPEN",0))
    n=w+l; lines=[f"📊 Paper Trading\nفوز: {w} | خسارة: {l} | تعادل: {t} | مفتوحة: {o}",
                  f"نسبة الفوز (دون التعادل): {w/n*100 if n else 0:.1f}%","",
                  "حسب الفريم:"]
    for it,a,b,c in intervals:
        a,b,c=a or 0,b or 0,c or 0; total=a+b
        lines.append(f"{LABEL.get(it,it)}: فوز {a} / خسارة {b} / تعادل {c} | نجاح {a/total*100 if total else 0:.1f}%")
    lines.append("\nآخر الصفقات:")
    for row in latest:
        tid,sym,it,direction,entry,exitp,res,score=row
        lines.append(f"#{tid} {sym} {LABEL[it]} {direction} {res or 'OPEN'} | نقاط {score}")
    await update.effective_message.reply_text("\n".join(lines)[:3900])

async def paper_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    rows=await asyncio.to_thread(open_trades)
    if not rows:
        await update.effective_message.reply_text("لا توجد صفقات افتراضية مفتوحة."); return
    lines=["🧪 الصفقات المفتوحة:"]
    for tid,sym,market,it,direction,entry,due,best,worst in rows[:25]:
        left=max(0,int(due-time.time()))
        lines.append(f"#{tid} {market} | {LABEL[it]} | {direction}\nدخول {entry:.8g} | متبقي {left//60}:{left%60:02d}")
    await update.effective_message.reply_text("\n\n".join(lines)[:3900])

async def scan_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    with CHAT_LOCK: TARGET_CHAT_ID=update.effective_chat.id
    await update.effective_message.reply_text("بدأ الفحص الشامل؛ قد يستغرق عدة دقائق بسبب حدود مزود البيانات.")
    await scan_all(context.application)

async def buttons(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    global TARGET_CHAT_ID
    with CHAT_LOCK: TARGET_CHAT_ID=q.message.chat_id
    data=q.data
    if data=="BACK":
        await q.edit_message_text("اختر السوق:",reply_markup=keyboard()); return
    if data=="SCAN":
        await q.edit_message_text("بدأ فحص جميع الأسواق والفريمات...")
        await scan_all(context.application); return
    if data=="STATS":
        c,_,_=await asyncio.to_thread(stats)
        w,l,t,o=c.get("WIN",0),c.get("LOSS",0),c.get("TIE",0),c.get("OPEN",0); n=w+l
        await q.edit_message_text(f"📊 Paper Trading\nفوز: {w}\nخسارة: {l}\nتعادل: {t}\nمفتوحة: {o}\nنسبة الفوز: {w/n*100 if n else 0:.1f}%",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الأسواق",callback_data="BACK")]])); return
    if data.startswith("M|"):
        sym=data.split("|",1)[1]
        await q.edit_message_text(f"اختر فريم التحليل لـ {sym}:",reply_markup=interval_keyboard(sym)); return
    if data.startswith("A|"):
        _,sym,it=data.split("|")
        await q.edit_message_text("⏳ يجري التحليل...")
        try:
            r=await asyncio.to_thread(analyze,sym,it)
            if r["direction"]=="WAIT":
                msg=f"⚪ انتظار — لا دخول\n{sym} | {LABEL[it]}\nRSI {r['rsi']:.1f} | ADX {r['adx']:.1f}\n"+"\n".join("• "+x for x in r["blockers"][:8])
            else:
                if await asyncio.to_thread(open_count)>=MAX_OPEN: raise ValueError("بلغت الصفقات المفتوحة الحد الأقصى.")
                tid=await asyncio.to_thread(add_trade,sym,sym,it,r)
                msg=f"🧪 فتحت صفقة افتراضية #{tid}\n{sym} | {LABEL[it]} | {r['direction']}\nالدخول: {r['price']:.8g}\nستُغلق تلقائياً بعد مدة الفريم."
            await q.edit_message_text(msg,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ الأسواق",callback_data="BACK")]]))
        except Exception as e:
            await q.edit_message_text(f"تعذر التحليل: {str(e)[:300]}",reply_markup=keyboard())

async def post_init(app):
    app.create_task(background_worker(app))

async def error_handler(update:object,context:ContextTypes.DEFAULT_TYPE):
    log.error("Telegram error",exc_info=context.error)

def main():
    init_db()
    app=ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(CommandHandler("stats",stats_cmd))
    app.add_handler(CommandHandler("paper",paper_cmd))
    app.add_handler(CommandHandler("scan",scan_cmd))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_error_handler(error_handler)
    log.info("Paper Trading bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
