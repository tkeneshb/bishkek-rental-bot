"""
bot.py — полный Telegram-бот с многоуровневой защитой от риелторов

Команды покупателя:  /start /subscribe /mysubs /search /stats
Команды собственника: /post /mylistings
Команды модератора:   /modqueue
"""

import os
from dotenv import load_dotenv
load_dotenv()
import re
import json
import asyncio
import logging
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    filters, ContextTypes,
)
from telegram.error import Forbidden, BadRequest

from database import (
    init_db, save_user, is_user_banned,
    add_subscription, get_active_subscriptions, get_user_subscriptions,
    deactivate_subscription, listing_matches_sub,
    get_new_owner_listings, already_notified, record_notification, mark_notified,
    save_listing, update_classification, register_phone, register_photo_hashes,
    get_modqueue, approve_listing, reject_listing, pull_to_modqueue,
    get_stats, get_conn,
)
from defense import (
    normalize_phone, run_full_check, handle_complaint,
    classify_text, COMPLAINT_THRESHOLD,
)
from photo_guard import check_photos_telegram, phash_from_telegram_file
from parser import run_pipeline

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

# ── Состояния ─────────────────────────────────────────────────────────────
S_DIST, S_RMIN, S_RMAX, S_PMIN, S_PMAX = range(5)
(P_DEAL, P_ROOMS, P_DIST2, P_FLOOR, P_AREA,
 P_COND, P_PRICE, P_AMEN, P_DESC, P_PHONE,
 P_PHOTO, P_CONFIRM) = range(12, 24)

DISTRICTS = ["Свердловский","Октябрьский","Ленинский","Первомайский","Аламединский","Любой"]

kb_dist   = ReplyKeyboardMarkup([DISTRICTS[:3], DISTRICTS[3:]], resize_keyboard=True, one_time_keyboard=True)
kb_rooms  = ReplyKeyboardMarkup([["1","2","3","4","5+"]], resize_keyboard=True, one_time_keyboard=True)
kb_cond   = ReplyKeyboardMarkup(
    [["✨ Евроремонт","🏠 Хороший ремонт"],["🔨 Требует ремонта","🏗 Черновая"]],
    resize_keyboard=True, one_time_keyboard=True
)
kb_amen   = ReplyKeyboardMarkup(
    [["🚗 Парковка","🛗 Лифт","🔒 Охрана"],["🌳 Двор","🔥 Газ","💧 Отопление"],["➡️ Готово"]],
    resize_keyboard=True, one_time_keyboard=False
)
kb_confirm = ReplyKeyboardMarkup([["✅ Опубликовать","✏️ Заново","❌ Отмена"]], resize_keyboard=True, one_time_keyboard=True)


# ── Хэлперы ───────────────────────────────────────────────────────────────

def fmt_listing(row, show_source=False) -> str:
    price = f"${row['price_usd']:,}" if row["price_usd"] else "не указана"
    rooms = f"{row['rooms']}-комн." if row["rooms"] else ""
    area  = f"{row['area_sqm']} кв.м" if row["area_sqm"] else ""
    parts = " · ".join(p for p in [rooms, area, row["floor"] or ""] if p)
    src   = ""
    if show_source:
        src = "📝 Прямая подача\n" if row["source"] == "owner_direct" else "🌐 Парсинг\n"
    score = int((row["owner_score"] or 0) * 100)
    url_line = f"\n🔗 [Открыть]({row['url']})" if row["url"] else ""
    return (
        f"{src}🏠 *{row['title'] or 'Квартира'}*\n"
        f"📍 {row['district'] or '—'}\n"
        f"📐 {parts or '—'}\n"
        f"💰 {price}\n"
        f"📞 {row['phone'] or 'в объявлении'}\n"
        f"✅ Собственник {score}%{url_line}"
    )


async def guard_banned(update: Update) -> bool:
    banned, reason = is_user_banned(update.effective_user.id)
    if banned:
        await update.message.reply_text(f"⛔ Аккаунт заблокирован.\nПричина: {reason}")
        return True
    return False


# ── /start ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    save_user(u.id, u.username or "", u.first_name or "")
    await update.message.reply_text(
        f"👋 Привет, {u.first_name}!\n\n"
        "Связываю покупателей и собственников Бишкека — без риелторов.\n\n"
        "🔍 *Ищешь квартиру?*\n"
        "/subscribe — настроить фильтр уведомлений\n"
        "/search — найти прямо сейчас\n\n"
        "🏠 *Продаёшь или сдаёшь?*\n"
        "/post — разместить объявление бесплатно\n"
        "/mylistings — мои объявления\n\n"
        "📊 /stats — статистика базы",
        parse_mode="Markdown",
    )


# ── Подписка покупателя ───────────────────────────────────────────────────

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await guard_banned(update): return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text("📍 Выбери район:", reply_markup=kb_dist)
    return S_DIST

async def s_dist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["district"] = None if update.message.text == "Любой" else update.message.text
    await update.message.reply_text("Минимум комнат:", reply_markup=kb_rooms)
    return S_RMIN

async def s_rmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.replace("+","")
    if not t.isdigit(): await update.message.reply_text("Введи цифру:"); return S_RMIN
    ctx.user_data["rooms_min"] = int(t)
    await update.message.reply_text("Максимум комнат:", reply_markup=kb_rooms)
    return S_RMAX

async def s_rmax(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.replace("+","")
    if not t.isdigit(): await update.message.reply_text("Введи цифру:"); return S_RMAX
    ctx.user_data["rooms_max"] = int(t)
    await update.message.reply_text("Мин. цена USD (0 = без ограничений):", reply_markup=ReplyKeyboardRemove())
    return S_PMIN

async def s_pmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.replace(" ","")
    if not t.isdigit(): await update.message.reply_text("Только цифры:"); return S_PMIN
    ctx.user_data["price_min"] = int(t)
    await update.message.reply_text("Макс. цена USD:")
    return S_PMAX

async def s_pmax(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.replace(" ","")
    if not t.isdigit(): await update.message.reply_text("Только цифры:"); return S_PMAX
    ctx.user_data["price_max"] = int(t)
    sub_id = add_subscription(update.effective_user.id, ctx.user_data)
    dl = ctx.user_data.get("district") or "Любой"
    await update.message.reply_text(
        f"✅ *Подписка #{sub_id} создана!*\n"
        f"📍 {dl} · 🚪 {ctx.user_data['rooms_min']}–{ctx.user_data['rooms_max']} · "
        f"💰 ${ctx.user_data['price_min']:,}–${ctx.user_data['price_max']:,}\n\n"
        "Пришлю уведомление как только появится подходящая квартира от собственника.",
        parse_mode="Markdown",
    )
    ctx.user_data.clear()
    return ConversationHandler.END

async def cmd_mysubs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = get_user_subscriptions(update.effective_user.id)
    if not subs:
        await update.message.reply_text("Нет подписок. /subscribe — создать"); return
    for s in subs:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Удалить", callback_data=f"del_sub:{s['id']}")]])
        await update.message.reply_text(
            f"📌 *#{s['id']}* · {s['district'] or 'Любой'} · "
            f"{s['rooms_min']}–{s['rooms_max']} комн. · "
            f"${s['price_min']:,}–${s['price_max']:,}",
            parse_mode="Markdown", reply_markup=kb,
        )

async def cb_del_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sub_id = int(q.data.split(":")[1])
    deactivate_subscription(sub_id, q.from_user.id)
    await q.edit_message_text(f"✅ Подписка #{sub_id} удалена.")


# ── /search ───────────────────────────────────────────────────────────────

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE is_owner=1 ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await update.message.reply_text("База пуста. /post — размести квартиру.")
        return
    await update.message.reply_text(f"🏠 {len(rows)} свежих объявлений от собственников:")
    for row in rows:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ Это риелтор", callback_data=f"complaint:{row['id']}")
        ]])
        await update.message.reply_text(
            fmt_listing(row, show_source=True),
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        await asyncio.sleep(0.3)


# ── Жалобы ────────────────────────────────────────────────────────────────

async def cb_complaint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    listing_id = int(q.data.split(":")[1])
    msg = await handle_complaint(listing_id, q.from_user.id)
    await q.answer(msg, show_alert=True)


# ── /post — публикация от собственника ───────────────────────────────────

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await guard_banned(update): return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["amenities"] = []
    ctx.user_data["photos"]    = []
    ctx.user_data["uid"]       = update.effective_user.id
    ctx.user_data["uname"]     = update.effective_user.username or ""
    await update.message.reply_text(
        "🏠 *Разместить объявление от собственника*\n\n"
        "Без посредников — напрямую покупателям.\n\n"
        "Тип сделки:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["🏷 Продажа","🔑 Аренда"]],
                                          resize_keyboard=True, one_time_keyboard=True),
    )
    return P_DEAL

async def p_deal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["deal"] = "Продажа" if "Продажа" in update.message.text else "Аренда"
    await update.message.reply_text("Комнат:", reply_markup=kb_rooms)
    return P_ROOMS

async def p_rooms(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["rooms"] = int(update.message.text.replace("+","")) if update.message.text.replace("+","").isdigit() else 1
    await update.message.reply_text("Район:", reply_markup=kb_dist)
    return P_DIST2

async def p_dist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["district"] = update.message.text
    await update.message.reply_text("Этаж / всего этажей? Например: *5/9*",
                                     parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return P_FLOOR

async def p_floor(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["floor"] = update.message.text
    await update.message.reply_text("Площадь кв.м? Например: 65")
    return P_AREA

async def p_area(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        ctx.user_data["area"] = float(update.message.text.replace(",","."))
    except ValueError:
        await update.message.reply_text("Введи число:"); return P_AREA
    await update.message.reply_text("Состояние:", reply_markup=kb_cond)
    return P_COND

async def p_cond(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["condition"] = re.sub(r"^[^\s]+\s","",update.message.text)
    await update.message.reply_text("Цена в USD (только цифры):", reply_markup=ReplyKeyboardRemove())
    return P_PRICE

async def p_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.replace(" ","").replace(",","")
    if not raw.isdigit(): await update.message.reply_text("Только цифры:"); return P_PRICE
    ctx.user_data["price"] = int(raw)
    await update.message.reply_text("Удобства (выбирай по одному, потом «Готово»):", reply_markup=kb_amen)
    return P_AMEN

async def p_amen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if "Готово" in update.message.text:
        await update.message.reply_text(
            "Расскажи о квартире своими словами (2–5 предложений):\n"
            "Как давно живёшь, соседи, рядом что есть, почему продаёшь.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return P_DESC
    amenity = re.sub(r"^[^\s]+\s","",update.message.text)
    if amenity not in ctx.user_data["amenities"]:
        ctx.user_data["amenities"].append(amenity)
    sel = ", ".join(ctx.user_data["amenities"])
    await update.message.reply_text(f"✅ {sel}\n\nДобавь ещё или «Готово»", reply_markup=kb_amen)
    return P_AMEN

async def p_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["desc"] = update.message.text
    await update.message.reply_text("Номер телефона для покупателей:")
    return P_PHONE

async def p_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["phone"] = update.message.text
    ctx.user_data["phone_norm"] = normalize_phone(update.message.text)
    await update.message.reply_text(
        "Отправь фото квартиры (до 5 штук).\n"
        "Объявления с фото получают в 3× больше откликов.\n\n"
        "Или напиши «без фото» чтобы пропустить.",
        reply_markup=ReplyKeyboardMarkup([["➡️ Без фото"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return P_PHOTO

async def p_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text and ("без фото" in update.message.text.lower() or "Без" in update.message.text):
        return await _show_preview(update, ctx)
    if update.message.photo:
        fid = update.message.photo[-1].file_id
        ctx.user_data["photos"].append(fid)
        count = len(ctx.user_data["photos"])
        if count >= 5:
            return await _show_preview(update, ctx)
        await update.message.reply_text(
            f"📷 {count}/5 фото добавлено. Ещё или «Готово»",
            reply_markup=ReplyKeyboardMarkup([["✅ Достаточно"]], resize_keyboard=True, one_time_keyboard=True),
        )
        return P_PHOTO
    if update.message.text and "Достаточно" in update.message.text:
        return await _show_preview(update, ctx)
    await update.message.reply_text("Отправь фото или выбери «Без фото»")
    return P_PHOTO

async def _show_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    d = ctx.user_data
    amen = ", ".join(d.get("amenities",[])) or "не указаны"
    photo_count = len(d.get("photos",[]))
    await update.message.reply_text(
        f"👀 *Предпросмотр:*\n\n"
        f"🏷 {d['deal']} · {d['rooms']}-комн.\n"
        f"📍 {d['district']}, этаж {d['floor']}, {d['area']} кв.м\n"
        f"🔨 {d['condition']}\n"
        f"✨ {amen}\n"
        f"💰 ${d['price']:,}\n"
        f"📞 {d['phone']}\n"
        f"📷 Фото: {photo_count}\n\n"
        f"📝 {d['desc']}\n\n"
        "Всё верно?",
        parse_mode="Markdown", reply_markup=kb_confirm,
    )
    return P_CONFIRM

async def p_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Заново" in text or "Отмена" in text:
        ctx.user_data.clear()
        await update.message.reply_text("Отменено. /post — начать заново.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    await update.message.reply_text("⏳ Проверяем объявление...", reply_markup=ReplyKeyboardRemove())

    d = ctx.user_data
    uid = d["uid"]
    photo_ids = d.get("photos", [])

    # Вычисляем pHash для фото
    photo_hashes = []
    if photo_ids:
        await update.message.reply_text("📷 Анализируем фото...")
        tasks = [phash_from_telegram_file(update.get_bot(), fid) for fid in photo_ids]
        results = await asyncio.gather(*tasks)
        photo_hashes = [h for h in results if h]

    # Полная проверка всех слоёв защиты
    check = await run_full_check(
        user_id=uid,
        phone=d["phone"],
        phone_norm=d["phone_norm"],
        description=d["desc"],
        photo_hashes=photo_hashes,
    )

    # Жёсткий блок
    if not check.passed:
        await update.message.reply_text(
            f"⛔ *Объявление не прошло проверку*\n\n{check.block_reason}",
            parse_mode="Markdown",
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    # Сохраняем в БД
    listing_data = {
        "source":      "owner_direct",
        "external_id": f"tg_{uid}_{int(datetime.utcnow().timestamp())}",
        "url":         f"https://t.me/{d['uname']}" if d["uname"] else "",
        "title":       f"{d['rooms']}-комн. квартира, {d['district']} район",
        "description": d["desc"],
        "price_usd":   d["price"],
        "district":    d["district"],
        "rooms":       d["rooms"],
        "area_sqm":    d["area"],
        "floor":       d["floor"],
        "phone":       d["phone"],
        "phone_norm":  d["phone_norm"],
    }
    lid = save_listing(listing_data)

    if not lid:
        await update.message.reply_text("❌ Ошибка сохранения. Попробуй /post позже.")
        ctx.user_data.clear()
        return ConversationHandler.END

    # Регистрируем телефон и фото-хэши
    register_phone(d["phone_norm"], lid, uid)
    if photo_hashes:
        register_photo_hashes(lid, photo_hashes, "owner_direct")

    # В модерацию или публикуем
    if check.send_to_mod:
        pull_to_modqueue(lid, check.warn_reason)
        update_classification(lid, False, check.claude_score, check.claude_reason, photo_hashes)
        await update.message.reply_text(
            f"⏳ *Объявление отправлено на проверку*\n\n"
            f"{check.warn_reason}\n\n"
            "Обычно проверка занимает несколько часов. "
            "После одобрения покупатели получат уведомления.",
            parse_mode="Markdown",
        )
    else:
        update_classification(lid, True, check.claude_score, check.claude_reason, photo_hashes)
        await update.message.reply_text(
            f"✅ *Объявление опубликовано! #{lid}*\n\n"
            f"Квартира добавлена в базу. Покупатели с подходящим фильтром "
            f"получат уведомление.\n\n"
            f"📞 Звонки на: {d['phone']}\n"
            f"Управление: /mylistings",
            parse_mode="Markdown",
        )

    ctx.user_data.clear()
    return ConversationHandler.END


# ── /mylistings ───────────────────────────────────────────────────────────

async def cmd_mylistings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE external_id LIKE 'tg_'||?||'_%' ORDER BY created_at DESC LIMIT 10",
            (uid,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("Нет объявлений. /post — разместить"); return
    for row in rows:
        status = "✅ Опубликовано" if row["is_owner"] == 1 else ("⏳ На проверке" if row["is_owner"] is None else "❌ Отклонено")
        with get_conn() as conn:
            notif = conn.execute("SELECT COUNT(*) FROM notifications WHERE listing_id=?", (row["id"],)).fetchone()[0]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Снять", callback_data=f"del_listing:{row['id']}"),
            InlineKeyboardButton(f"👁 {notif} просмотров", callback_data=f"noop"),
        ]])
        price = f"${row['price_usd']:,}" if row["price_usd"] else "—"
        await update.message.reply_text(
            f"*{row['title']}*\n💰 {price} · {status}\n📅 {row['created_at'][:10]}",
            parse_mode="Markdown", reply_markup=kb,
        )

async def cb_del_listing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    lid = int(q.data.split(":")[1])
    with get_conn() as conn:
        conn.execute("UPDATE listings SET is_owner=0, owner_reason='Снято собственником' WHERE id=?", (lid,))
    await q.edit_message_text(f"✅ Объявление #{lid} снято.")

async def cb_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ── /modqueue — модерация (только для админов) ────────────────────────────

async def cmd_modqueue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет доступа."); return

    rows = get_modqueue()
    if not rows:
        await update.message.reply_text("✅ Очередь модерации пуста."); return

    await update.message.reply_text(f"📋 В очереди: {len(rows)} объявлений")
    for row in rows[:5]:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"mod_ok:{row['listing_id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"mod_no:{row['listing_id']}"),
        ]])
        price = f"${row['price_usd']:,}" if row["price_usd"] else "—"
        score = int((row["owner_score"] or 0) * 100)
        await update.message.reply_text(
            f"🔍 *Объявление #{row['listing_id']}*\n"
            f"Причина проверки: {row['reason']}\n\n"
            f"*{row['title']}*\n"
            f"📍 {row['district'] or '—'} · 💰 {price}\n"
            f"📞 {row['phone'] or '—'}\n"
            f"Claude: {score}% — {row['owner_reason'] or '—'}\n\n"
            f"📝 {(row['description'] or '')[:300]}{'...' if len(row['description'] or '')>300 else ''}\n\n"
            + (f"🔗 {row['url']}" if row["url"] else ""),
            parse_mode="Markdown", reply_markup=kb,
        )

async def cb_mod_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS: await q.answer("Нет доступа"); return
    await q.answer()
    lid = int(q.data.split(":")[1])
    approve_listing(lid)
    await q.edit_message_text(f"✅ Объявление #{lid} одобрено и опубликовано.")

async def cb_mod_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS: await q.answer("Нет доступа"); return
    await q.answer()
    lid = int(q.data.split(":")[1])
    reject_listing(lid)
    await q.edit_message_text(f"❌ Объявление #{lid} отклонено.")


# ── /stats ────────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_stats()
    await update.message.reply_text(
        f"📊 *Статистика базы*\n\n"
        f"🏠 Всего объявлений: {s['total']}\n"
        f"✅ Собственники: {s['owners']}\n"
        f"   📝 Прямая подача: {s['direct']}\n"
        f"   🌐 Парсинг: {s['owners']-s['direct']}\n"
        f"❌ Риелторы: {s['realtors']}\n"
        f"⏳ На модерации: {s['modqueue']}\n\n"
        f"📸 Фото-хэшей в базе: {s['photo_hashes']}\n"
        f"⚠️ Всего жалоб: {s['complaints']}\n\n"
        f"👤 Пользователей: {s['users']}\n"
        f"🔔 Подписок: {s['subscriptions']}",
        parse_mode="Markdown",
    )


# ── Уведомления и планировщик ─────────────────────────────────────────────

async def notify_subscribers(app: Application):
    listings = get_new_owner_listings()
    if not listings: return
    subs = get_active_subscriptions()
    sent = 0
    for lst in listings:
        notified_anyone = False
        for sub in subs:
            uid = sub["user_id"]
            if already_notified(uid, lst["id"]) or not listing_matches_sub(lst, sub):
                continue
            try:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚠️ Это риелтор", callback_data=f"complaint:{lst['id']}")
                ]])
                await app.bot.send_message(
                    chat_id=uid,
                    text="🔔 *Новая квартира от собственника!*\n\n" + fmt_listing(lst, True),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=kb,
                )
                record_notification(uid, lst["id"])
                sent += 1; notified_anyone = True
                await asyncio.sleep(0.1)
            except (Forbidden, BadRequest): pass
        if notified_anyone: mark_notified(lst["id"])
    if sent: logger.info(f"Уведомлений отправлено: {sent}")

async def scheduled_job(app: Application):
    while True:
        try:
            await run_pipeline(pages=2)
            await notify_subscribers(app)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(3600)


# ── Сборка приложения ─────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    sub_conv = ConversationHandler(
        entry_points=[CommandHandler("subscribe", cmd_subscribe)],
        states={
            S_DIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_dist)],
            S_RMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_rmin)],
            S_RMAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_rmax)],
            S_PMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_pmin)],
            S_PMAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_pmax)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
    )

    post_conv = ConversationHandler(
        entry_points=[CommandHandler("post", cmd_post)],
        states={
            P_DEAL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, p_deal)],
            P_ROOMS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, p_rooms)],
            P_DIST2:   [MessageHandler(filters.TEXT & ~filters.COMMAND, p_dist)],
            P_FLOOR:   [MessageHandler(filters.TEXT & ~filters.COMMAND, p_floor)],
            P_AREA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, p_area)],
            P_COND:    [MessageHandler(filters.TEXT & ~filters.COMMAND, p_cond)],
            P_PRICE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, p_price)],
            P_AMEN:    [MessageHandler(filters.TEXT & ~filters.COMMAND, p_amen)],
            P_DESC:    [MessageHandler(filters.TEXT & ~filters.COMMAND, p_desc)],
            P_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, p_phone)],
            P_PHOTO:   [
                MessageHandler(filters.PHOTO, p_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, p_photo),
            ],
            P_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, p_confirm)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
    )

    app.add_handler(post_conv)
    app.add_handler(sub_conv)
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_start))
    app.add_handler(CommandHandler("search",      cmd_search))
    app.add_handler(CommandHandler("mysubs",      cmd_mysubs))
    app.add_handler(CommandHandler("mylistings",  cmd_mylistings))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("modqueue",    cmd_modqueue))
    app.add_handler(CallbackQueryHandler(cb_del_sub,      pattern=r"^del_sub:"))
    app.add_handler(CallbackQueryHandler(cb_del_listing,  pattern=r"^del_listing:"))
    app.add_handler(CallbackQueryHandler(cb_complaint,    pattern=r"^complaint:"))
    app.add_handler(CallbackQueryHandler(cb_mod_ok,       pattern=r"^mod_ok:"))
    app.add_handler(CallbackQueryHandler(cb_mod_no,       pattern=r"^mod_no:"))
    app.add_handler(CallbackQueryHandler(cb_noop,         pattern=r"^noop"))

    async def post_init(application: Application):
        asyncio.create_task(scheduled_job(application))
    app.post_init = post_init

    logger.info("🚀 Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
