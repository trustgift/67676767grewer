import asyncio
import html
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    AuthKeyUnregisteredError,
)
from telethon.tl import functions
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import PeerUser


# ============================================================
# CONFIG
# ============================================================

API_ID = 20734425
API_HASH = "f72fa8d1d63a8f984e47a115c76df123"
BOT_TOKEN = "8603082526:AAE9w4B7AG6C_-OkAXcDAXNW7mRibLa6N4c"
ADMIN_ID = 1602809044

SESSION_NAME = "telethon_market_userbot"
TARGET_CHAT_ID = -1004314339535

# Скорость работы (безопасные интервалы)
MONITOR_INTERVAL = 10     # Пауза между кругами (было 30)
MONITOR_PAGE_LIMIT = 2    # Сканировать первые 2 страницы (было 5)
MONITOR_API_DELAY = 1.0   # Задержка запросов (было 2.2)
MONITOR_SEND_DELAY = 0.35 # Отправка в чат (было 1.0)      # Задержка между отправкой сообщений (сек)

# Фильтры по умолчанию
DEFAULT_MAX_NFT_GIFTS = 5       # Максимум NFT-подарков в профиле
DEFAULT_MAX_RATING = 10         # Порог рейтинга (парсим только тех, у кого уровень строго < 10)
DEFAULT_MIN_PRICE = 0
DEFAULT_MAX_PRICE = 0
DEFAULT_USERNAME_MODE = "any"

SEND_EXISTING_ON_FIRST_SCAN = False

GIFTS_PER_PAGE = 8
REQUEST_PAGE_LIMIT = 20
MAX_MANUAL_PAGES = 15

OWNER_BLACKLIST_FILE = "owner_blacklist.json"
OWNER_CACHE_FILE = "owner_cache.json"
SENT_GIFTS_FILE = "sent_gifts.json"
MONITOR_SETTINGS_FILE = "monitor_settings.json"
TOPICS_CACHE_FILE = "topics_cache.json"


# ============================================================
# LOGGING / GLOBALS
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("gift-parser")


def validate_config():
    errors = []
    if not isinstance(API_ID, int) or API_ID <= 0:
        errors.append("API_ID")
    if not API_HASH or API_HASH.startswith("ВСТАВЬ_"):
        errors.append("API_HASH")
    if not BOT_TOKEN or BOT_TOKEN.startswith("ВСТАВЬ_"):
        errors.append("BOT_TOKEN")
    if not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        errors.append("ADMIN_ID")
    if errors:
        raise SystemExit(
            "\nНе заполнены настройки: " + ", ".join(errors) +
            "\nЗаполни блок CONFIG в верхней части скрипта."
        )


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

BASE_GIFTS: List["BaseGift"] = []
BASE_GIFTS_BY_ID: Dict[int, "BaseGift"] = {}

OWNERS_BLACKLIST: Dict[str, str] = {}
OWNER_CACHE: Dict[str, Dict[str, Any]] = {}
SENT_GIFTS: Dict[str, Dict[str, Any]] = {}
TOPICS_CACHE: Dict[str, int] = {}

topic_creation_lock = asyncio.Lock()

MONITOR_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "target_chat_id": TARGET_CHAT_ID,
    "min_price": DEFAULT_MIN_PRICE,
    "max_price": DEFAULT_MAX_PRICE,
    "username_mode": DEFAULT_USERNAME_MODE,
    "model_ids": [],
    "max_owner_nft_gifts": DEFAULT_MAX_NFT_GIFTS,
    "max_rating_level": DEFAULT_MAX_RATING,
}

monitor_task: Optional[asyncio.Task] = None
monitor_running = False
monitor_first_scan_done = False
monitor_lock = asyncio.Lock()
market_api_lock = asyncio.Lock()
last_market_api_request = 0.0

USER_SELECTED_GIFT: Dict[int, int] = {}
USER_SEARCH_HISTORY: Dict[int, Dict[str, Any]] = {}
USER_LAST_RESULTS: Dict[int, List["MarketGift"]] = {}


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class BaseGift:
    gift_id: int
    title: str
    stars: int
    availability_resale: int
    resell_min_stars: int
    sold_out: bool


@dataclass
class OwnerInfo:
    key: str
    label: str
    username: Optional[str] = None
    user_id: Optional[int] = None
    nft_gifts_count: int = 0
    total_gifts_count: int = 0
    rating_level: int = 0
    rating_stars: int = 0

    @property
    def display(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.label


@dataclass
class MarketGift:
    title: str
    num: int
    slug: str
    price: int
    owner: OwnerInfo

    @property
    def link(self) -> str:
        return f"https://t.me/nft/{self.slug}"


# ============================================================
# FSM STATES
# ============================================================

class AuthState(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class MonitorPriceState(StatesGroup):
    waiting_range = State()


class MonitorRatingState(StatesGroup):
    waiting_rating = State()


class MonitorNftLimitState(StatesGroup):
    waiting_limit = State()


class BlacklistState(StatesGroup):
    waiting_value = State()


# ============================================================
# JSON STORAGE
# ============================================================

def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.error("Save JSON error %s: %s", path, e)


def load_owner_blacklist() -> Dict[str, str]:
    data = load_json(OWNER_BLACKLIST_FILE, {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_owner_blacklist() -> None:
    save_json(OWNER_BLACKLIST_FILE, OWNERS_BLACKLIST)


def load_owner_cache() -> Dict[str, Dict[str, Any]]:
    data = load_json(OWNER_CACHE_FILE, {})
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save_owner_cache() -> None:
    save_json(OWNER_CACHE_FILE, OWNER_CACHE)


def load_sent_gifts() -> Dict[str, Dict[str, Any]]:
    data = load_json(SENT_GIFTS_FILE, {})
    return {str(k): v if isinstance(v, dict) else {} for k, v in data.items()} if isinstance(data, dict) else {}


def save_sent_gifts() -> None:
    if len(SENT_GIFTS) > 5000:
        keys_to_remove = list(SENT_GIFTS.keys())[:-4000]
        for k in keys_to_remove:
            SENT_GIFTS.pop(k, None)
    save_json(SENT_GIFTS_FILE, SENT_GIFTS)


def load_topics() -> Dict[str, int]:
    data = load_json(TOPICS_CACHE_FILE, {})
    return {str(k): safe_int(v) for k, v in data.items() if safe_int(v) > 0} if isinstance(data, dict) else {}


def save_topics() -> None:
    save_json(TOPICS_CACHE_FILE, TOPICS_CACHE)


def load_monitor_settings() -> None:
    global MONITOR_SETTINGS

    data = load_json(MONITOR_SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}

    MONITOR_SETTINGS["enabled"] = bool(data.get("enabled", False))
    MONITOR_SETTINGS["target_chat_id"] = int(data.get("target_chat_id", TARGET_CHAT_ID) or 0)
    MONITOR_SETTINGS["min_price"] = max(0, int(data.get("min_price", DEFAULT_MIN_PRICE) or 0))
    MONITOR_SETTINGS["max_price"] = max(0, int(data.get("max_price", DEFAULT_MAX_PRICE) or 0))
    MONITOR_SETTINGS["max_owner_nft_gifts"] = max(0, int(data.get("max_owner_nft_gifts", DEFAULT_MAX_NFT_GIFTS) or 0))
    MONITOR_SETTINGS["max_rating_level"] = max(0, int(data.get("max_rating_level", DEFAULT_MAX_RATING) or 0))

    username_mode = str(data.get("username_mode", DEFAULT_USERNAME_MODE)).lower()
    MONITOR_SETTINGS["username_mode"] = username_mode if username_mode in {"any", "required"} else "any"

    model_ids = data.get("model_ids", [])
    if isinstance(model_ids, list):
        MONITOR_SETTINGS["model_ids"] = [int(x) for x in model_ids if str(x).lstrip("-").isdigit()]
    else:
        MONITOR_SETTINGS["model_ids"] = []

    if not MONITOR_SETTINGS["model_ids"]:
        MONITOR_SETTINGS["enabled"] = False


def save_monitor_settings() -> None:
    save_json(MONITOR_SETTINGS_FILE, MONITOR_SETTINGS)


# ============================================================
# HELPERS
# ============================================================

def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and user_id == ADMIN_ID


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except Exception:
        return default


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_owner_id(raw_id: Any) -> Optional[int]:
    if raw_id is None:
        return None
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, PeerUser):
        return raw_id.user_id
    if hasattr(raw_id, "user_id"):
        return safe_int(getattr(raw_id, "user_id"), 0) or None
    try:
        return int(raw_id)
    except Exception:
        return None


def extract_stars_amount(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        for item in value:
            amount = extract_stars_amount(item)
            if amount:
                return amount
        return 0
    amount = get_field(value, "amount")
    if amount is not None:
        return safe_int(amount)
    stars = get_field(value, "stars")
    if stars is not None:
        return safe_int(stars)
    return 0


def owner_blacklist_keys(owner: OwnerInfo) -> Set[str]:
    keys: Set[str] = set()
    if owner.user_id:
        keys.add(f"id:{owner.user_id}")
    if owner.username:
        keys.add(f"username:{owner.username.lower()}")
    if owner.key:
        keys.add(owner.key)
    return keys


def is_owner_blacklisted(owner: OwnerInfo) -> bool:
    return any(key in OWNERS_BLACKLIST for key in owner_blacklist_keys(owner))


def blacklist_owner(owner: OwnerInfo) -> None:
    display = owner.display
    for key in owner_blacklist_keys(owner):
        OWNERS_BLACKLIST[key] = display
    save_owner_blacklist()


def get_target_chat_id() -> int:
    return safe_int(MONITOR_SETTINGS.get("target_chat_id"), 0)


def price_matches(price: int) -> bool:
    min_price = safe_int(MONITOR_SETTINGS.get("min_price"), 0)
    max_price = safe_int(MONITOR_SETTINGS.get("max_price"), 0)
    if min_price and price < min_price:
        return False
    if max_price and price > max_price:
        return False
    return True


def username_matches(owner: OwnerInfo) -> bool:
    mode = MONITOR_SETTINGS.get("username_mode", "any")
    return bool(owner.username) if mode == "required" else True


def escape_text(value: Any) -> str:
    return html.escape(str(value or ""))


# ============================================================
# TELETHON CLIENT & RATE LIMITER
# ============================================================

async def ensure_user_client_connected() -> None:
    if not user_client.is_connected():
        await user_client.connect()


async def is_user_client_authorized() -> bool:
    await ensure_user_client_connected()
    try:
        return await user_client.is_user_authorized()
    except (AuthKeyUnregisteredError, ValueError, ConnectionError) as e:
        log.warning("Сессия сброшена или не зарегистрирована: %s", e)
        return False


async def pace_market_api() -> None:
    global last_market_api_request
    async with market_api_lock:
        now = asyncio.get_running_loop().time()
        wait_for = MONITOR_API_DELAY - (now - last_market_api_request)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        last_market_api_request = asyncio.get_running_loop().time()


# ============================================================
# ТОЧНЫЙ ПОДСЧЁТ ТОЛЬКО NFT-ПОДАРКОВ
# ============================================================

def is_nft_gift(obj: Any) -> bool:
    """
    Проверяет, является ли подарок уникальным NFT-подарком.
    Поддерживает SavedStarGift, UserStarGift и StarGiftUnique.
    """
    gift = getattr(obj, "gift", None) or getattr(obj, "stargift", None) or obj

    # 1. Проверка по имени класса объекта и вложенного подарка
    for target in (obj, gift):
        cls_name = type(target).__name__.lower()
        if "unique" in cls_name:
            return True

    # 2. Проверка по атрибутам NFT (slug, num, gift_num)
    for target in (obj, gift):
        if getattr(target, "slug", None):
            return True
        num = getattr(target, "num", None)
        if num is not None and num != 0:
            return True
        gift_num = getattr(target, "gift_num", None)
        if gift_num is not None and gift_num != 0:
            return True

    return False


async def fetch_user_nft_gifts_count(user_id: int, max_to_check: int = 10) -> int:
    """
    Считает количество уникальных NFT-подарков на витрине пользователя,
    используя современный метод GetSavedStarGiftsRequest.
    """
    try:
        await pace_market_api()
        peer = await user_client.get_input_entity(user_id)
        raw_gifts = []

        # Актуальный метод Telegram API Layer 198+
        if hasattr(functions.payments, "GetSavedStarGiftsRequest"):
            try:
                res = await user_client(
                    functions.payments.GetSavedStarGiftsRequest(
                        peer=peer,
                        offset="",
                        limit=max_to_check + 5,
                        exclude_unlimited=True,  # Отсекает обычные звёздные подарки (торты, звёзды)
                    )
                )
                raw_gifts = getattr(res, "gifts", []) or []
            except TypeError:
                res = await user_client(
                    functions.payments.GetSavedStarGiftsRequest(
                        peer=peer,
                        offset="",
                        limit=30,
                    )
                )
                raw_gifts = getattr(res, "gifts", []) or []
        elif hasattr(functions.payments, "GetUserStarGiftsRequest"):
            res = await user_client(
                functions.payments.GetUserStarGiftsRequest(
                    user_id=peer,
                    offset="",
                    limit=30,
                )
            )
            raw_gifts = getattr(res, "gifts", []) or []

        # Считаем строго NFT
        nft_count = sum(1 for g in raw_gifts if is_nft_gift(g))
        return nft_count

    except FloodWaitError as e:
        wait_time = int(e.seconds) + 1
        log.warning("FloodWait при проверке NFT подарков: %s сек.", wait_time)
        await asyncio.sleep(wait_time)
        return 999
    except Exception as e:
        log.warning("Ошибка подсчёта NFT для %s: %s", user_id, e)
        return 0


async def resolve_owner_info(raw_gift: Any) -> OwnerInfo:
    owner_id = extract_owner_id(get_field(raw_gift, "owner_id"))
    owner_name = get_field(raw_gift, "owner_name")

    if owner_id:
        cache_key = str(owner_id)
        cached = OWNER_CACHE.get(cache_key)

        # Автоматическая очистка старого ошибочного кэша:
        # Если в кэше NFT=0, но подарки у юзера есть, то это была ошибка из-за отсутствия GetUserStarGiftsRequest
        cache_is_corrupted = (
            cached
            and cached.get("nft_gifts_count", 0) == 0
            and cached.get("total_gifts_count", 0) > 0
        )

        if cached and "nft_gifts_count" in cached and "rating_level" in cached and not cache_is_corrupted:
            username = cached.get("username") or None
            label = cached.get("label") or f"id:{owner_id}"
            key = f"username:{username.lower()}" if username else f"id:{owner_id}"
            return OwnerInfo(
                key=key,
                label=label,
                username=username,
                user_id=owner_id,
                nft_gifts_count=cached.get("nft_gifts_count", 0),
                total_gifts_count=cached.get("total_gifts_count", 0),
                rating_level=cached.get("rating_level", 0),
                rating_stars=cached.get("rating_stars", 0),
            )

        try:
            await ensure_user_client_connected()
            await pace_market_api()

            peer = await user_client.get_input_entity(owner_id)
            full = await user_client(GetFullUserRequest(id=peer))
            full_user = full.full_user

            user_obj = next((u for u in full.users if u.id == owner_id), None)
            username = getattr(user_obj, "username", None)
            first_name = getattr(user_obj, "first_name", "") or ""
            last_name = getattr(user_obj, "last_name", "") or ""
            name = f"{first_name} {last_name}".strip()

            label = f"@{username}" if username else (name or f"id:{owner_id}")
            key = f"username:{username.lower()}" if username else f"id:{owner_id}"

            total_gifts = getattr(full_user, "stargifts_count", None)
            if total_gifts is None:
                total_gifts = getattr(full_user, "gifts_count", 0)

            stars_rating = getattr(full_user, "stars_rating", None)
            rating_level = 0
            rating_stars = 0
            if stars_rating:
                rating_level = safe_int(getattr(stars_rating, "level", 0))
                rating_stars = safe_int(getattr(stars_rating, "stars", 0))

            max_limit = safe_int(MONITOR_SETTINGS.get("max_owner_nft_gifts"), DEFAULT_MAX_NFT_GIFTS)
            nft_gifts_count = await fetch_user_nft_gifts_count(owner_id, max_to_check=max_limit + 1)

            OWNER_CACHE[cache_key] = {
                "label": label,
                "username": username or "",
                "user_id": owner_id,
                "nft_gifts_count": nft_gifts_count,
                "total_gifts_count": total_gifts,
                "rating_level": rating_level,
                "rating_stars": rating_stars,
            }
            save_owner_cache()

            log.info("Профиль %s: всего подарков=%s, из них NFT=%s, рейтинг=%s", label, total_gifts, nft_gifts_count, rating_level)

            return OwnerInfo(
                key=key,
                label=label,
                username=username,
                user_id=owner_id,
                nft_gifts_count=nft_gifts_count,
                total_gifts_count=total_gifts,
                rating_level=rating_level,
                rating_stars=rating_stars,
            )

        except FloodWaitError as e:
            wait_time = int(e.seconds) + 1
            log.warning("FloodWait при запросе профиля %s: %s сек.", owner_id, wait_time)
            await asyncio.sleep(wait_time)
            return OwnerInfo(key=f"id:{owner_id}", label=f"id:{owner_id}", user_id=owner_id)

        except Exception as e:
            log.warning("Не удалось получить профиль %s: %s", owner_id, e)
            return OwnerInfo(key=f"id:{owner_id}", label=f"id:{owner_id}", user_id=owner_id)

    if owner_name:
        return OwnerInfo(
            key=f"name:{str(owner_name).lower()}",
            label=str(owner_name),
            username=None,
            user_id=None,
        )

    return OwnerInfo(key="unknown", label="не указан")


# ============================================================
# MARKETPLACE API
# ============================================================

async def get_resale_page(
    gift_id: int,
    offset: str = "",
    limit: int = REQUEST_PAGE_LIMIT,
    sort_by_price: bool = False,
):
    while True:
        try:
            await pace_market_api()
            try:
                return await user_client(
                    functions.payments.GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset=offset,
                        limit=limit,
                        sort_by_price=sort_by_price,
                        sort_by_num=False,
                        stars_only=True,
                        for_craft=False,
                    )
                )
            except TypeError as e:
                if "stars_only" in str(e) or "for_craft" in str(e):
                    return await user_client(
                        functions.payments.GetResaleStarGiftsRequest(
                            gift_id=gift_id,
                            offset=offset,
                            limit=limit,
                            sort_by_price=sort_by_price,
                            sort_by_num=False,
                        )
                    )
                raise

        except FloodWaitError as e:
            wait_time = int(e.seconds) + 1
            log.warning("FloodWait на запросе маркета: %s сек.", wait_time)
            await asyncio.sleep(wait_time)


async def find_market_gifts(
    gift_id: int,
    min_price: int,
    max_price: int,
    need: int = 10,
    skip_slugs: Optional[Set[str]] = None,
    max_pages: int = MAX_MANUAL_PAGES,
    sort_by_price: bool = True,
) -> List[MarketGift]:
    found: List[MarketGift] = []
    skip_slugs = skip_slugs or set()
    offset = ""
    pages = 0

    max_nft_gifts = safe_int(MONITOR_SETTINGS.get("max_owner_nft_gifts"), 0)
    max_rating = safe_int(MONITOR_SETTINGS.get("max_rating_level"), 0)

    while len(found) < need and pages < max_pages:
        pages += 1
        result = await get_resale_page(
            gift_id=gift_id,
            offset=offset,
            limit=REQUEST_PAGE_LIMIT,
            sort_by_price=sort_by_price,
        )

        raw_gifts = getattr(result, "gifts", []) or []
        if not raw_gifts:
            break

        for raw in raw_gifts:
            slug = get_field(raw, "slug")
            if not slug or str(slug) in skip_slugs:
                continue

            slug = str(slug)
            price = extract_stars_amount(get_field(raw, "resell_amount"))
            if price < min_price or (max_price and price > max_price):
                continue

            owner = await resolve_owner_info(raw)
            if is_owner_blacklisted(owner):
                continue

            # Фильтрация
            if max_nft_gifts > 0 and owner.nft_gifts_count > max_nft_gifts:
                continue
            if max_rating > 0 and owner.rating_level >= max_rating:
                continue

            base = BASE_GIFTS_BY_ID.get(gift_id)
            base_title = base.title if base else "Gift"

            found.append(
                MarketGift(
                    title=str(get_field(raw, "title") or base_title),
                    num=safe_int(get_field(raw, "num")),
                    slug=slug,
                    price=price,
                    owner=owner,
                )
            )

            if len(found) >= need:
                return found

        offset = getattr(result, "next_offset", "") or ""
        if not offset:
            break

    return found


# ============================================================
# TOPIC MANAGEMENT
# ============================================================

async def get_or_create_model_topic(chat_id: int, model_name: str) -> Optional[int]:
    global TOPICS_CACHE

    clean_name = model_name.strip()
    if clean_name in TOPICS_CACHE:
        return TOPICS_CACHE[clean_name]

    async with topic_creation_lock:
        if clean_name in TOPICS_CACHE:
            return TOPICS_CACHE[clean_name]

        topic_title = clean_name[:128]
        try:
            forum_topic = await bot.create_forum_topic(
                chat_id=chat_id,
                name=topic_title
            )
            thread_id = forum_topic.message_thread_id
            TOPICS_CACHE[clean_name] = thread_id
            save_topics()

            log.info("Создан топик форума '%s' [ID: %s]", topic_title, thread_id)
            await asyncio.sleep(1.5)
            return thread_id

        except TelegramBadRequest as e:
            log.error("Не удалось создать топик '%s': %s", topic_title, e)
            return None
        except Exception as e:
            log.exception("Ошибка при создании топика: %s", e)
            return None


# ============================================================
# MONITORING LOGIC
# ============================================================

async def get_monitor_models() -> List[BaseGift]:
    await ensure_models_loaded()
    selected = {safe_int(x, 0) for x in (MONITOR_SETTINGS.get("model_ids") or [])}
    if not selected:
        return []
    return [g for g in BASE_GIFTS if g.gift_id in selected and g.availability_resale > 0]


async def send_monitor_gift(market_gift: MarketGift, base_gift: BaseGift) -> bool:
    target_chat_id = get_target_chat_id()
    if not target_chat_id:
        return False

    model_name = base_gift.title or market_gift.title or "Остальные подарки"
    thread_id = await get_or_create_model_topic(target_chat_id, model_name)

    owner = market_gift.owner
    username_line = f"👤 <b>Владелец:</b> @{escape_text(owner.username)}" if owner.username else "👤 <b>Владелец:</b> без username"
    profile_line = f'\n🔎 <a href="https://t.me/{escape_text(owner.username)}">Профиль владельца</a>' if owner.username else ""
    num_line = f"\n🔢 <b>Номер:</b> #{market_gift.num}" if market_gift.num else ""

    rating_line = f"\n👑 <b>Рейтинг Telegram:</b> Уровень {owner.rating_level} ({owner.rating_stars} ⭐)" if owner.rating_level > 0 else "\n👑 <b>Рейтинг Telegram:</b> 0 (без уровня)"
    nft_count_line = f"\n🖼 <b>NFT в профиле:</b> {owner.nft_gifts_count} шт."

    text = (
        "🚨 <b>НОВЫЙ NFT-ПОДАРОК</b>\n\n"
        f"🎁 <b>{escape_text(market_gift.title)}</b>{num_line}\n"
        f"💰 <b>Цена:</b> {market_gift.price} ⭐\n"
        f"📦 <b>Модель:</b> {escape_text(base_gift.title)}\n"
        f"{username_line}"
        f"{rating_line}"
        f"{nft_count_line}"
        f"{profile_line}\n\n"
        f'🔗 <a href="{escape_text(market_gift.link)}">Открыть подарок</a>'
    )

    try:
        await bot.send_message(
            chat_id=target_chat_id,
            text=text,
            message_thread_id=thread_id,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        log.error("Failed to send alert for %s: %s", market_gift.slug, e)
        return False


async def process_monitor_model(base_gift: BaseGift, first_scan: bool) -> int:
    sent_count = 0
    max_nft_gifts = safe_int(MONITOR_SETTINGS.get("max_owner_nft_gifts"), 0)
    max_rating = safe_int(MONITOR_SETTINGS.get("max_rating_level"), 0)

    try:
        result = await get_resale_page(
            gift_id=base_gift.gift_id,
            offset="",
            limit=MONITOR_PAGE_LIMIT,
            sort_by_price=False,
        )
        raw_gifts = getattr(result, "gifts", []) or []

        for raw in raw_gifts:
            slug = get_field(raw, "slug")
            if not slug or str(slug) in SENT_GIFTS:
                continue

            slug = str(slug)
            price = extract_stars_amount(get_field(raw, "resell_amount"))
            if not price_matches(price):
                continue

            owner = await resolve_owner_info(raw)
            if not username_matches(owner) or is_owner_blacklisted(owner):
                continue

            # ФИЛЬТР 1: ТОЛЬКО ПО NFT-ПОДАРКАМ
            if max_nft_gifts > 0 and owner.nft_gifts_count > max_nft_gifts:
                log.info("Пропуск %s: в профиле %s NFT (> %s)", slug, owner.nft_gifts_count, max_nft_gifts)
                continue

            # ФИЛЬТР 2: Порог рейтинга
            if max_rating > 0 and owner.rating_level >= max_rating:
                log.info("Пропуск %s: рейтинг %s >= порога %s", slug, owner.rating_level, max_rating)
                continue

            market_gift = MarketGift(
                title=str(get_field(raw, "title") or base_gift.title),
                num=safe_int(get_field(raw, "num")),
                slug=slug,
                price=price,
                owner=owner,
            )

            SENT_GIFTS[slug] = {
                "title": market_gift.title,
                "price": market_gift.price,
                "owner": market_gift.owner.display,
                "gift_id": base_gift.gift_id,
            }

            if first_scan and not SEND_EXISTING_ON_FIRST_SCAN:
                SENT_GIFTS[slug]["baseline"] = True
                continue

            sent = await send_monitor_gift(market_gift, base_gift)
            if sent:
                await asyncio.sleep(MONITOR_SEND_DELAY)
                sent_count += 1
            else:
                SENT_GIFTS.pop(slug, None)

    except FloodWaitError as e:
        wait_time = int(e.seconds) + 1
        log.warning("FloodWait в мониторе для %s: %s сек.", base_gift.title, wait_time)
        await asyncio.sleep(wait_time)
    except Exception as e:
        log.exception("Ошибка проверки модели %s: %s", base_gift.title, e)

    return sent_count


async def monitor_cycle(first_scan: bool = False) -> int:
    async with monitor_lock:
        models = await get_monitor_models()
        if not models:
            return 0

        total_sent = 0
        for index, model in enumerate(models, 1):
            if not MONITOR_SETTINGS.get("enabled"):
                break
            try:
                sent = await process_monitor_model(model, first_scan)
                total_sent += sent
                if sent:
                    save_sent_gifts()
                    log.info("Монитор: %s/%s | %s | отправлено=%s", index, len(models), model.title, sent)
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Ошибка в очереди модели %s: %s", model.title, e)

        if total_sent > 0 or first_scan:
            save_sent_gifts()

        return total_sent


async def monitor_loop() -> None:
    global monitor_running, monitor_first_scan_done
    monitor_running = True
    monitor_first_scan_done = False

    log.info(
        "Монитор запущен. Порог рейтинга: <%s, Макс. NFT: %s",
        MONITOR_SETTINGS.get("max_rating_level"),
        MONITOR_SETTINGS.get("max_owner_nft_gifts"),
    )

    try:
        while MONITOR_SETTINGS.get("enabled"):
            first_scan = not monitor_first_scan_done
            selected_models = MONITOR_SETTINGS.get("model_ids") or []
            if not selected_models:
                MONITOR_SETTINGS["enabled"] = False
                save_monitor_settings()
                break

            await monitor_cycle(first_scan=first_scan)
            monitor_first_scan_done = True
            await asyncio.sleep(max(15, MONITOR_INTERVAL))

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("Аварийное завершение цикла монитора: %s", e)
    finally:
        monitor_running = False
        log.info("Монитор остановлен.")


async def start_monitor() -> bool:
    global monitor_task
    if not is_admin(ADMIN_ID) or not get_target_chat_id() or not await is_user_client_authorized():
        return False

    MONITOR_SETTINGS["enabled"] = True
    save_monitor_settings()

    if monitor_task and not monitor_task.done():
        return True

    monitor_task = asyncio.create_task(monitor_loop())
    return True


async def stop_monitor() -> None:
    global monitor_task
    MONITOR_SETTINGS["enabled"] = False
    save_monitor_settings()

    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
    monitor_task = None


# ============================================================
# MODELS LOADER
# ============================================================

async def load_base_gifts() -> List[BaseGift]:
    global BASE_GIFTS, BASE_GIFTS_BY_ID
    log.info("Загрузка моделей подарков с маркета...")
    try:
        await ensure_user_client_connected()
        if not await user_client.is_user_authorized():
            return []

        result = await user_client(functions.payments.GetStarGiftsRequest(hash=0))
        raw_gifts = getattr(result, "gifts", []) or []

        gifts: List[BaseGift] = []
        for raw in raw_gifts:
            gift_id = safe_int(get_field(raw, "id"))
            title = get_field(raw, "title")
            availability_resale = safe_int(get_field(raw, "availability_resale"))
            resell_min_stars = safe_int(get_field(raw, "resell_min_stars"))

            if not gift_id or not title or availability_resale <= 0:
                continue

            gifts.append(
                BaseGift(
                    gift_id=gift_id,
                    title=str(title),
                    stars=safe_int(get_field(raw, "stars")),
                    availability_resale=availability_resale,
                    resell_min_stars=resell_min_stars,
                    sold_out=bool(get_field(raw, "sold_out")),
                )
            )

        gifts.sort(key=lambda g: (g.resell_min_stars or 999999999, g.title.lower()))
        BASE_GIFTS = gifts
        BASE_GIFTS_BY_ID = {g.gift_id: g for g in gifts}
        log.info("Успешно загружено %s моделей.", len(gifts))
        return gifts
    except Exception as e:
        log.exception("Ошибка загрузки моделей: %s", e)
        return []


async def ensure_models_loaded() -> None:
    if not BASE_GIFTS:
        await load_base_gifts()


# ============================================================
# KEYBOARDS & UI
# ============================================================

def main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📡 МОНИТОРИНГ", callback_data="monitor_menu")],
        [InlineKeyboardButton(text="🔎 РУЧНОЙ ПОИСК", callback_data="manual_parse")],
        [InlineKeyboardButton(text="🚫 ЧЁРНЫЙ СПИСОК", callback_data="blacklist")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_panel")])
    rows.append([InlineKeyboardButton(text="🔄 ОБНОВИТЬ МОДЕЛИ", callback_data="reload_models")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def monitor_keyboard() -> InlineKeyboardMarkup:
    enabled = bool(MONITOR_SETTINGS.get("enabled"))
    rows = []

    if enabled:
        rows.append([InlineKeyboardButton(text="⏸ ОСТАНОВИТЬ", callback_data="monitor_stop")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ ЗАПУСТИТЬ", callback_data="monitor_start")])

    rows.extend([
        [
            InlineKeyboardButton(text="💰 ЦЕНА", callback_data="monitor_price"),
            InlineKeyboardButton(text="👑 РЕЙТИНГ", callback_data="monitor_rating"),
        ],
        [
            InlineKeyboardButton(text="🖼 NFT ЛИМИТ", callback_data="monitor_nft_limit"),
            InlineKeyboardButton(text="👤 USERNAME", callback_data="monitor_username"),
        ],
        [InlineKeyboardButton(text="🎁 ВЫБОР МОДЕЛЕЙ", callback_data="monitor_models")],
        [InlineKeyboardButton(text="🧹 СБРОСИТЬ ОТПРАВЛЕННЫЕ", callback_data="sent_clear")],
        [InlineKeyboardButton(text="🏠 ГЛАВНОЕ МЕНЮ", callback_data="menu")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def monitor_status_text() -> str:
    enabled = bool(MONITOR_SETTINGS.get("enabled"))
    target = get_target_chat_id()
    min_price = safe_int(MONITOR_SETTINGS.get("min_price"), 0)
    max_price = safe_int(MONITOR_SETTINGS.get("max_price"), 0)
    max_rating = safe_int(MONITOR_SETTINGS.get("max_rating_level"), 0)
    max_nft = safe_int(MONITOR_SETTINGS.get("max_owner_nft_gifts"), 0)

    if min_price and max_price:
        price_text = f"{min_price}—{max_price} ⭐"
    elif max_price:
        price_text = f"до {max_price} ⭐"
    elif min_price:
        price_text = f"от {min_price} ⭐"
    else:
        price_text = "без ограничения"

    username_text = "только с username" if MONITOR_SETTINGS.get("username_mode") == "required" else "все (с/без username)"
    selected_models = MONITOR_SETTINGS.get("model_ids") or []
    model_text = "все модели" if not selected_models else f"{len(selected_models)} выбранных"
    target_text = str(target) if target else "❌ не задана"

    rating_info = f"до {max_rating} уровня (строго < {max_rating})" if max_rating > 0 else "выключен"
    nft_info = f"максимум {max_nft} шт." if max_nft > 0 else "выключен"

    return (
        "📡 <b>АВТОМАТИЧЕСКИЙ МОНИТОРИНГ</b>\n\n"
        f"Статус: {'🟢 РАБОТАЕТ' if enabled else '🔴 ОСТАНОВЛЕН'}\n"
        f"Целевая группа: <code>{escape_text(target_text)}</code>\n"
        f"Диапазон цены: <b>{escape_text(price_text)}</b>\n"
        f"Фильтр рейтинга: <b>{escape_text(rating_info)}</b>\n"
        f"NFT-подарков в профиле: <b>{escape_text(nft_info)}</b>\n"
        f"Фильтр юзернеймов: <b>{escape_text(username_text)}</b>\n"
        f"Модели: <b>{escape_text(model_text)}</b>\n"
        f"Интервал обхода: <b>{MONITOR_INTERVAL} сек.</b>\n"
        f"Отправлено лотов: <b>{len(SENT_GIFTS)}</b> | Создано тем: <b>{len(TOPICS_CACHE)}</b>\n\n"
        "💡 Для привязки группы напиши команду <code>/set_target_chat</code> прямо в ней."
    )


# ============================================================
# BOT HANDLERS
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    if not await is_user_client_authorized():
        if is_admin(user_id):
            await message.answer(
                "⚠️ Сессия Telethon не подключена.\nНажми /add_session",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="➕ ДОБАВИТЬ СЕССИЮ", callback_data="add_session_btn")]]
                ),
            )
        else:
            await message.answer("⚠️ Бот настраивается администратором.")
        return

    await ensure_models_loaded()
    await message.answer(
        "🎁 <b>NFT GIFT MONITOR BOT</b>\n\nВыбери нужный раздел:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(user_id),
    )


@dp.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎁 <b>NFT GIFT MONITOR BOT</b>\n\nВыбери нужный раздел:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data == "monitor_menu")
async def monitor_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для администратора.", show_alert=True)
        return

    await callback.message.edit_text(
        monitor_status_text(),
        parse_mode="HTML",
        reply_markup=monitor_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "monitor_start")
async def monitor_start_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    if not get_target_chat_id():
        await callback.answer("Сначала привяжи группу через /set_target_chat", show_alert=True)
        return
    if not await is_user_client_authorized():
        await callback.answer("Сессия не авторизована.", show_alert=True)
        return
    if not MONITOR_SETTINGS.get("model_ids"):
        await callback.answer("Выбери хотя бы 1 модель в меню 🎁 ВЫБОР МОДЕЛЕЙ", show_alert=True)
        return

    await start_monitor()
    await callback.message.edit_text(monitor_status_text(), parse_mode="HTML", reply_markup=monitor_keyboard())
    await callback.answer("🟢 Мониторинг запущен.")


@dp.callback_query(F.data == "monitor_stop")
async def monitor_stop_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await stop_monitor()
    await callback.message.edit_text(monitor_status_text(), parse_mode="HTML", reply_markup=monitor_keyboard())
    await callback.answer("⏸ Мониторинг остановлен.")


@dp.callback_query(F.data == "monitor_username")
async def monitor_username_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    current = MONITOR_SETTINGS.get("username_mode", "any")
    MONITOR_SETTINGS["username_mode"] = "required" if current == "any" else "any"
    save_monitor_settings()
    await callback.message.edit_text(monitor_status_text(), parse_mode="HTML", reply_markup=monitor_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "monitor_price")
async def monitor_price_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(MonitorPriceState.waiting_range)
    await callback.message.answer("💰 Отправь диапазон цены (например: <code>0 5000</code>)\n❌ Отмена — /cancel", parse_mode="HTML")
    await callback.answer()


@dp.message(MonitorPriceState.waiting_range)
async def monitor_price_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").strip().replace("-", " ").split()
    if len(parts) != 2:
        await message.answer("❌ Введи два числа через пробел, например: <code>0 5000</code>", parse_mode="HTML")
        return
    try:
        min_p, max_p = int(parts[0]), int(parts[1])
        if min_p < 0 or max_p < 0 or (min_p and max_p and min_p > max_p):
            raise ValueError
    except ValueError:
        await message.answer("❌ Некорректный диапазон.")
        return

    MONITOR_SETTINGS["min_price"] = min_p
    MONITOR_SETTINGS["max_price"] = max_p
    save_monitor_settings()
    await state.clear()
    await message.answer(monitor_status_text(), parse_mode="HTML", reply_markup=monitor_keyboard())


# --- НАСТРОЙКА РЕЙТИНГА ---

@dp.callback_query(F.data == "monitor_rating")
async def monitor_rating_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(MonitorRatingState.waiting_rating)
    await callback.message.answer(
        "👑 <b>Настройка фильтра рейтинга Telegram</b>\n\n"
        "Введи максимальный уровень рейтинга продавца.\n"
        "• <code>10</code> — будут приходить только продавцы с рейтингом <b>строго меньше 10</b> (уровень 10 и выше отсекаются).\n"
        "• <code>0</code> — отключить проверку рейтинга.\n\n"
        "❌ Отмена — /cancel",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(MonitorRatingState.waiting_rating)
async def monitor_rating_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("❌ Отправь одно целое число (например, <code>10</code> или <code>0</code>).", parse_mode="HTML")
        return

    val = int(text)
    MONITOR_SETTINGS["max_rating_level"] = val
    save_monitor_settings()
    await state.clear()
    await message.answer(
        f"✅ Фильтр рейтинга сохранён: <b>{'парсим только тех, у кого уровень < ' + str(val) if val > 0 else 'отключен'}</b>",
        parse_mode="HTML"
    )
    await message.answer(monitor_status_text(), parse_mode="HTML", reply_markup=monitor_keyboard())


# --- НАСТРОЙКА ЛИМИТА NFT-ПОДАРКОВ ---

@dp.callback_query(F.data == "monitor_nft_limit")
async def monitor_nft_limit_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(MonitorNftLimitState.waiting_limit)
    await callback.message.answer(
        "🖼 <b>Максимум NFT-подарков в профиле</b>\n\n"
        "Укажи лимит именно уникальных NFT-подарков на витрине владельца.\n"
        "Обычные звёздные подарки (торты, мишки) не учитываются!\n\n"
        "• <code>5</code> — пропускать тех, у кого в профиле больше 5 NFT-подарков.\n"
        "• <code>0</code> — выключить фильтр.\n\n"
        "❌ Отмена — /cancel",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(MonitorNftLimitState.waiting_limit)
async def monitor_nft_limit_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("❌ Отправь число (например: <code>5</code> или <code>0</code>).", parse_mode="HTML")
        return

    val = int(text)
    MONITOR_SETTINGS["max_owner_nft_gifts"] = val
    save_monitor_settings()
    await state.clear()
    await message.answer(
        f"✅ Лимит NFT-подарков сохранён: <b>максимум {val} шт.</b>",
        parse_mode="HTML"
    )
    await message.answer(monitor_status_text(), parse_mode="HTML", reply_markup=monitor_keyboard())


# --- ВЫБОР МОДЕЛЕЙ И ПАГИНАЦИЯ ---

def monitor_models_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    selected = set(MONITOR_SETTINGS.get("model_ids") or [])
    start = page * GIFTS_PER_PAGE
    end = start + GIFTS_PER_PAGE
    rows = []

    for gift in BASE_GIFTS[start:end]:
        mark = "✅" if gift.gift_id in selected else "⬜"
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {gift.title}"[:64],
                callback_data=f"toggle_monitor_model:{page}:{gift.gift_id}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"monitor_models_page:{page - 1}"))
    if end < len(BASE_GIFTS):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"monitor_models_page:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text="✅ ВЫБРАТЬ ВСЕ", callback_data=f"monitor_models_all:{page}"),
        InlineKeyboardButton(text="⬜ СБРОСИТЬ ВСЕ", callback_data=f"monitor_models_none:{page}"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ НАЗАД", callback_data="monitor_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "monitor_models")
async def monitor_models_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    selected = MONITOR_SETTINGS.get("model_ids") or []
    await callback.message.edit_text(
        f"🎁 <b>МОДЕЛИ ДЛЯ МОНИТОРИНГА</b> (Выбрано: {len(selected)})\n\n✅ = парсим, ⬜ = пропускаем.",
        parse_mode="HTML",
        reply_markup=monitor_models_keyboard(0),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("monitor_models_page:"))
async def monitor_models_page_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split(":")[1])
    selected = MONITOR_SETTINGS.get("model_ids") or []
    await callback.message.edit_text(
        f"🎁 <b>МОДЕЛИ ДЛЯ МОНИТОРИНГА</b> (Выбрано: {len(selected)})\n\n✅ = парсим, ⬜ = пропускаем.",
        parse_mode="HTML",
        reply_markup=monitor_models_keyboard(page),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_monitor_model:"))
async def toggle_monitor_model_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    _, page_str, gift_id_str = callback.data.split(":")
    page, gift_id = int(page_str), int(gift_id_str)
    selected = set(MONITOR_SETTINGS.get("model_ids") or [])

    if gift_id in selected:
        selected.remove(gift_id)
    else:
        selected.add(gift_id)

    MONITOR_SETTINGS["model_ids"] = sorted(selected)
    save_monitor_settings()

    try:
        await callback.message.edit_text(
            f"🎁 <b>МОДЕЛИ ДЛЯ МОНИТОРИНГА</b> (Выбрано: {len(selected)})\n\n✅ = парсим, ⬜ = пропускаем.",
            parse_mode="HTML",
            reply_markup=monitor_models_keyboard(page),
        )
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("monitor_models_all"))
async def monitor_models_all_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split(":")[1]) if ":" in callback.data else 0
    MONITOR_SETTINGS["model_ids"] = [g.gift_id for g in BASE_GIFTS]
    save_monitor_settings()
    await callback.message.edit_text(
        f"🎁 <b>МОДЕЛИ МОНИТОРИНГА</b>\n\nВыбраны все {len(BASE_GIFTS)} моделей.",
        parse_mode="HTML",
        reply_markup=monitor_models_keyboard(page),
    )
    await callback.answer("Выбраны все.")


@dp.callback_query(F.data.startswith("monitor_models_none"))
async def monitor_models_none_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split(":")[1]) if ":" in callback.data else 0
    MONITOR_SETTINGS["model_ids"] = []
    MONITOR_SETTINGS["enabled"] = False
    save_monitor_settings()
    await callback.message.edit_text(
        "🎁 <b>МОДЕЛИ МОНИТОРИНГА</b>\n\nВсе модели сняты. Мониторинг выключен.",
        parse_mode="HTML",
        reply_markup=monitor_models_keyboard(page),
    )
    await callback.answer("Сброшено.")


@dp.callback_query(F.data == "sent_clear")
async def sent_clear_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    SENT_GIFTS.clear()
    save_sent_gifts()
    await callback.answer("🧹 Список отправленных очищен.", show_alert=True)
    await callback.message.edit_text(monitor_status_text(), parse_mode="HTML", reply_markup=monitor_keyboard())


# ============================================================
# TARGET CHAT, SESSION & BLACKLIST
# ============================================================

@dp.message(Command("set_target_chat"))
async def set_target_chat(message: Message):
    if not is_admin(message.from_user.id):
        return
    MONITOR_SETTINGS["target_chat_id"] = message.chat.id
    save_monitor_settings()
    await message.answer(f"✅ Целевая группа форума привязана!\nChat ID: <code>{message.chat.id}</code>", parse_mode="HTML")


@dp.message(Command("add_session"))
async def add_session_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if await is_user_client_authorized():
        await message.answer("✅ Сессия уже подключена.")
        return
    await state.set_state(AuthState.waiting_phone)
    await message.answer("📱 Введи номер телефона аккаунта Telethon (например: +79991234567):\n❌ /cancel")


@dp.message(AuthState.waiting_phone)
async def auth_phone(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    phone = (message.text or "").strip()
    if not phone.startswith("+"):
        phone = "+" + phone
    await ensure_user_client_connected()
    try:
        await user_client.send_code_request(phone)
        await state.update_data(phone=phone)
        await state.set_state(AuthState.waiting_code)
        await message.answer("✅ Код подтверждения отправлен в Telegram. Введи код:")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки кода: {e}")


@dp.message(AuthState.waiting_code)
async def auth_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    code = "".join(ch for ch in (message.text or "").strip() if ch.isdigit())
    data = await state.get_data()
    try:
        await user_client.sign_in(phone=data.get("phone"), code=code)
        await state.clear()
        await message.answer("✅ Сессия авторизована! Загружаю модели подарков...")
        await load_base_gifts()
        await message.answer(f"Готово. Доступно моделей: {len(BASE_GIFTS)}")
    except SessionPasswordNeededError:
        await state.set_state(AuthState.waiting_password)
        await message.answer("🔐 На аккаунте включён 2FA. Введи пароль от облачного пароля:")
    except Exception as e:
        await message.answer(f"❌ Ошибка входа: {e}")


@dp.message(AuthState.waiting_password)
async def auth_password(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await user_client.sign_in(password=(message.text or "").strip())
        await state.clear()
        await message.answer("✅ 2FA успешно пройдена! Загружаю модели...")
        await load_base_gifts()
        await message.answer(f"Готово. Загружено моделей: {len(BASE_GIFTS)}")
    except Exception as e:
        await message.answer(f"❌ Неверный пароль: {e}")


@dp.callback_query(F.data == "blacklist")
async def show_blacklist(callback: CallbackQuery):
    items = list(OWNERS_BLACKLIST.items())
    text = "🚫 <b>ЧЁРНЫЙ СПИСОК ПУСТ</b>" if not items else f"🚫 <b>ЧЁРНЫЙ СПИСОК</b> ({len(items)})"
    rows = []
    for key, label in items[:20]:
        rows.append([InlineKeyboardButton(text=f"❌ {label}"[:64], callback_data=f"unban:{key}"[:64])])
    if is_admin(callback.from_user.id):
        rows.append([InlineKeyboardButton(text="➕ ДОБАВИТЬ В ЧС", callback_data="blacklist_add")])
        rows.append([InlineKeyboardButton(text="🧹 ОЧИСТИТЬ ВСЁ", callback_data="blacklist_clear")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data == "blacklist_add")
async def blacklist_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(BlacklistState.waiting_value)
    await callback.message.answer("🚫 Введи <b>@username</b> или числовой <b>user_id</b> продавца:\n❌ /cancel", parse_mode="HTML")
    await callback.answer()


@dp.message(BlacklistState.waiting_value)
async def blacklist_add_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw:
        return
    if raw.startswith("@"):
        key = f"username:{raw[1:].lower()}"
        label = raw
    elif raw.lstrip("-").isdigit():
        key = f"id:{int(raw)}"
        label = key
    else:
        await message.answer("❌ Нужен @username или числовой user_id.")
        return

    OWNERS_BLACKLIST[key] = label
    save_owner_blacklist()
    await state.clear()
    await message.answer(f"✅ Добавлен в ЧС: <code>{key}</code>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("unban:"))
async def unban_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    key = callback.data.split(":", 1)[1]
    OWNERS_BLACKLIST.pop(key, None)
    save_owner_blacklist()
    await callback.answer(f"✅ Удалено: {key}")
    await show_blacklist(callback)


@dp.callback_query(F.data == "blacklist_clear")
async def clear_blacklist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    OWNERS_BLACKLIST.clear()
    save_owner_blacklist()
    await callback.answer("🧹 ЧС очищен.")
    await show_blacklist(callback)


# ============================================================
# MANUAL SEARCH & ADMIN PANEL
# ============================================================

def models_keyboard(page: int = 0, mode: str = "manual") -> InlineKeyboardMarkup:
    total = len(BASE_GIFTS)
    start = page * GIFTS_PER_PAGE
    end = start + GIFTS_PER_PAGE
    rows = []

    for gift in BASE_GIFTS[start:end]:
        text = gift.title
        if gift.resell_min_stars:
            text += f" · от {gift.resell_min_stars}⭐"
        rows.append([InlineKeyboardButton(text=text[:64], callback_data=f"gift_{mode}:{gift.gift_id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"models_{mode}:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"models_{mode}:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "manual_parse")
async def manual_parse(callback: CallbackQuery):
    if not await is_user_client_authorized():
        await callback.answer("Сессия не авторизована.", show_alert=True)
        return
    await ensure_models_loaded()
    await callback.message.edit_text("🔎 <b>РУЧНОЙ ПОИСК</b>\n\nВыбери модель:", parse_mode="HTML", reply_markup=models_keyboard(0, "manual"))
    await callback.answer()


@dp.callback_query(F.data.startswith("models_manual:"))
async def models_manual_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔎 <b>РУЧНОЙ ПОИСК</b>\n\nВыбери модель:", parse_mode="HTML", reply_markup=models_keyboard(page, "manual"))
    await callback.answer()


@dp.callback_query(F.data.startswith("gift_manual:"))
async def gift_manual(callback: CallbackQuery):
    gift_id = int(callback.data.split(":")[1])
    USER_SELECTED_GIFT[callback.from_user.id] = gift_id
    gift = BASE_GIFTS_BY_ID.get(gift_id)
    if not gift:
        return
    await callback.message.edit_text(
        f"✅ <b>{escape_text(gift.title)}</b>\n💰 Мин. цена: {gift.resell_min_stars or 0}⭐\n"
        f"📦 На маркете: {gift.availability_resale} шт.\n\nОтправь диапазон цен (например: <code>500 5000</code>):",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    is_auth = await is_user_client_authorized()
    me = await user_client.get_me() if is_auth else None
    account = f"{me.first_name} (@{me.username})" if me else "—"

    await callback.message.edit_text(
        "⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\n"
        f"🔐 Сессия: {'✅ АКТИВНА' if is_auth else '❌ НЕ АКТИВНА'}\n"
        f"👤 Аккаунт: {escape_text(account)}\n"
        f"📦 Моделей в базе: {len(BASE_GIFTS)}\n"
        f"🚫 В blacklist: {len(OWNERS_BLACKLIST)}\n"
        f"📤 Отправлено: {len(SENT_GIFTS)}\n"
        f"📂 Тем форума: {len(TOPICS_CACHE)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ ДОБАВИТЬ СЕССИЮ", callback_data="add_session_btn")],
                [InlineKeyboardButton(text="🔄 ОБНОВИТЬ МОДЕЛИ", callback_data="reload_models")],
                [InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")],
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(F.data == "reload_models")
async def reload_models_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer("Загрузка моделей...")
    await load_base_gifts()
    await callback.message.edit_text(f"✅ Модели обновлены. Доступно: {len(BASE_GIFTS)}", reply_markup=main_menu_keyboard(callback.from_user.id))


@dp.callback_query(F.data == "add_session_btn")
async def add_session_btn(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AuthState.waiting_phone)
    await callback.message.answer("📱 Введи номер телефона (пример: +79991234567):\n❌ /cancel")
    await callback.answer()


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    USER_SELECTED_GIFT.pop(message.from_user.id, None)
    await message.answer("❌ Отменено.")


@dp.message()
async def generic_message_handler(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return

    user_id = message.from_user.id
    if user_id not in USER_SELECTED_GIFT:
        return

    parts = (message.text or "").strip().replace("-", " ").split()
    if len(parts) != 2:
        await message.answer("❌ Введи два числа: <code>500 5000</code>", parse_mode="HTML")
        return

    try:
        min_p, max_p = int(parts[0]), int(parts[1])
        if min_p < 0 or max_p < 0 or min_p > max_p:
            raise ValueError
    except ValueError:
        await message.answer("❌ Некорректный диапазон цен.")
        return

    gift_id = USER_SELECTED_GIFT.pop(user_id)
    base = BASE_GIFTS_BY_ID.get(gift_id)
    if not base:
        return

    status = await message.answer(f"⏳ Поиск {escape_text(base.title)} ({min_p}—{max_p}⭐)...")
    results = await find_market_gifts(gift_id=gift_id, min_price=min_p, max_price=max_p, need=10)

    try:
        await status.delete()
    except Exception:
        pass

    if not results:
        await message.answer(f"❌ Ничего не найдено для {escape_text(base.title)} в диапазоне {min_p}—{max_p}⭐")
        return

    text = f"<b>{escape_text(base.title)}</b> (найдено: {len(results)}):\n\n"
    for i, g in enumerate(results, 1):
        text += (
            f"{i}. #{g.num} — <b>{g.price} ⭐</b>\n"
            f"👤 {escape_text(g.owner.display)} (Рейтинг: lvl {g.owner.rating_level}, NFT в профиле: {g.owner.nft_gifts_count})\n"
            f"🔗 {escape_text(g.link)}\n\n"
        )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


# ============================================================
# MAIN
# ============================================================

async def main():
    global OWNERS_BLACKLIST, OWNER_CACHE, SENT_GIFTS, TOPICS_CACHE

    validate_config()

    OWNERS_BLACKLIST = load_owner_blacklist()
    OWNER_CACHE = load_owner_cache()
    SENT_GIFTS = load_sent_gifts()
    TOPICS_CACHE = load_topics()
    load_monitor_settings()

    log.info(
        "Данные загружены: ЧС=%s, кэш=%s, отправлено=%s, тем=%s",
        len(OWNERS_BLACKLIST), len(OWNER_CACHE), len(SENT_GIFTS), len(TOPICS_CACHE)
    )

    await ensure_user_client_connected()

    if await is_user_client_authorized():
        me = await user_client.get_me()
        log.info("Сессия Telethon активна: %s (@%s)", me.first_name, me.username)
        await ensure_models_loaded()
        if MONITOR_SETTINGS.get("enabled") and get_target_chat_id():
            await start_monitor()
    else:
        log.warning("Сессия Telethon не авторизована. Напиши боту /add_session")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    log.info("Бот запущен в режиме polling.")
    try:
        await dp.start_polling(bot)
    finally:
        await stop_monitor()
        if user_client.is_connected():
            await user_client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
