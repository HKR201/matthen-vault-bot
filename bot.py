import os
import io
import asyncio
import logging
import zipfile
import bcrypt
import exifread
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    BufferedInputFile,
    BotCommand,
    BotCommandScopeDefault
)
from supabase import create_client, Client
from google import genai
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
CHANNEL_1_ID = int(os.getenv("CHANNEL_1_ID", "0"))
CHANNEL_2_ID = int(os.getenv("CHANNEL_2_ID", "0"))
CHANNEL_3_ID = int(os.getenv("CHANNEL_3_ID", "0"))
CHANNEL_4_ID = int(os.getenv("CHANNEL_4_ID", "0"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

class Form(StatesGroup):
    waiting_for_nickname = State()
    waiting_for_channel4_pin = State()
    waiting_for_new_pin = State()

def extract_exif_metadata(image_bytes: bytes):
    year = None
    location = "Unknown"
    try:
        tags = exifread.process_file(io.BytesIO(image_bytes), details=False)
        date_str = tags.get('EXIF DateTimeOriginal') or tags.get('Image DateTime')
        if date_str:
            year = int(str(date_str).split(':')[0])
    except Exception as e:
        logger.error(f"EXIF error: {e}")
    return year, location

async def analyze_photo_with_gemini(image_bytes: bytes):
    if not ai_client:
        return "General", None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        prompt = (
            "Analyze this family photo. Return ONLY two values comma-separated: "
            "Category (e.g. Nature, Landscape, Portrait, Document, Event), PersonTag (or None). "
            "Example: Portrait, Family"
        )
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=[img, prompt]
        )
        text = response.text.strip()
        parts = [p.strip() for p in text.split(",")]
        category = parts[0] if len(parts) > 0 else "General"
        person_tag = parts[1] if len(parts) > 1 and parts[1].lower() != "none" else None
        return category, person_tag
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return "General", None

async def daily_backup_task():
    try:
        tables = ["users", "files", "tags", "file_tags", "system_settings"]
        backup_data = {}
        for t in tables:
            res = supabase.table(t).select("*").execute()
            backup_data[f"{t}.json"] = str(res.data)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for fname, content in backup_data.items():
                zip_file.writestr(fname, content)
        zip_buffer.seek(0)

        doc = BufferedInputFile(zip_buffer.getvalue(), filename=f"backup_{datetime.now().strftime('%Y%m%d')}.zip")
        await bot.send_document(chat_id=CHANNEL_3_ID, document=doc, caption="📦 Automated Daily Database Backup")
        logger.info("Backup uploaded to Channel 3.")
    except Exception as e:
        logger.error(f"Backup failed: {e}")

# ----------------- ONBOARDING & MENU -----------------
@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    res = supabase.table("users").select("*").eq("telegram_id", user_id).execute()
    
    if not res.data:
        await message.answer("မင်္ဂလာပါ! Matthen Vault Bot မှ ကြိုဆိုပါတယ်။\nသင့်ရဲ့ အမည်ပြောင် (Nickname - ဥပမာ: ဖေဖေ၊ မေမေ) ကို ရိုက်ထည့်ပေးပါ:")
        await state.set_state(Form.waiting_for_nickname)
    else:
        user = res.data[0]
        await message.answer(
            f"ကြိုဆိုပါတယ် {user['nickname']}!\n\n"
            "အောက်ပါ လုပ်ဆောင်ချက်များကို Menu မှတစ်ဆင့် သုံးနိုင်ပါသည်:\n"
            "🔍 /search - ဖိုင်များ ပြန်လည်ရှာဖွေရန်\n"
            "🏷️ /tags - စာချုပ် Tags များ ကြည့်ရန်\n"
            "🔑 /setpin - Channel 4 PIN သတ်မှတ်ရန် (Admin Only)\n\n"
            "ပုံ/ဗီဒီယို/စာရွက်စာတမ်းများကို မူရင်းအတိုင်း တိုက်ရိုက် ပေးပို့သိမ်းဆည်းနိုင်ပါသည်။"
        )

@dp.message(Form.waiting_for_nickname)
async def process_nickname(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    nickname = message.text.strip()
    role = "ADMIN" if user_id == ADMIN_USER_ID else "MEMBER"
    
    supabase.table("users").insert({
        "telegram_id": user_id,
        "nickname": nickname,
        "role": role,
        "is_whitelisted": True
    }).execute()
    
    await state.clear()
    await message.answer(f"မှတ်တမ်းတင်ပြီးပါပြီ {nickname}! အသုံးပြုနိုင်ပါပြီ။")

# ----------------- INGESTION ENGINE -----------------
@dp.message(F.photo | F.video | F.document)
async def handle_media_upload(message: types.Message):
    user_id = message.from_user.id
    caption = message.caption or ""
    tags_list = [t for t in caption.split() if t.startswith("#")]
    
    # 1. Super-Private Check
    if "#private" in caption or "#hide" in caption:
        f_msg = await message.forward(chat_id=CHANNEL_4_ID)
        file_id = message.photo[-1].file_id if message.photo else (message.video.file_id if message.video else message.document.file_id)
        f_type = "PHOTO" if message.photo else ("VIDEO" if message.video else "DOCUMENT")
        
        supabase.table("files").insert({
            "telegram_file_id": file_id,
            "telegram_message_id": f_msg.message_id,
            "vault_channel": "CHANNEL_4",
            "file_type": f_type,
            "uploader_id": user_id,
            "access_level": "SUPER_PRIVATE",
            "is_super_private": True
        }).execute()
        await message.reply("🔒 Super-Private Vault (Channel 4) ထဲသို့ လုံခြုံစွာ သိမ်းဆည်းလိုက်ပါပြီ။")
        return

    # Document / Photo Check
    is_doc_tag = any(t in caption for t in ["#စာချုပ်", "#doc", "#နယ်", "#ရွာ"])
    is_image_doc = message.document and message.document.mime_type and message.document.mime_type.startswith("image/")

    # 2. Channel 3 (PDFs / Scans tagged with #စာချုပ်)
    if (message.document and not is_image_doc) or (is_image_doc and is_doc_tag):
        f_msg = await message.forward(chat_id=CHANNEL_3_ID)
        file_id = message.document.file_id if message.document else message.photo[-1].file_id
        
        res = supabase.table("files").insert({
            "telegram_file_id": file_id,
            "telegram_message_id": f_msg.message_id,
            "vault_channel": "CHANNEL_3",
            "file_type": "DOCUMENT",
            "uploader_id": user_id,
            "access_level": "SHARED"
        }).execute()
        
        if res.data and tags_list:
            file_uuid = res.data[0]['id']
            for tag in tags_list:
                tag_record = supabase.table("tags").upsert({"tag_name": tag}, on_conflict="tag_name").execute()
                tag_id = tag_record.data[0]['id']
                supabase.table("file_tags").insert({"file_id": file_uuid, "tag_id": tag_id}).execute()
                
        await message.reply("📄 Docs & System Vault (Channel 3) ထဲသို့ သိမ်းဆည်းပြီး Tag တွဲပေးလိုက်ပါပြီ။")
        return

    # 3. Channel 2 (Videos)
    if message.video:
        f_msg = await message.forward(chat_id=CHANNEL_2_ID)
        supabase.table("files").insert({
            "telegram_file_id": message.video.file_id,
            "telegram_message_id": f_msg.message_id,
            "vault_channel": "CHANNEL_2",
            "file_type": "VIDEO",
            "uploader_id": user_id,
            "year": datetime.now().year,
            "access_level": "SHARED"
        }).execute()
        await message.reply("🎥 Videos Vault (Channel 2) ထဲသို့ မူရင်းအရည်အသွေးအတိုင်း သိမ်းဆည်းလိုက်ပါပြီ။")
        return

    # 4. Channel 1 (Standard Photos & Uncompressed Images)
    if message.photo or is_image_doc:
        f_msg = await message.forward(chat_id=CHANNEL_1_ID)
        file_id = message.document.file_id if is_image_doc else message.photo[-1].file_id
        
        file_info = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        img_bytes = file_bytes.read()

        year, location = extract_exif_metadata(img_bytes)
        year = year or datetime.now().year
        category, person_tag = await analyze_photo_with_gemini(img_bytes)

        supabase.table("files").insert({
            "telegram_file_id": file_id,
            "telegram_message_id": f_msg.message_id,
            "vault_channel": "CHANNEL_1",
            "file_type": "PHOTO",
            "uploader_id": user_id,
            "year": year,
            "location": location,
            "ai_category": category,
            "person_tag": person_tag,
            "access_level": "SHARED"
        }).execute()
        await message.reply(f"📷 Photos Vault (Channel 1) ထဲသို့ မူရင်းအတိုင်း သိမ်းဆည်းပြီးပါပြီ။\n🏷️ AI Category: {category}")

# ----------------- DYNAMIC SEARCH UI -----------------
@dp.message(Command("search"))
async def search_root_handler(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📷 ဓာတ်ပုံများ", callback_data="search_photos_root"),
            InlineKeyboardButton(text="🎥 ဗီဒီယိုများ", callback_data="search_videos")
        ],
        [
            InlineKeyboardButton(text="📄 စာချုပ်စာတမ်းများ", callback_data="search_docs"),
            InlineKeyboardButton(text="🔒 Channel 4 Vault", callback_data="unlock_ch4")
        ]
    ])
    await message.answer("ရှာဖွေလိုသော အုပ်စုကို ရွေးချယ်ပါ:", reply_markup=kb)

@dp.callback_query(F.data == "search_photos_root")
async def search_photos_options(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 ခုနှစ်အလိုက် ရှာရန်", callback_data="photo_by_year")],
        [InlineKeyboardButton(text="🏷️ AI အမျိုးအစားအလိုက် ရှာရန်", callback_data="photo_by_category")],
        [InlineKeyboardButton(text="⬅️ နောက်သို့", callback_data="back_to_root")]
    ])
    await callback.message.edit_text("ဓာတ်ပုံများ ရှာဖွေမည့် ပုံစံကို ရွေးပါ:", reply_markup=kb)

@dp.callback_query(F.data == "photo_by_year")
async def search_photos_by_year(callback: types.CallbackQuery):
    years_res = supabase.table("files").select("year").eq("vault_channel", "CHANNEL_1").execute()
    years = sorted(list(set(r['year'] for r in years_res.data if r.get('year'))))
    
    kb_buttons = [[InlineKeyboardButton(text=f"📅 {y}", callback_data=f"filter_year_{y}")] for y in years]
    kb_buttons.append([InlineKeyboardButton(text="⬅️ နောက်သို့", callback_data="search_photos_root")])
    await callback.message.edit_text("ခုနှစ်အလိုက် ရွေးချယ်ပါ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

@dp.callback_query(F.data == "photo_by_category")
async def search_photos_by_category(callback: types.CallbackQuery):
    cat_res = supabase.table("files").select("ai_category").eq("vault_channel", "CHANNEL_1").execute()
    categories = sorted(list(set(r['ai_category'] for r in cat_res.data if r.get('ai_category'))))
    
    kb_buttons = [[InlineKeyboardButton(text=f"🏷️ {c}", callback_data=f"filter_cat_{c}")] for c in categories]
    kb_buttons.append([InlineKeyboardButton(text="⬅️ နောက်သို့", callback_data="search_photos_root")])
    await callback.message.edit_text("AI အမျိုးအစားအလိုက် ရွေးချယ်ပါ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

@dp.callback_query(F.data.startswith("filter_year_"))
async def show_files_by_year(callback: types.CallbackQuery):
    year = int(callback.data.split("_")[2])
    res = supabase.table("files").select("*").eq("vault_channel", "CHANNEL_1").eq("year", year).limit(5).execute()
    
    if not res.data:
        await callback.answer("ဖိုင်မရှိသေးပါခင်ဗျာ။", show_alert=True)
        return
        
    for item in res.data:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬇️ Download Original", callback_data=f"getraw_{item['id']}")]
        ])
        await bot.send_photo(
            chat_id=callback.from_user.id,
            photo=item['telegram_file_id'],
            caption=f"📅 Year: {item['year']} | 🏷️ {item.get('ai_category', 'General')}",
            reply_markup=kb
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("filter_cat_"))
async def show_files_by_category(callback: types.CallbackQuery):
    cat = callback.data.split("_")[2]
    res = supabase.table("files").select("*").eq("vault_channel", "CHANNEL_1").eq("ai_category", cat).limit(5).execute()
    
    if not res.data:
        await callback.answer("ဖိုင်မရှိသေးပါခင်ဗျာ။", show_alert=True)
        return
        
    for item in res.data:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬇️ Download Original", callback_data=f"getraw_{item['id']}")]
        ])
        await bot.send_photo(
            chat_id=callback.from_user.id,
            photo=item['telegram_file_id'],
            caption=f"🏷️ Category: {item.get('ai_category')} | 📅 Year: {item['year']}",
            reply_markup=kb
        )
    await callback.answer()

@dp.callback_query(F.data == "back_to_root")
async def back_to_root_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📷 ဓာတ်ပုံများ", callback_data="search_photos_root"),
            InlineKeyboardButton(text="🎥 ဗီဒီယိုများ", callback_data="search_videos")
        ],
        [
            InlineKeyboardButton(text="📄 စာချုပ်စာတမ်းများ", callback_data="search_docs"),
            InlineKeyboardButton(text="🔒 Channel 4 Vault", callback_data="unlock_ch4")
        ]
    ])
    await callback.message.edit_text("ရှာဖွေလိုသော အုပ်စုကို ရွေးချယ်ပါ:", reply_markup=kb)

@dp.callback_query(F.data.startswith("getraw_"))
async def stream_raw_file(callback: types.CallbackQuery):
    file_uuid = callback.data.split("_")[1]
    res = supabase.table("files").select("*").eq("id", file_uuid).execute()
    if res.data:
        f = res.data[0]
        channel_id = globals().get(f"{f['vault_channel']}_ID")
        await bot.copy_message(
            chat_id=callback.from_user.id,
            from_chat_id=channel_id,
            message_id=f['telegram_message_id']
        )
    await callback.answer()

# ----------------- CHANNEL 4 & PIN -----------------
@dp.callback_query(F.data == "unlock_ch4")
async def prompt_ch4_pin(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("🔒 Super-Private Vault PIN ရိုက်ထည့်ပေးပါ:")
    await state.set_state(Form.waiting_for_channel4_pin)
    await callback.answer()

@dp.message(Form.waiting_for_channel4_pin)
async def verify_ch4_pin(message: types.Message, state: FSMContext):
    entered_pin = message.text.strip()
    await message.delete()

    res = supabase.table("system_settings").select("key_value_hash").eq("key_name", "CH4_MASTER_PIN").execute()
    if not res.data:
        await message.answer("⚠️ Master PIN မသတ်မှတ်ရသေးပါ။ Admin ထံ /setpin ဖြင့် PIN အသစ် သတ်မှတ်ခိုင်းပါ။")
        await state.clear()
        return

    stored_hash = res.data[0]['key_value_hash'].encode('utf-8')
    if bcrypt.checkpw(entered_pin.encode('utf-8'), stored_hash):
        files_res = supabase.table("files").select("*").eq("vault_channel", "CHANNEL_4").execute()
        await message.answer(f"🔓 PIN မှန်ကန်ပါသည်။ Channel 4 တွင် ဖိုင်စုစုပေါင်း {len(files_res.data)} ခု ရှိပါသည်။")
    else:
        await message.answer("❌ PIN မှားယွင်းနေပါသည်။")
    await state.clear()

@dp.message(Command("setpin"))
async def set_new_pin(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        await message.reply("Admin သာလျှင် PIN သတ်မှတ်ခွင့်ရှိသည်။")
        return
    await message.answer("PIN အသစ် ရိုက်ထည့်ပေးပါ:")
    await state.set_state(Form.waiting_for_new_pin)

@dp.message(Form.waiting_for_new_pin)
async def save_new_pin(message: types.Message, state: FSMContext):
    new_pin = message.text.strip()
    await message.delete()
    hashed = bcrypt.hashpw(new_pin.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    supabase.table("system_settings").upsert({
        "key_name": "CH4_MASTER_PIN",
        "key_value_hash": hashed,
        "updated_by": message.from_user.id
    }).execute()
    
    await message.answer("✅ Channel 4 Master PIN အသစ်ကို လုံခြုံစွာ သတ်မှတ်သိမ်းဆည်းလိုက်ပါပြီ။")
    await state.clear()

# ----------------- WEB SERVER & STARTUP -----------------
async def handle_ping(request):
    return web.Response(text="Matthen Vault Bot is running 24/7!")

async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Bot စတင်ရန် / မိတ်ဆက်"),
        BotCommand(command="search", description="ဖိုင်များ ရှာဖွေရန်"),
        BotCommand(command="setpin", description="Channel 4 PIN သတ်မှတ်ရန် (Admin)")
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

async def main():
    scheduler.add_job(daily_backup_task, "cron", hour=2, minute=0)
    scheduler.start()

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await bot.delete_webhook(drop_pending_updates=True)
    await set_bot_commands(bot)
    logger.info("Matthen Vault Bot is fully upgraded and running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
