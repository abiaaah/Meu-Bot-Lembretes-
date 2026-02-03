
import logging
import datetime
import os
import pytz
import pytesseract
import time
import json
from PIL import Image
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from openai import OpenAI

# Configuração de Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configurações via Variáveis de Ambiente (Segurança para Hospedagem)
TOKEN = os.getenv("TELEGRAM_TOKEN", "8582619524:AAHwWm1GhAUZaV7HFTUQ53u8tFcAMiooPOI")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TIMEZONE = pytz.timezone("America/Sao_Paulo")
DATA_FILE = "reminders_db.json"

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAI()

def load_db():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        except: return {}
    return {}

def save_db(db):
    with open(DATA_FILE, 'w') as f:
        json.dump(db, f, indent=4)

def add_reminder_to_db(chat_id, subject, run_date, recurrence):
    db = load_db()
    chat_id_str = str(chat_id)
    if chat_id_str not in db:
        db[chat_id_str] = []
    
    db[chat_id_str].append({
        "subject": subject,
        "run_date": run_date.strftime("%Y-%m-%d %H:%M:%S"),
        "recurrence": recurrence
    })
    save_db(db)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Olá! Sou seu Agente de Lembretes 24h.\n\n"
        "✅ **Funcionalidades Ativas:**\n"
        "• Texto, Voz 🎙️ e Imagem 📸\n"
        "• Aniversários Anuais 🎂\n"
        "• `/listar` para ver agendamentos\n"
        "• `/limpar` para apagar tudo"
    )

async def send_reminder_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        await context.bot.send_message(chat_id=job.chat_id, text=f"🔔 LEMBRETE: {job.data}")
    except Exception as e:
        logger.error(f"Erro ao disparar: {e}")

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = load_db()
    user_reminders = db.get(str(chat_id), [])
    
    now = datetime.datetime.now(TIMEZONE)
    future_reminders = []
    for r in user_reminders:
        try:
            dt = datetime.datetime.strptime(r['run_date'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
            if dt > now:
                future_reminders.append((dt, r['subject']))
        except: continue
    
    if not future_reminders:
        await update.message.reply_text("Nenhum lembrete agendado. 😊")
        return
    
    future_reminders.sort()
    msg = "📂 **Seus Lembretes:**\n\n"
    for i, (dt, subject) in enumerate(future_reminders, 1):
        msg += f"{i}. 📝 {subject}\n   📅 {dt.strftime('%d/%m/%Y às %H:%M')}\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def clear_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = load_db()
    db[str(chat_id)] = []
    save_db(db)
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    await update.message.reply_text("✅ Lista limpa!")

def extract_reminders_with_llm(text):
    now_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    prompt = (
        f"Analise: \"{text}\"\nData Atual: {now_str}\n\n"
        "Extraia lembretes. Formato: ASSUNTO | YYYY-MM-DD HH:MM:SS | RECORRENCIA\n"
        "Se for aniversário (Nome - DD/MM), use RECORRENCIA: anual e HORA: 09:00:00.\n"
        "Se não houver hora, use 08:00:00. Datas relativas (amanhã, daqui um mês) devem ser calculadas."
    )
    try:
        response = client.chat.completions.create(model="gpt-4.1-mini", messages=[{"role": "user", "content": prompt}])
        lines = response.choices[0].message.content.strip().split('\n')
        results = []
        for line in lines:
            if "|" in line:
                parts = line.split("|")
                try:
                    dt = datetime.datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
                    results.append((parts[0].strip(), dt, parts[2].strip().lower()))
                except: continue
        return results
    except: return []

async def process_reminders(update, context, reminders):
    chat_id = update.effective_chat.id
    if not reminders:
        await update.message.reply_text("Não entendi o lembrete. Tente ser mais específico.")
        return

    for subject, run_date, recurrence in reminders:
        if recurrence == "anual" and run_date < datetime.datetime.now(TIMEZONE):
            run_date = run_date.replace(year=run_date.year + 1)
        
        if run_date > datetime.datetime.now(TIMEZONE):
            add_reminder_to_db(chat_id, subject, run_date, recurrence)
            context.job_queue.run_once(
                send_reminder_callback, 
                run_date, 
                data=subject if recurrence != "anual" else f"Aniversário de {subject} 🎂", 
                chat_id=chat_id, 
                name=str(chat_id)
            )
    await update.message.reply_text(f"✅ Agendado! Verifique em /listar.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("📸 Analisando imagem...")
    file = await update.message.photo[-1].get_file()
    path = f"photo_{chat_id}.jpg"
    await file.download_to_drive(path)
    try:
        text = pytesseract.image_to_string(Image.open(path), lang='por')
        os.remove(path)
        await process_reminders(update, context, extract_reminders_with_llm(text))
    except Exception as e:
        logger.error(f"Erro OCR: {e}")
        await update.message.reply_text("Erro ao ler a imagem.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🎙️ Processando áudio...")
    file = await update.message.voice.get_file()
    path = f"voice_{chat_id}.ogg"
    await file.download_to_drive(path)
    try:
        with open(path, "rb") as f:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=f, language="pt")
        os.remove(path)
        await update.message.reply_text(f"💬 Entendi: \"{transcript.text}\"")
        await process_reminders(update, context, extract_reminders_with_llm(transcript.text))
    except Exception as e:
        logger.error(f"Erro Voz: {e}")
        await update.message.reply_text("Erro ao processar áudio.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_reminders(update, context, extract_reminders_with_llm(update.message.text))

def sync_jobs(application):
    db = load_db()
    now = datetime.datetime.now(TIMEZONE)
    for chat_id, reminders in db.items():
        for r in reminders:
            try:
                dt = datetime.datetime.strptime(r['run_date'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
                if dt > now:
                    application.job_queue.run_once(send_reminder_callback, dt, data=r['subject'], chat_id=int(chat_id), name=chat_id)
            except: continue

if __name__ == '__main__':
    while True:
        try:
            req = HTTPXRequest(connect_timeout=60.0, read_timeout=60.0)
            app = ApplicationBuilder().token(TOKEN).request(req).build()
            app.add_handler(CommandHandler('start', start))
            app.add_handler(CommandHandler('listar', list_reminders))
            app.add_handler(CommandHandler('limpar', clear_reminders))
            app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
            app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
            app.add_handler(MessageHandler(filters.VOICE, handle_voice))
            sync_jobs(app)
            logger.info("Bot Iniciado.")
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            logger.error(f"Erro fatal: {e}")
            time.sleep(10)
