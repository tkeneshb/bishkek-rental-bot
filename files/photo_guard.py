"""
photo_guard.py — pHash (perceptual hash) реализация через Pillow
Та же логика что у House.kg: устойчив к сжатию, ресайзу, смене яркости.
Расстояние Хэмминга < 8 = фото считаются одинаковыми.
"""

import io
import math
import asyncio
import logging
import urllib.request
from PIL import Image

logger = logging.getLogger(__name__)

HASH_THRESHOLD = 8   # максимальное расстояние для "одинаковых" фото
HASH_SIZE = 8        # итоговый хэш 8×8 = 64 бит = 16 hex символов


def compute_phash(image: Image.Image) -> str:
    """
    Вычисляет perceptual hash изображения.

    Алгоритм:
    1. Уменьшаем до 32×32 в grayscale
    2. Применяем DCT (дискретное косинусное преобразование)
    3. Берём верхний левый угол 8×8 (низкие частоты)
    4. Сравниваем каждый пиксель со средним → битовый вектор
    5. Конвертируем в hex-строку

    Почему это работает: сжатие, ресайз, изменение яркости меняют
    высокие частоты, но не меняют структуру низких частот изображения.
    """
    DCT_SIZE = 32

    # 1. Уменьшаем и конвертируем в оттенки серого
    img = image.convert("L").resize((DCT_SIZE, DCT_SIZE), Image.LANCZOS)
    pixels = list(img.getdata())

    # 2. DCT по строкам и столбцам (упрощённая 2D DCT)
    matrix = [pixels[i*DCT_SIZE:(i+1)*DCT_SIZE] for i in range(DCT_SIZE)]
    dct = _dct2d(matrix, DCT_SIZE)

    # 3. Берём верхний левый угол HASH_SIZE × HASH_SIZE
    low_freq = []
    for row in range(HASH_SIZE):
        for col in range(HASH_SIZE):
            low_freq.append(dct[row][col])

    # 4. Среднее (без DC-компоненты [0][0])
    mean = sum(low_freq[1:]) / (len(low_freq) - 1)

    # 5. Битовый вектор → int → hex
    bits = 0
    for val in low_freq:
        bits = (bits << 1) | (1 if val > mean else 0)

    return format(bits, f'0{HASH_SIZE * HASH_SIZE // 4}x')


def _dct2d(matrix: list[list], size: int) -> list[list]:
    """2D DCT через два прохода 1D DCT."""
    # DCT по строкам
    result = [_dct1d(row, size) for row in matrix]
    # Транспонируем → DCT по столбцам → транспонируем обратно
    transposed = [[result[r][c] for r in range(size)] for c in range(size)]
    result2 = [_dct1d(row, size) for row in transposed]
    return [[result2[c][r] for c in range(size)] for r in range(size)]


def _dct1d(signal: list, n: int) -> list:
    """1D DCT-II."""
    out = []
    for k in range(n):
        s = sum(signal[j] * math.cos(math.pi * k * (2*j+1) / (2*n))
                for j in range(n))
        out.append(s)
    return out


def hamming_distance(h1: str, h2: str) -> int:
    """Расстояние Хэмминга между двумя hex pHash."""
    try:
        xor = int(h1, 16) ^ int(h2, 16)
        return bin(xor).count('1')
    except (ValueError, TypeError):
        return 999


def phash_from_bytes(data: bytes) -> str | None:
    """Вычисляет pHash из байтов изображения."""
    try:
        img = Image.open(io.BytesIO(data))
        return compute_phash(img)
    except Exception as e:
        logger.debug(f"pHash error: {e}")
        return None


async def phash_from_url(url: str) -> str | None:
    """Скачивает изображение и вычисляет pHash."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        return phash_from_bytes(data)
    except Exception as e:
        logger.debug(f"Photo download error {url}: {e}")
        return None


async def phash_from_telegram_file(bot, file_id: str) -> str | None:
    """Скачивает фото из Telegram и вычисляет pHash."""
    try:
        tg_file = await bot.get_file(file_id)
        data = await tg_file.download_as_bytearray()
        return phash_from_bytes(bytes(data))
    except Exception as e:
        logger.debug(f"Telegram photo error {file_id}: {e}")
        return None


# ── Проверка набора фото ──────────────────────────────────────────────────

class PhotoCheckResult:
    def __init__(self):
        self.hashes: list[str] = []
        self.duplicates: list[dict] = []
        self.is_duplicate: bool = False
        self.duplicate_listing_id: int | None = None
        self.min_distance: int = 999

    def __repr__(self):
        return (f"PhotoCheckResult(hashes={len(self.hashes)}, "
                f"is_duplicate={self.is_duplicate}, "
                f"min_dist={self.min_distance})")


async def check_photos_urls(photo_urls: list[str],
                            existing_hashes_db) -> PhotoCheckResult:
    """
    Скачивает фото по URL, вычисляет pHash, ищет дубли в БД.
    existing_hashes_db: функция find_similar_photos из database.py
    """
    result = PhotoCheckResult()
    if not photo_urls:
        return result

    # Параллельно скачиваем и хэшируем
    tasks = [phash_from_url(url) for url in photo_urls[:5]]
    hashes = await asyncio.gather(*tasks)
    result.hashes = [h for h in hashes if h]

    if not result.hashes:
        return result

    # Ищем дубли
    matches = existing_hashes_db(result.hashes, threshold=HASH_THRESHOLD)
    result.duplicates = matches

    if matches:
        result.is_duplicate = True
        result.min_distance = min(m["distance"] for m in matches)
        result.duplicate_listing_id = matches[0]["existing_listing_id"]

    return result


async def check_photos_telegram(photo_file_ids: list[str],
                                bot,
                                existing_hashes_db) -> PhotoCheckResult:
    """
    Проверяет фото загруженные в Telegram-бот.
    """
    result = PhotoCheckResult()
    if not photo_file_ids:
        return result

    tasks = [phash_from_telegram_file(bot, fid) for fid in photo_file_ids[:5]]
    hashes = await asyncio.gather(*tasks)
    result.hashes = [h for h in hashes if h]

    if not result.hashes:
        return result

    matches = existing_hashes_db(result.hashes, threshold=HASH_THRESHOLD)
    result.duplicates = matches

    if matches:
        result.is_duplicate = True
        result.min_distance = min(m["distance"] for m in matches)
        result.duplicate_listing_id = matches[0]["existing_listing_id"]

    return result
