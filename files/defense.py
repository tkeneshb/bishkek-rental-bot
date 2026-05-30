"""
defense.py — многоуровневая защита от риелторов

Слои:
  1. Claude анализирует текст объявления
  2. Один телефон = одно объявление
  3. Один аккаунт = максимум 2 активных объявления
  4. Кулдаун 24ч между объявлениями
  5. pHash проверка фото на дубли
  6. Кнопка «Это риелтор» у покупателей — 3+ жалобы → в очередь
  7. Ручная очередь /modqueue для спорных случаев
"""

import os
import re
import json
import logging
import asyncio
from datetime import datetime, timedelta

import anthropic
from database import (
    check_phone_duplicate, get_user_listing_count,
    add_complaint, pull_to_modqueue, get_conn,
    find_similar_photos, hamming_distance,
)

logger = logging.getLogger(__name__)
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MAX_LISTINGS_PER_USER   = 2      # макс активных объявлений с одного аккаунта
COOLDOWN_HOURS          = 24     # минимальный интервал между объявлениями
COMPLAINT_THRESHOLD     = 3      # жалоб до отправки в модерацию
PHOTO_HASH_THRESHOLD    = 8      # расстояние Хэмминга для «одинаковых» фото


# ── Нормализация телефона ─────────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    """Приводит номер к виду 996XXXXXXXXX для сравнения."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0") and len(digits) == 10:
        digits = "996" + digits[1:]
    elif digits.startswith("7") and len(digits) == 11:
        digits = "996" + digits[3:]  # 7 XXX → 996 XXX (для КЗ-номеров)
    elif digits.startswith("8") and len(digits) == 11:
        digits = "996" + digits[3:]
    return digits


# ── Слой 1: Claude классификатор ──────────────────────────────────────────

CLASSIFY_PROMPT = """Ты — эксперт по рынку недвижимости Кыргызстана.

Определи: объявление написано СОБСТВЕННИКОМ или РИЕЛТОРОМ/АГЕНТСТВОМ.

МАРКЕРЫ РИЕЛТОРА:
- Слова: агентство, агент, риелтор, брокер, наш клиент, реализуем, объект
- «без комиссии», «юридическое сопровождение», «наша база»
- Профессиональный шаблонный маркетинговый текст
- Нет личных деталей (имя, история, соседи, причина продажи)
- Несколько объявлений с одного контакта

МАРКЕРЫ СОБСТВЕННИКА:
- Личный текст от первого лица: «продаю свою», «живём здесь», «переезжаем»
- Конкретные детали: соседи, этаж, вид из окна, история ремонта
- «без посредников», «хозяин», «хозяйка»
- Простой неполированный текст, иногда с опечатками

ОБЪЯВЛЕНИЕ:
Заголовок: {title}
Описание: {description}
Телефон: {phone}

Отвечай ТОЛЬКО JSON без markdown:
{{"is_owner": true/false, "confidence": 0.0-1.0, "reason": "1-2 предложения на русском"}}"""


def classify_text(listing: dict) -> dict:
    """Claude анализирует текст → собственник или риелтор."""
    prompt = CLASSIFY_PROMPT.format(
        title=str(listing.get("title",""))[:300],
        description=str(listing.get("description",""))[:800],
        phone=str(listing.get("phone","нет")),
    )
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role":"user","content":prompt}],
        )
        text = resp.content[0].text.strip().strip("```json").strip("```").strip()
        result = json.loads(text)
        return {
            "is_owner":   bool(result.get("is_owner", False)),
            "confidence": float(result.get("confidence", 0.5)),
            "reason":     str(result.get("reason", "")),
        }
    except Exception as e:
        logger.error(f"Claude classify error: {e}")
        return {"is_owner": False, "confidence": 0.0, "reason": "ошибка классификации"}


# ── Слой 2–4: Поведенческие проверки (для прямой подачи) ─────────────────

class DefenseResult:
    """Результат проверки объявления от прямой подачи собственника."""
    def __init__(self):
        self.passed         = True
        self.block_reason   = ""     # жёсткий блок
        self.warn_reason    = ""     # предупреждение (в очередь модерации)
        self.phone_dup_lid  = None   # id объявления с тем же телефоном
        self.photo_dup_lid  = None   # id объявления с теми же фото
        self.photo_distance = 999
        self.claude_score   = 0.0
        self.claude_reason  = ""
        self.send_to_mod    = False  # True = отправить в modqueue, не публиковать


def check_user_limits(user_id: int) -> DefenseResult:
    """Слой 2: лимит объявлений на аккаунт."""
    r = DefenseResult()
    count = get_user_listing_count(user_id)
    if count >= MAX_LISTINGS_PER_USER:
        r.passed = False
        r.block_reason = (
            f"У тебя уже {count} активных объявлений. "
            f"Максимум {MAX_LISTINGS_PER_USER} на один аккаунт. "
            f"Сними старое объявление через /mylistings чтобы добавить новое."
        )
    return r


def check_cooldown(user_id: int) -> DefenseResult:
    """Слой 3: кулдаун 24 часа между объявлениями."""
    r = DefenseResult()
    with get_conn() as conn:
        last = conn.execute(
            """SELECT created_at FROM listings
               WHERE source='owner_direct'
               AND external_id LIKE 'tg_'||?||'_%'
               ORDER BY created_at DESC LIMIT 1""",
            (user_id,)
        ).fetchone()
    if last:
        last_dt = datetime.fromisoformat(last["created_at"])
        delta = datetime.utcnow() - last_dt
        if delta < timedelta(hours=COOLDOWN_HOURS):
            remaining = COOLDOWN_HOURS - int(delta.total_seconds() / 3600)
            r.passed = False
            r.block_reason = (
                f"Слишком часто. Следующее объявление можно разместить "
                f"через {remaining} ч. Это защита от спама."
            )
    return r


def check_phone(phone_norm: str) -> DefenseResult:
    """Слой 4: один телефон — одно объявление."""
    r = DefenseResult()
    if not phone_norm:
        return r
    existing_lid = check_phone_duplicate(phone_norm)
    if existing_lid:
        r.phone_dup_lid = existing_lid
        r.warn_reason = (
            f"Этот номер телефона уже используется в объявлении #{existing_lid}. "
            "Если это ты — проверь /mylistings. Если нет — возможно, кто-то "
            "использует твой номер. Объявление отправлено на проверку."
        )
        r.send_to_mod = True
    return r


def check_photo_duplicates(photo_hashes: list[str]) -> DefenseResult:
    """Слой 5: pHash проверка фото."""
    r = DefenseResult()
    if not photo_hashes:
        return r
    matches = find_similar_photos(photo_hashes, threshold=PHOTO_HASH_THRESHOLD)
    if matches:
        best = min(matches, key=lambda m: m["distance"])
        r.photo_dup_lid = best["existing_listing_id"]
        r.photo_distance = best["distance"]
        if best["distance"] <= 3:
            # Почти идентичные фото — жёсткий блок
            r.passed = False
            r.block_reason = (
                f"Фотографии уже используются в объявлении #{best['existing_listing_id']}. "
                "Использование чужих фото не допускается."
            )
        else:
            # Похожие фото — в очередь модерации
            r.warn_reason = (
                f"Фото похожи на уже опубликованные (объявление #{best['existing_listing_id']}). "
                "Объявление отправлено на проверку."
            )
            r.send_to_mod = True
    return r


def check_claude_authenticity(description: str, phone: str = "") -> DefenseResult:
    """Слой 6: Claude проверяет подлинность текста собственника."""
    r = DefenseResult()
    result = classify_text({"title": "", "description": description, "phone": phone})
    r.claude_score = result["confidence"]
    r.claude_reason = result["reason"]

    if not result["is_owner"]:
        if result["confidence"] >= 0.85:
            # Уверены что риелтор — блокируем
            r.passed = False
            r.block_reason = (
                "Текст похож на объявление от агентства.\n"
                f"Причина: {result['reason']}\n\n"
                "Если ты реальный собственник — напиши описание своими словами: "
                "как давно живёшь в квартире, почему продаёшь, что рядом."
            )
        else:
            # Не уверены — в очередь
            r.send_to_mod = True
            r.warn_reason = f"Текст отправлен на проверку: {result['reason']}"
    return r


# ── Полная проверка для прямой подачи ────────────────────────────────────

async def run_full_check(user_id: int, phone: str, phone_norm: str,
                         description: str, photo_hashes: list[str]) -> DefenseResult:
    """
    Запускает все слои защиты последовательно.
    Возвращает первый жёсткий блок или объединённый результат.
    """
    # Слой 2: лимит объявлений
    r = check_user_limits(user_id)
    if not r.passed:
        return r

    # Слой 3: кулдаун
    r2 = check_cooldown(user_id)
    if not r2.passed:
        return r2

    # Слой 4: дубль телефона
    r3 = check_phone(phone_norm)
    if not r3.passed:
        return r3

    # Слой 5: дубль фото
    r4 = check_photo_duplicates(photo_hashes)
    if not r4.passed:
        return r4

    # Слой 6: Claude
    r5 = check_claude_authenticity(description, phone)
    if not r5.passed:
        return r5

    # Объединяем предупреждения
    final = DefenseResult()
    final.phone_dup_lid  = r3.phone_dup_lid
    final.photo_dup_lid  = r4.photo_dup_lid
    final.photo_distance = r4.photo_distance
    final.claude_score   = r5.claude_score
    final.claude_reason  = r5.claude_reason
    final.send_to_mod    = r3.send_to_mod or r4.send_to_mod or r5.send_to_mod
    if r3.warn_reason or r4.warn_reason or r5.warn_reason:
        final.warn_reason = " | ".join(filter(None, [
            r3.warn_reason, r4.warn_reason, r5.warn_reason
        ]))
    return final


# ── Слой 7: Жалобы покупателей ────────────────────────────────────────────

async def handle_complaint(listing_id: int, user_id: int) -> str:
    """
    Обрабатывает жалобу покупателя на объявление.
    При 3+ жалобах → автоматически в modqueue.
    """
    count = add_complaint(listing_id, user_id)
    if count >= COMPLAINT_THRESHOLD:
        pull_to_modqueue(
            listing_id,
            f"Автоматически: {count} жалоб от покупателей"
        )
        return f"Объявление #{listing_id} снято и отправлено на проверку ({count} жалоб)."
    remaining = COMPLAINT_THRESHOLD - count
    return f"Жалоба принята ({count}/{COMPLAINT_THRESHOLD}). Ещё {remaining} жалоб — объявление уйдёт на проверку."
