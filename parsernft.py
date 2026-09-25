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
from telethon.tl.types import PeerUser


# ============================================================
# CONFIG
# ============================================================

API_ID = 26259835
API_HASH = "3fa32264398920f001dd2428b42060f6"
BOT_TOKEN = "8206373294:AAEeZp8zrOquQeWrPHL7SJ4nG-mp3nfivrI"
ADMIN_ID = 8986358602

SESSION_NAME = "telethon_market_userbot"

TARGET_CHAT_ID = -1003660453372

MONITOR_INTERVAL = 15
MONITOR_PAGE_LIMIT = 10
REQUEST_CONCURRENCY = 1
MONITOR_API_DELAY = 1.10
MONITOR_SEND_DELAY = 0.35
MONITOR_MAX_SEND_PER_CYCLE = 0

DEFAULT_MIN_PRICE = 0
DEFAULT_MAX_PRICE = 0
DEFAULT_USERNAME_MODE = "any"

SEND_EXISTING_ON_FIRST_SCAN = False

GIFTS_PER_PAGE = 8
REQUEST_PAGE_LIMIT = 50
MAX_MANUAL_PAGES = 30

OWNER_BLACKLIST_FILE = "owner_blacklist.json"
OWNER_CACHE_FILE = "owner_cache.json"
SENT_GIFTS_FILE = "sent_gifts.json"
MONITOR_SETTINGS_FILE = "monitor_settings.json"


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
            "\nОткрой верхнюю секцию CONFIG в этом файле и заполни их."
        )


validate_config()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

BASE_GIFTS: List["BaseGift"] = []
BASE_GIFTS_BY_ID: Dict[int, "BaseGift"] = {}

OWNERS_BLACKLIST: Dict[str, str] = {}
OWNER_CACHE: Dict[str, Dict[str, Any]] = {}

SENT_GIFTS: Dict[str, Dict[str, Any]] = {}

MONITOR_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "target_chat_id": TARGET_CHAT_ID,
    "min_price": DEFAULT_MIN_PRICE,
    "max_price": DEFAULT_MAX_PRICE,
    "username_mode": DEFAULT_USERNAME_MODE,
    "model_ids": [],
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

session_alert_sent = False


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
# FSM
# ============================================================

class AuthState(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class MonitorPriceState(StatesGroup):
    waiting_range = State()


class BlacklistState(StatesGroup):
    waiting_value = State()


# ============================================================
# JSON
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
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_owner_blacklist() -> None:
    save_json(OWNER_BLACKLIST_FILE, OWNERS_BLACKLIST)


def load_owner_cache() -> Dict[str, Dict[str, Any]]:
    data = load_json(OWNER_CACHE_FILE, {})
    if not isinstance(data, dict):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            result[str(k)] = dict(v)
    return result


def save_owner_cache() -> None:
    save_json(OWNER_CACHE_FILE, OWNER_CACHE)


def load_sent_gifts() -> Dict[str, Dict[str, Any]]:
    data = load_json(SENT_GIFTS_FILE, {})
    if not isinstance(data, dict):
        return {}
    return {
        str(k): v if isinstance(v, dict) else {}
        for k, v in data.items()
    }


def save_sent_gifts() -> None:
    save_json(SENT_GIFTS_FILE, SENT_GIFTS)


def load_monitor_settings() -> None:
    global MONITOR_SETTINGS

    data = load_json(MONITOR_SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}

    MONITOR_SETTINGS["enabled"] = bool(data.get("enabled", False))
    MONITOR_SETTINGS["target_chat_id"] = int(
        data.get("target_chat_id", TARGET_CHAT_ID) or 0
    )
    MONITOR_SETTINGS["min_price"] = max(
        0, int(data.get("min_price", DEFAULT_MIN_PRICE) or 0)
    )
    MONITOR_SETTINGS["max_price"] = max(
        0, int(data.get("max_price", DEFAULT_MAX_PRICE) or 0)
    )

    username_mode = str(
        data.get("username_mode", DEFAULT_USERNAME_MODE)
    ).lower()
    MONITOR_SETTINGS["username_mode"] = (
        username_mode if username_mode in {"any", "required"} else "any"
    )

    model_ids = data.get("model_ids", [])
    if isinstance(model_ids, list):
        MONITOR_SETTINGS["model_ids"] = [
            int(x) for x in model_ids if str(x).lstrip("-").isdigit()
        ]
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
        value = safe_int(getattr(raw_id, "user_id"), 0)
        return value or None
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
    if mode == "required":
        return bool(owner.username)
    return True


def model_is_enabled(gift_id: int) -> bool:
    selected = {safe_int(x, 0) for x in (MONITOR_SETTINGS.get("model_ids") or [])}
    return gift_id in selected


def escape_text(value: Any) -> str:
    return html.escape(str(value or ""))


# ============================================================
# TELETHON CONNECTION / AUTH
# ============================================================

async def ensure_user_client_connected() -> None:
    if not user_client.is_connected():
        await user_client.connect()


async def is_user_client_authorized() -> bool:
    await ensure_user_client_connected()
    try:
        return await user_client.is_user_authorized()
    except (AuthKeyUnregisteredError, ValueError, ConnectionError) as e:
        log.warning("Session invalid or kicked: %s", e)
        return False


# ============================================================
# AUTH
# ============================================================

@dp.message(Command("add_session"))
async def add_session_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора.")
        return

    if await is_user_client_authorized():
        await message.answer("✅ Сессия уже добавлена.")
        return

    await state.set_state(AuthState.waiting_phone)
    await message.answer(
        "📱 ДОБАВЛЕНИЕ TELEGRAM-СЕССИИ\n\n"
        "Введите номер телефона:\n"
        "Пример: +79991234567\n\n"
        "❌ Отмена — /cancel"
    )


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
        await message.answer("✅ Код отправлен. Введите код из Telegram.")
    except PhoneNumberInvalidError:
        await message.answer("❌ Неверный номер.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(AuthState.waiting_code)
async def auth_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    code = "".join(ch for ch in (message.text or "").strip() if ch.isdigit())
    if len(code) < 4:
        await message.answer("❌ Код слишком короткий.")
        return

    data = await state.get_data()
    phone = data.get("phone")

    try:
        await user_client.sign_in(phone=phone, code=code)
        await state.clear()
        await finish_auth(message)
    except SessionPasswordNeededError:
        await state.set_state(AuthState.waiting_password)
        await message.answer("🔐 Введите пароль от 2FA.")
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код.")
    except PhoneCodeExpiredError:
        await message.answer("❌ Код истёк. Начните заново: /add_session")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(AuthState.waiting_password)
async def auth_password(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        await user_client.sign_in(password=(message.text or "").strip())
        await state.clear()
        await finish_auth(message)
    except PasswordHashInvalidError:
        await message.answer("❌ Неверный пароль.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def finish_auth(message: Message):
    global session_alert_sent

    session_alert_sent = False

    me = await user_client.get_me()
    name = me.first_name or me.username or str(me.id)

    await message.answer(
        f"✅ Сессия добавлена!\n"
        f"👤 Аккаунт: {escape_text(name)}\n\n"
        f"Загружаю модели подарков..."
    )

    await load_base_gifts()

    await message.answer(
        f"✅ Готово!\n"
        f"📦 Моделей: {len(BASE_GIFTS)}\n\n"
        f"Нажмите /start"
    )


# ============================================================
# LOAD BASE GIFTS
# ============================================================

async def load_base_gifts() -> List[BaseGift]:
    global BASE_GIFTS, BASE_GIFTS_BY_ID

    log.info("Loading base Star Gifts...")

    try:
        await ensure_user_client_connected()

        if not await user_client.is_user_authorized():
            log.warning("User client not authorized")
            return []

        result = await user_client(
            functions.payments.GetStarGiftsRequest(hash=0)
        )

        raw_gifts = getattr(result, "gifts", []) or []
        log.info("Raw base gifts received: %s", len(raw_gifts))

        gifts: List[BaseGift] = []

        for raw in raw_gifts:
            gift_id = safe_int(get_field(raw, "id"))
            title = get_field(raw, "title")
            availability_resale = safe_int(
                get_field(raw, "availability_resale")
            )
            resell_min_stars = safe_int(
                get_field(raw, "resell_min_stars")
            )

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

        gifts.sort(
            key=lambda g: (
                g.resell_min_stars or 999999999,
                g.title.lower(),
            )
        )

        BASE_GIFTS = gifts
        BASE_GIFTS_BY_ID = {g.gift_id: g for g in gifts}

        log.info("Loaded %s marketplace gift models", len(gifts))
        return gifts

    except Exception as e:
        log.exception("Error loading gifts: %s", e)
        return []


async def ensure_models_loaded() -> None:
    if not BASE_GIFTS:
        await load_base_gifts()


# ============================================================
# MARKETPLACE RATE LIMITER
# ============================================================

async def pace_market_api() -> None:
    global last_market_api_request

    async with market_api_lock:
        now = asyncio.get_running_loop().time()
        wait_for = MONITOR_API_DELAY - (now - last_market_api_request)
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        last_market_api_request = asyncio.get_running_loop().time()


# ============================================================
# OWNER RESOLUTION
# ============================================================

async def resolve_owner_info(raw_gift: Any) -> OwnerInfo:
    owner_id = extract_owner_id(get_field(raw_gift, "owner_id"))
    owner_name = get_field(raw_gift, "owner_name")

    if owner_id:
        cache_key = str(owner_id)
        cached = OWNER_CACHE.get(cache_key)

        if cached:
            username = cached.get("username") or None
            label = cached.get("label") or f"id:{owner_id}"
            key = (
                f"username:{username.lower()}"
                if username
                else f"id:{owner_id}"
            )
            return OwnerInfo(
                key=key,
                label=label,
                username=username,
                user_id=owner_id,
            )

        try:
            await ensure_user_client_connected()

            await pace_market_api()
            entity = await user_client.get_entity(owner_id)

            username = getattr(entity, "username", None)
            first_name = getattr(entity, "first_name", None)
            last_name = getattr(entity, "last_name", None)

            name = " ".join(
                x for x in [first_name, last_name] if x
            ).strip()

            if username:
                label = f"@{username}"
                key = f"username:{username.lower()}"
            else:
                label = name or f"id:{owner_id}"
                key = f"id:{owner_id}"

            OWNER_CACHE[cache_key] = {
                "label": label,
                "username": username or "",
                "user_id": owner_id,
            }
            save_owner_cache()

            return OwnerInfo(
                key=key,
                label=label,
                username=username,
                user_id=owner_id,
            )

        except FloodWaitError as e:
            wait_time = int(e.seconds) + 1
            log.warning(
                "FloodWait on owner resolve: %s sec",
                wait_time,
            )
            await asyncio.sleep(wait_time)

            return OwnerInfo(
                key=f"id:{owner_id}",
                label=f"id:{owner_id}",
                username=None,
                user_id=owner_id,
            )

        except Exception as e:
            log.debug(
                "Owner resolve failed for %s: %s",
                owner_id,
                e,
            )
            return OwnerInfo(
                key=f"id:{owner_id}",
                label=f"id:{owner_id}",
                username=None,
                user_id=owner_id,
            )

    if owner_name:
        return OwnerInfo(
            key=f"name:{str(owner_name).lower()}",
            label=str(owner_name),
            username=None,
            user_id=None,
        )

    return OwnerInfo(
        key="unknown",
        label="не указан",
        username=None,
        user_id=None,
    )


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
                if (
                    "stars_only" in str(e)
                    or "for_craft" in str(e)
                ):
                    log.warning(
                        "Old Telethon GetResaleStarGiftsRequest signature; "
                        "retrying without newer flags."
                    )

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
            log.warning(
                "FloodWait on resale request: %s sec",
                wait_time,
            )
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

            if not slug:
                continue

            slug = str(slug)

            if slug in skip_slugs:
                continue

            price = extract_stars_amount(
                get_field(raw, "resell_amount")
            )

            if price < min_price:
                continue

            if max_price and price > max_price:
                continue

            base = BASE_GIFTS_BY_ID.get(gift_id)
            base_title = base.title if base else "Gift"

            owner = await resolve_owner_info(raw)

            if is_owner_blacklisted(owner):
                continue

            found.append(
                MarketGift(
                    title=str(
                        get_field(raw, "title") or base_title
                    ),
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
# MONITORING
# ============================================================

async def get_monitor_models() -> List[BaseGift]:
    await ensure_models_loaded()

    selected = {safe_int(x, 0) for x in (MONITOR_SETTINGS.get("model_ids") or [])}

    if not selected:
        return []

    return [
        gift
        for gift in BASE_GIFTS
        if gift.gift_id in selected
        and gift.availability_resale > 0
    ]


async def send_monitor_gift(
    market_gift: MarketGift,
    base_gift: BaseGift,
) -> bool:
    target_chat_id = get_target_chat_id()

    if not target_chat_id:
        log.warning(
            "Monitor cannot send notifications: target chat is not set."
        )
        return False

    owner = market_gift.owner

    username_line = (
        f"👤 <b>Владелец:</b> @{escape_text(owner.username)}"
        if owner.username
        else "👤 <b>Владелец:</b> без username"
    )

    profile_line = ""
    if owner.username:
        profile_line = (
            f'\n🔎 <a href="https://t.me/'
            f'{escape_text(owner.username)}">Профиль</a>'
        )

    num_line = (
        f"\n🔢 <b>Номер:</b> #{market_gift.num}"
        if market_gift.num
        else ""
    )

    text = (
        "🚨 <b>НОВЫЙ NFT-ПОДАРОК</b>\n\n"
        f"🎁 <b>{escape_text(market_gift.title)}</b>"
        f"{num_line}\n"
        f"💰 <b>Цена:</b> {market_gift.price} ⭐\n"
        f"📦 <b>Модель:</b> {escape_text(base_gift.title)}\n"
        f"{username_line}"
        f"{profile_line}\n\n"
        f'🔗 <a href="{escape_text(market_gift.link)}">'
        f"Открыть подарок</a>"
    )

    try:
        await bot.send_message(
            target_chat_id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True

    except Exception as e:
        log.error(
            "Failed to send monitor notification for %s: %s",
            market_gift.slug,
            e,
        )
        return False


async def process_monitor_model(
    base_gift: BaseGift,
    first_scan: bool,
) -> int:
    sent_count = 0

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

            if not slug:
                continue

            slug = str(slug)

            if slug in SENT_GIFTS:
                continue

            price = extract_stars_amount(
                get_field(raw, "resell_amount")
            )

            if not price_matches(price):
                continue

            owner = await resolve_owner_info(raw)

            if not username_matches(owner):
                continue

            if is_owner_blacklisted(owner):
                continue

            market_gift = MarketGift(
                title=str(
                    get_field(raw, "title")
                    or base_gift.title
                ),
                num=safe_int(get_field(raw, "num")),
                slug=slug,
                price=price,
                owner=owner,
            )

            if first_scan and not SEND_EXISTING_ON_FIRST_SCAN:
                SENT_GIFTS[slug] = {
                    "title": market_gift.title,
                    "price": market_gift.price,
                    "owner": market_gift.owner.display,
                    "baseline": True,
                }
                continue

            sent = await send_monitor_gift(
                market_gift,
                base_gift,
            )

            if sent:
                await asyncio.sleep(MONITOR_SEND_DELAY)

                SENT_GIFTS[slug] = {
                    "title": market_gift.title,
                    "price": market_gift.price,
                    "owner": market_gift.owner.display,
                    "gift_id": base_gift.gift_id,
                }
                sent_count += 1

    except FloodWaitError as e:
        wait_time = int(e.seconds) + 1
        log.warning(
            "FloodWait in monitor for %s: %s sec",
            base_gift.title,
            wait_time,
        )
        await asyncio.sleep(wait_time)

    except Exception as e:
        log.exception(
            "Monitor model error %s (%s): %s",
            base_gift.title,
            base_gift.gift_id,
            e,
        )

    return sent_count


async def monitor_cycle(first_scan: bool = False) -> int:
    async with monitor_lock:
        models = await get_monitor_models()

        if not models:
            log.info("Monitor: no models selected. Waiting for admin selection.")
            return 0

        total_sent = 0

        for index, model in enumerate(models, 1):
            if not MONITOR_SETTINGS.get("enabled"):
                break

            try:
                sent = await process_monitor_model(
                    model,
                    first_scan,
                )
                total_sent += sent

                if sent:
                    save_sent_gifts()
                    log.info(
                        "Monitor: %s/%s | %s | sent=%s",
                        index,
                        len(models),
                        model.title,
                        sent,
                    )

                await asyncio.sleep(0.05)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(
                    "Monitor queue error for %s (%s): %s",
                    model.title,
                    model.gift_id,
                    e,
                )

        return total_sent


async def monitor_loop() -> None:
    global monitor_running
    global monitor_first_scan_done

    monitor_running = True
    monitor_first_scan_done = False

    log.info(
        "Monitor started: cycle_pause=%ss, api_delay=%.2fs, models=%s, max_price=%s, username_mode=%s",
        MONITOR_INTERVAL,
        MONITOR_API_DELAY,
        len(await get_monitor_models()),
        MONITOR_SETTINGS.get("max_price"),
        MONITOR_SETTINGS.get("username_mode"),
    )

    try:
        while MONITOR_SETTINGS.get("enabled"):
            first_scan = not monitor_first_scan_done

            selected_models = MONITOR_SETTINGS.get("model_ids") or []
            if not selected_models:
                MONITOR_SETTINGS["enabled"] = False
                save_monitor_settings()
                log.info("Monitor stopped: no models selected.")
                break

            sent = await monitor_cycle(
                first_scan=first_scan
            )

            monitor_first_scan_done = True

            if sent:
                log.info(
                    "Monitor cycle finished: sent=%s",
                    sent,
                )

            await asyncio.sleep(max(5, MONITOR_INTERVAL))

    except asyncio.CancelledError:
        log.info("Monitor task cancelled.")
        raise

    except Exception as e:
        log.exception(
            "Monitor loop crashed: %s",
            e,
        )

    finally:
        monitor_running = False
        log.info("Monitor stopped.")


async def start_monitor() -> bool:
    global monitor_task

    if not is_admin(ADMIN_ID):
        return False

    if not get_target_chat_id():
        return False

    if not await is_user_client_authorized():
        return False

    MONITOR_SETTINGS["enabled"] = True
    save_monitor_settings()

    if monitor_task and not monitor_task.done():
        return True

    monitor_task = asyncio.create_task(
        monitor_loop()
    )

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
# UI
# ============================================================

def main_menu_keyboard(
    user_id: Optional[int] = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="📡 МОНИТОРИНГ",
                callback_data="monitor_menu",
            )
        ],
        [
            InlineKeyboardButton(
                text="🔎 РУЧНОЙ ПОИСК",
                callback_data="manual_parse",
            )
        ],
        [
            InlineKeyboardButton(
                text="🚫 ЧЁРНЫЙ СПИСОК",
                callback_data="blacklist",
            )
        ],
    ]

    if is_admin(user_id):
        rows.append(
            [
                InlineKeyboardButton(
                    text="⚙️ АДМИН-ПАНЕЛЬ",
                    callback_data="admin_panel",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 ОБНОВИТЬ МОДЕЛИ",
                callback_data="reload_models",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def monitor_keyboard() -> InlineKeyboardMarkup:
    enabled = bool(MONITOR_SETTINGS.get("enabled"))
    target = get_target_chat_id()

    rows = []

    if enabled:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⏸ ОСТАНОВИТЬ",
                    callback_data="monitor_stop",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ ЗАПУСТИТЬ",
                    callback_data="monitor_start",
                )
            ]
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="💰 ЦЕНА",
                    callback_data="monitor_price",
                ),
                InlineKeyboardButton(
                    text="👤 USERNAME",
                    callback_data="monitor_username",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎁 МОДЕЛИ",
                    callback_data="monitor_models",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧹 СБРОСИТЬ ОТПРАВЛЕННЫЕ",
                    callback_data="sent_clear",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 ГЛАВНОЕ",
                    callback_data="menu",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def models_keyboard(
    page: int = 0,
    mode: str = "manual",
) -> InlineKeyboardMarkup:
    total = len(BASE_GIFTS)
    start = page * GIFTS_PER_PAGE
    end = start + GIFTS_PER_PAGE

    rows = []

    for gift in BASE_GIFTS[start:end]:
        text = gift.title

        if gift.resell_min_stars:
            text += f" · от {gift.resell_min_stars}⭐"

        if gift.availability_resale:
            text += f" · {gift.availability_resale} шт."

        rows.append(
            [
                InlineKeyboardButton(
                    text=text[:64],
                    callback_data=(
                        f"gift_{mode}:{gift.gift_id}"
                    ),
                )
            ]
        )

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=(
                    f"models_{mode}:{page - 1}"
                ),
            )
        )

    if end < total:
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=(
                    f"models_{mode}:{page + 1}"
                ),
            )
        )

    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 ГЛАВНОЕ",
                callback_data="menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 ПРОВЕРИТЬ СЕССИЮ",
                    callback_data="check_session_admin",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ ДОБАВИТЬ СЕССИЮ",
                    callback_data="add_session_btn",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 БЛОКИРОВКА ЮЗЕРОВ",
                    callback_data="blacklist",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 СТАТУС БОТА",
                    callback_data="bot_status",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 ГЛАВНОЕ",
                    callback_data="menu",
                )
            ],
        ]
    )


def monitor_status_text() -> str:
    enabled = bool(MONITOR_SETTINGS.get("enabled"))
    target = get_target_chat_id()

    min_price = safe_int(
        MONITOR_SETTINGS.get("min_price"),
        0,
    )
    max_price = safe_int(
        MONITOR_SETTINGS.get("max_price"),
        0,
    )

    if min_price and max_price:
        price_text = f"{min_price}—{max_price} ⭐"
    elif max_price:
        price_text = f"до {max_price} ⭐"
    elif min_price:
        price_text = f"от {min_price} ⭐"
    else:
        price_text = "без ограничения"

    username_text = (
        "только с username"
        if MONITOR_SETTINGS.get("username_mode") == "required"
        else "с username и без username"
    )

    selected_models = MONITOR_SETTINGS.get("model_ids") or []
    model_text = (
        "все модели"
        if not selected_models
        else f"{len(selected_models)} выбранных моделей"
    )

    target_text = (
        str(target)
        if target
        else "❌ не задана"
    )

    return (
        "📡 <b>АВТОМАТИЧЕСКИЙ МОНИТОРИНГ</b>\n\n"
        f"Статус: {'🟢 работает' if enabled else '🔴 выключен'}\n"
        f"Группа: <code>{escape_text(target_text)}</code>\n"
        f"Цена: <b>{escape_text(price_text)}</b>\n"
        f"Владелец: <b>{escape_text(username_text)}</b>\n"
        f"Модели: <b>{escape_text(model_text)}</b>\n"
        f"Интервал: <b>{MONITOR_INTERVAL} сек.</b>\n"
        f"Уже отправлено: <b>{len(SENT_GIFTS)}</b>\n\n"
        "💡 Для назначения группы отправь "
        "<code>/set_target_chat</code> прямо в нужной группе."
    )


# ============================================================
# START / MENU
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id

    if not await is_user_client_authorized():
        if is_admin(user_id):
            await message.answer(
                "⚠️ Telegram-сессия не добавлена.\n\n"
                "Используйте /add_session",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="➕ ДОБАВИТЬ СЕССИЮ",
                                callback_data="add_session_btn",
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(
                "⚠️ Бот ещё не настроен."
            )
        return

    await ensure_models_loaded()

    await message.answer(
        "🎁 <b>NFT GIFT MONITOR</b>\n\n"
        "📡 Автоматический мониторинг — новые лоты "
        "в группу.\n"
        "🔎 Ручной поиск — поиск по модели и цене.\n\n"
        "Для фильтрации владельцев можно выбрать:\n"
        "• с username\n"
        "• с username и без username",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(user_id),
    )


@dp.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery):
    user_id = callback.from_user.id

    await callback.message.edit_text(
        "🎁 <b>NFT GIFT MONITOR</b>\n\n"
        "Выбери режим:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(user_id),
    )
    await callback.answer()


# ============================================================
# MONITOR UI
# ============================================================

@dp.callback_query(F.data == "monitor_menu")
async def monitor_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        monitor_status_text(),
        parse_mode="HTML",
        reply_markup=monitor_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "monitor_start")
async def monitor_start_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    if not get_target_chat_id():
        await callback.answer(
            "Сначала назначь группу через /set_target_chat.",
            show_alert=True,
        )
        return

    if not await is_user_client_authorized():
        await callback.answer(
            "Сначала добавь Telegram-сессию.",
            show_alert=True,
        )
        return

    selected_models = MONITOR_SETTINGS.get("model_ids") or []
    if not selected_models:
        await callback.answer(
            "Сначала выбери хотя бы одну модель в разделе 🎁 МОДЕЛИ.",
            show_alert=True,
        )
        return

    await start_monitor()

    await callback.message.edit_text(
        monitor_status_text(),
        parse_mode="HTML",
        reply_markup=monitor_keyboard(),
    )
    await callback.answer("🟢 Мониторинг запущен.")


@dp.callback_query(F.data == "monitor_stop")
async def monitor_stop_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    await stop_monitor()

    await callback.message.edit_text(
        monitor_status_text(),
        parse_mode="HTML",
        reply_markup=monitor_keyboard(),
    )
    await callback.answer("⏸ Мониторинг остановлен.")


@dp.callback_query(F.data == "monitor_username")
async def monitor_username_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    current = MONITOR_SETTINGS.get(
        "username_mode",
        "any",
    )

    new_mode = (
        "required"
        if current == "any"
        else "any"
    )

    MONITOR_SETTINGS["username_mode"] = new_mode
    save_monitor_settings()

    label = (
        "только с username"
        if new_mode == "required"
        else "с username и без username"
    )

    await callback.message.edit_text(
        monitor_status_text(),
        parse_mode="HTML",
        reply_markup=monitor_keyboard(),
    )
    await callback.answer(
        f"Фильтр: {label}"
    )


@dp.callback_query(F.data == "monitor_price")
async def monitor_price_callback(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    await state.set_state(
        MonitorPriceState.waiting_range
    )

    await callback.message.answer(
        "💰 Отправь диапазон цены.\n\n"
        "Примеры:\n"
        "<code>0 5000</code> — до 5000 ⭐\n"
        "<code>1000 5000</code> — от 1000 до 5000 ⭐\n"
        "<code>1000 0</code> — от 1000 ⭐ без верхнего лимита\n"
        "<code>0 0</code> — без ограничения\n\n"
        "❌ /cancel",
        parse_mode="HTML",
    )

    await callback.answer()


@dp.message(MonitorPriceState.waiting_range)
async def monitor_price_input(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    parts = (
        (message.text or "")
        .strip()
        .replace("-", " ")
        .split()
    )

    if len(parts) != 2:
        await message.answer(
            "❌ Нужно два числа, например: <code>0 5000</code>",
            parse_mode="HTML",
        )
        return

    try:
        min_price = int(parts[0])
        max_price = int(parts[1])

        if min_price < 0 or max_price < 0:
            raise ValueError

        if min_price and max_price and min_price > max_price:
            raise ValueError

    except ValueError:
        await message.answer(
            "❌ Некорректный диапазон."
        )
        return

    MONITOR_SETTINGS["min_price"] = min_price
    MONITOR_SETTINGS["max_price"] = max_price

    save_monitor_settings()
    await state.clear()

    await message.answer(
        monitor_status_text(),
        parse_mode="HTML",
        reply_markup=monitor_keyboard(),
    )


@dp.callback_query(F.data == "monitor_models")
async def monitor_models_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    selected = MONITOR_SETTINGS.get("model_ids") or []

    await callback.message.edit_text(
        "🎁 <b>МОДЕЛИ МОНИТОРИНГА</b>\n\n"
        "Сейчас: "
        + (
            "⚠️ ничего не выбрано"
            if not selected
            else f"{len(selected)} выбранных"
        )
        + "\n\n"
        "Нажимай на модели — ✅ = парсим, ⬜ = не парсим.",
        parse_mode="HTML",
        reply_markup=monitor_models_keyboard(0),
    )
    await callback.answer()


def monitor_models_keyboard(
    page: int = 0,
) -> InlineKeyboardMarkup:
    selected = set(
        MONITOR_SETTINGS.get("model_ids") or []
    )

    start = page * GIFTS_PER_PAGE
    end = start + GIFTS_PER_PAGE

    rows = []

    for gift in BASE_GIFTS[start:end]:
        mark = "✅" if gift.gift_id in selected else "⬜"

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {gift.title}"[:64],
                    callback_data=(
                        f"toggle_monitor_model:{page}:{gift.gift_id}"
                    ),
                )
            ]
        )

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=(
                    f"monitor_models_page:{page - 1}"
                ),
            )
        )

    if end < len(BASE_GIFTS):
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=(
                    f"monitor_models_page:{page + 1}"
                ),
            )
        )

    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                text="✅ ВЫБРАТЬ ВСЕ",
                callback_data=f"monitor_models_all:{page}",
            ),
            InlineKeyboardButton(
                text="⬜ СБРОСИТЬ ВСЕ",
                callback_data=f"monitor_models_none:{page}",
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ НАЗАД",
                callback_data="monitor_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


@dp.callback_query(
    F.data.startswith("monitor_models_page:")
)
async def monitor_models_page_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    page = int(callback.data.split(":")[1])

    selected = MONITOR_SETTINGS.get("model_ids") or []

    await callback.message.edit_text(
        "🎁 <b>МОДЕЛИ МОНИТОРИНГА</b>\n\n"
        "Сейчас: "
        + (
            "⚠️ ничего не выбрано"
            if not selected
            else f"{len(selected)} выбранных"
        )
        + "\n\n"
        "Нажми на модель, чтобы включить/выключить её.",
        parse_mode="HTML",
        reply_markup=monitor_models_keyboard(page),
    )
    await callback.answer()


@dp.callback_query(
    F.data.startswith("toggle_monitor_model:")
)
async def toggle_monitor_model_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    parts = callback.data.split(":")

    if len(parts) != 3:
        await callback.answer(
            "Кнопка устарела, открой меню заново.",
            show_alert=True,
        )
        return

    try:
        page = int(parts[1])
        gift_id = int(parts[2])
    except ValueError:
        await callback.answer(
            "Некорректные данные.",
            show_alert=True,
        )
        return

    selected = set(
        MONITOR_SETTINGS.get("model_ids") or []
    )

    if gift_id in selected:
        selected.remove(gift_id)
        text = "Модель выключена."
    else:
        selected.add(gift_id)
        text = "Модель включена."

    MONITOR_SETTINGS["model_ids"] = sorted(selected)
    save_monitor_settings()

    header = (
        "🎁 <b>МОДЕЛИ МОНИТОРИНГА</b>\n\n"
        f"Сейчас: {len(selected)} выбрано\n\n"
        "Нажимай на модели — ✅ = парсим, ⬜ = не парсим."
    )

    try:
        await callback.message.edit_text(
            header,
            parse_mode="HTML",
            reply_markup=monitor_models_keyboard(page),
        )
    except Exception:
        pass

    await callback.answer(text)


@dp.callback_query(
    F.data.startswith("monitor_models_all")
)
async def monitor_models_all_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    page = 0
    if ":" in callback.data:
        try:
            page = int(callback.data.split(":")[1])
        except ValueError:
            page = 0

    MONITOR_SETTINGS["model_ids"] = [gift.gift_id for gift in BASE_GIFTS]
    save_monitor_settings()

    await callback.message.edit_text(
        "🎁 <b>МОДЕЛИ МОНИТОРИНГА</b>\n\n"
        f"Выбраны <b>все {len(BASE_GIFTS)}</b> модели.\n\n"
        "Если нужно меньше — просто нажми на ненужные модели.",
        parse_mode="HTML",
        reply_markup=monitor_models_keyboard(page),
    )
    await callback.answer("✅ Выбраны все модели.")


@dp.callback_query(
    F.data.startswith("monitor_models_none")
)
async def monitor_models_none_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    page = 0
    if ":" in callback.data:
        try:
            page = int(callback.data.split(":")[1])
        except ValueError:
            page = 0

    MONITOR_SETTINGS["model_ids"] = []
    MONITOR_SETTINGS["enabled"] = False
    save_monitor_settings()

    await callback.message.edit_text(
        "🎁 <b>МОДЕЛИ МОНИТОРИНГА</b>\n\n"
        "⬜ Все модели сняты.\n"
        "Теперь монитор ничего не парсит, пока ты не выберешь модели.",
        parse_mode="HTML",
        reply_markup=monitor_models_keyboard(page),
    )
    await callback.answer("⬜ Все модели сняты.")


@dp.callback_query(F.data == "sent_clear")
async def sent_clear_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для администратора.",
            show_alert=True,
        )
        return

    SENT_GIFTS.clear()
    save_sent_gifts()

    await callback.answer(
        "🧹 История отправленных подарков очищена.",
        show_alert=True,
    )

    await callback.message.edit_text(
        monitor_status_text(),
        parse_mode="HTML",
        reply_markup=monitor_keyboard(),
    )


# ============================================================
# TARGET GROUP
# ============================================================

@dp.message(Command("set_target_chat"))
async def set_target_chat(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "⛔ Только для администратора."
        )
        return

    MONITOR_SETTINGS["target_chat_id"] = message.chat.id
    save_monitor_settings()

    await message.answer(
        "✅ Эта группа установлена как цель мониторинга.\n\n"
        f"Chat ID: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )


@dp.message(Command("target_chat"))
async def target_chat(message: Message):
    if not is_admin(message.from_user.id):
        return

    target = get_target_chat_id()

    await message.answer(
        "📢 Целевая группа:\n"
        f"<code>{target or 'не задана'}</code>",
        parse_mode="HTML",
    )


# ============================================================
# MANUAL SEARCH
# ============================================================

@dp.callback_query(F.data == "manual_parse")
async def manual_parse(callback: CallbackQuery):
    if not await is_user_client_authorized():
        await callback.answer(
            "Сессия не активна.",
            show_alert=True,
        )
        return

    await ensure_models_loaded()

    await callback.message.edit_text(
        "🔎 <b>РУЧНОЙ ПОИСК</b>\n\n"
        "Выбери модель:",
        parse_mode="HTML",
        reply_markup=models_keyboard(
            0,
            "manual",
        ),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("models_manual:"))
async def models_manual_page(
    callback: CallbackQuery,
):
    page = int(
        callback.data.split(":")[1]
    )

    await callback.message.edit_text(
        "🔎 <b>РУЧНОЙ ПОИСК</b>\n\n"
        "Выбери модель:",
        parse_mode="HTML",
        reply_markup=models_keyboard(
            page,
            "manual",
        ),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("gift_manual:"))
async def gift_manual(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id
    gift_id = int(
        callback.data.split(":")[1]
    )

    USER_SELECTED_GIFT[user_id] = gift_id

    gift = BASE_GIFTS_BY_ID.get(gift_id)

    if not gift:
        await callback.answer(
            "Модель не найдена.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        f"✅ <b>{escape_text(gift.title)}</b>\n\n"
        f"💰 Мин. цена: "
        f"{gift.resell_min_stars or 0}⭐\n"
        f"📦 На маркете: "
        f"{gift.availability_resale} шт.\n\n"
        "Отправь диапазон:\n"
        "<code>500 5000</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message()
async def price_handler(message: Message):
    user_id = message.from_user.id

    if user_id not in USER_SELECTED_GIFT:
        return

    parts = (
        (message.text or "")
        .strip()
        .replace("-", " ")
        .split()
    )

    if len(parts) != 2:
        await message.answer(
            "❌ Отправь два числа: <code>500 5000</code>",
            parse_mode="HTML",
        )
        return

    try:
        min_price = int(parts[0])
        max_price = int(parts[1])

        if min_price < 0 or max_price < 0:
            raise ValueError

        if min_price > max_price:
            raise ValueError

    except ValueError:
        await message.answer(
            "❌ Введи корректные числа."
        )
        return

    gift_id = USER_SELECTED_GIFT.pop(user_id)

    base = BASE_GIFTS_BY_ID.get(gift_id)

    if not base:
        await message.answer(
            "❌ Модель не найдена."
        )
        return

    USER_SEARCH_HISTORY[user_id] = {
        "gift_id": gift_id,
        "min_price": min_price,
        "max_price": max_price,
    }

    status = await message.answer(
        f"⏳ Ищу {escape_text(base.title)} "
        f"от {min_price} до {max_price}⭐...",
        parse_mode="HTML",
    )

    results = await find_market_gifts(
        gift_id=gift_id,
        min_price=min_price,
        max_price=max_price,
        need=10,
        sort_by_price=True,
    )

    USER_LAST_RESULTS[user_id] = list(results)

    try:
        await status.delete()
    except Exception:
        pass

    await send_search_results(
        message,
        f"🎁 {base.title}",
        results,
        min_price,
        max_price,
    )


def search_results_keyboard(
    results: List[MarketGift],
) -> InlineKeyboardMarkup:
    rows = []

    for gift in results[:10]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"🚫 Забанить "
                        f"{gift.owner.display}"
                    )[:64],
                    callback_data=(
                        f"ban_owner:{gift.slug}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔁 ПОВТОРИТЬ ПОИСК",
                callback_data="repeat_search",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🚫 ЧЁРНЫЙ СПИСОК",
                callback_data="blacklist",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 ГЛАВНОЕ",
                callback_data="menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


async def send_search_results(
    message: Message,
    title: str,
    results: List[MarketGift],
    min_price: int,
    max_price: int,
) -> None:
    if not results:
        await message.answer(
            f"❌ {escape_text(title)}\n"
            f"Ничего не найдено в диапазоне "
            f"{min_price}-{max_price} ⭐",
            parse_mode="HTML",
        )
        return

    text = (
        f"<b>{escape_text(title)}</b>\n"
        f"💰 Диапазон: {min_price}—{max_price} ⭐\n"
        f"🔎 Найдено: {len(results)}\n\n"
    )

    for i, gift in enumerate(
        results[:10],
        1,
    ):
        num = (
            f" #{gift.num}"
            if gift.num
            else ""
        )

        text += (
            f"{i}. <b>{escape_text(gift.title)}</b>"
            f"{num}\n"
            f"💰 {gift.price} ⭐ | "
            f"👤 {escape_text(gift.owner.display)}\n"
            f"🔗 {escape_text(gift.link)}\n\n"
        )

    if len(text) > 4000:
        text = text[:3950] + "\n\n..."

    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=search_results_keyboard(
            results
        ),
    )


@dp.callback_query(F.data == "repeat_search")
async def repeat_search(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id
    search = USER_SEARCH_HISTORY.get(user_id)

    if not search:
        await callback.answer(
            "Нет прошлого поиска.",
            show_alert=True,
        )
        return

    gift_id = search["gift_id"]
    min_price = search["min_price"]
    max_price = search["max_price"]

    base = BASE_GIFTS_BY_ID.get(gift_id)

    if not base:
        await callback.answer(
            "Модель не найдена.",
            show_alert=True,
        )
        return

    await callback.answer("Ищу...")

    results = await find_market_gifts(
        gift_id=gift_id,
        min_price=min_price,
        max_price=max_price,
        need=10,
        sort_by_price=True,
    )

    USER_LAST_RESULTS[user_id] = list(results)

    try:
        await callback.message.delete()
    except Exception:
        pass

    await send_search_results(
        callback.message,
        f"🎁 {base.title}",
        results,
        min_price,
        max_price,
    )


# ============================================================
# EXACT OWNER BLACKLIST
# ============================================================

@dp.callback_query(
    F.data.startswith("ban_owner:")
)
async def ban_owner_callback(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    try:
        slug = callback.data.split(
            ":", 1
        )[1]
    except Exception:
        await callback.answer(
            "Ошибка.",
            show_alert=True,
        )
        return

    results = USER_LAST_RESULTS.get(
        user_id,
        []
    )

    gift = next(
        (
            item
            for item in results
            if item.slug == slug
        ),
        None,
    )

    if not gift:
        await callback.answer(
            "Результат устарел. Сделай поиск ещё раз.",
            show_alert=True,
        )
        return

    owner = gift.owner

    if owner.key == "unknown":
        await callback.answer(
            "Не удалось определить владельца.",
            show_alert=True,
        )
        return

    blacklist_owner(owner)

    await callback.answer(
        f"✅ Заблокирован {owner.display}",
        show_alert=True,
    )


# ============================================================
# BLACKLIST UI
# ============================================================

@dp.callback_query(F.data == "blacklist")
async def show_blacklist(
    callback: CallbackQuery,
):
    items = list(OWNERS_BLACKLIST.items())

    if not items:
        text = "🚫 <b>ЧЁРНЫЙ СПИСОК ПУСТ</b>"
    else:
        text = (
            f"🚫 <b>ЧЁРНЫЙ СПИСОК</b> ({len(items)})\n\n"
            "Нажми на запись, чтобы разбанить."
        )

    rows = []

    for key, label in items[:20]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"❌ {label}"[:64],
                    callback_data=f"unban:{key}"[:64],
                )
            ]
        )

    if is_admin(callback.from_user.id):
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ ДОБАВИТЬ В ЧС",
                    callback_data="blacklist_add",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧹 ОЧИСТИТЬ ВСЁ",
                    callback_data="blacklist_clear",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 ГЛАВНОЕ",
                callback_data="menu",
            )
        ]
    )

    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=rows
            ),
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=rows
            ),
        )

    await callback.answer()


@dp.callback_query(F.data == "blacklist_add")
async def blacklist_add(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для админа.",
            show_alert=True,
        )
        return

    await state.set_state(
        BlacklistState.waiting_value
    )

    await callback.message.answer(
        "🚫 Введи <b>@username</b> или <b>user_id</b>:\n\n"
        "Примеры:\n"
        "<code>@durov</code>\n"
        "<code>123456789</code>\n\n"
        "❌ /cancel",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(BlacklistState.waiting_value)
async def blacklist_add_value(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    raw = (message.text or "").strip()

    if not raw:
        await message.answer("❌ Пусто.")
        return

    if raw.startswith("@"):
        username = raw[1:].lower()

        if not username.replace("_", "").isalnum():
            await message.answer(
                "❌ Некорректный username."
            )
            return

        key = f"username:{username}"
        label = f"@{username}"
    else:
        digits = raw.lstrip("-")

        if not digits.isdigit():
            await message.answer(
                "❌ Нужен @username или числовой user_id."
            )
            return

        uid = int(raw)
        key = f"id:{uid}"
        label = f"id:{uid}"

    OWNERS_BLACKLIST[key] = label
    save_owner_blacklist()

    await state.clear()

    await message.answer(
        f"✅ Добавлено в ЧС: <code>{escape_text(key)}</code>\n"
        f"Метка: {escape_text(label)}\n\n"
        "Посмотреть/удалить: /start → 🚫 ЧЁРНЫЙ СПИСОК",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("unban:"))
async def unban_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для админа.",
            show_alert=True,
        )
        return

    key = callback.data.split(":", 1)[1]

    if key in OWNERS_BLACKLIST:
        OWNERS_BLACKLIST.pop(key, None)
        save_owner_blacklist()
        await callback.answer(
            f"✅ Удалено: {key}",
            show_alert=True,
        )
    else:
        await callback.answer(
            "Уже нет в списке.",
            show_alert=True,
        )

    await show_blacklist(callback)


@dp.callback_query(F.data == "blacklist_clear")
async def clear_blacklist(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для админа.",
            show_alert=True,
        )
        return

    OWNERS_BLACKLIST.clear()
    save_owner_blacklist()

    await callback.answer(
        "🧹 Чёрный список очищен."
    )

    await show_blacklist(callback)


# ============================================================
# ADMIN
# ============================================================

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для админа."
        )
        return

    is_auth = await is_user_client_authorized()

    status = (
        "✅ АКТИВНА"
        if is_auth
        else "❌ НЕ АКТИВНА"
    )

    me = (
        await user_client.get_me()
        if is_auth
        else None
    )

    account = (
        f"{me.first_name} (@{me.username})"
        if me
        else "—"
    )

    await callback.message.edit_text(
        "⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\n"
        f"🔐 Сессия: {status}\n"
        f"👤 Аккаунт: {escape_text(account)}\n"
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"🚫 В blacklist: {len(OWNERS_BLACKLIST)}\n"
        f"📤 Отправлено NFT: {len(SENT_GIFTS)}",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )

    await callback.answer()


@dp.callback_query(F.data == "check_session_admin")
async def check_session_admin(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для админа."
        )
        return

    is_auth = await is_user_client_authorized()

    if is_auth:
        try:
            me = await user_client.get_me()
            name = me.first_name or "Пользователь"
            username = (
                f" (@{me.username})"
                if me.username
                else ""
            )

            await callback.answer(
                f"✅ Сессия активна\n"
                f"👤 {name}{username}",
                show_alert=True,
            )
        except Exception:
            await callback.answer(
                "✅ Сессия активна.",
                show_alert=True,
            )
    else:
        await callback.answer(
            "❌ Сессия не активна.\n"
            "Используй /add_session",
            show_alert=True,
        )

    await admin_panel(callback)


@dp.callback_query(F.data == "add_session_btn")
async def add_session_btn(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для админа."
        )
        return

    await callback.message.answer(
        "📱 ДОБАВЛЕНИЕ СЕССИИ\n\n"
        "Введите номер телефона:\n"
        "Пример: +79991234567\n\n"
        "❌ /cancel"
    )

    await state.set_state(
        AuthState.waiting_phone
    )

    await callback.answer()


@dp.callback_query(F.data == "bot_status")
async def bot_status_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ Только для админа."
        )
        return

    is_auth = await is_user_client_authorized()
    me = (
        await user_client.get_me()
        if is_auth
        else None
    )

    target = get_target_chat_id()

    text = (
        "📊 <b>СТАТУС БОТА</b>\n\n"
        f"🤖 Бот: @{escape_text(bot.username or '—')}\n"
        f"🔐 Сессия: "
        f"{'✅ АКТИВНА' if is_auth else '❌ НЕ АКТИВНА'}\n"
        f"📡 Монитор: "
        f"{'🟢 ON' if MONITOR_SETTINGS.get('enabled') else '🔴 OFF'}\n"
        f"📢 Группа: <code>{target or 'не задана'}</code>\n"
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"🚫 Blacklist: {len(OWNERS_BLACKLIST)}\n"
        f"📤 Sent: {len(SENT_GIFTS)}"
    )

    if me:
        text += (
            f"\n👤 Аккаунт: "
            f"{escape_text(me.first_name or '—')}"
        )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )

    await callback.answer()


@dp.callback_query(F.data == "reload_models")
async def reload_models_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Только для админа."
        )
        return

    await callback.answer(
        "Обновляю..."
    )

    await load_base_gifts()

    await callback.message.edit_text(
        f"✅ <b>Модели обновлены.</b>\n\n"
        f"📦 Загружено: {len(BASE_GIFTS)}",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(
            callback.from_user.id
        ),
    )


# ============================================================
# CANCEL
# ============================================================

@dp.message(Command("cancel"))
async def cancel_command(
    message: Message,
    state: FSMContext,
):
    await state.clear()
    USER_SELECTED_GIFT.pop(
        message.from_user.id,
        None,
    )

    await message.answer(
        "❌ Действие отменено."
    )


# ============================================================
# SESSION MONITOR
# ============================================================

async def check_session_and_alert():
    global session_alert_sent

    is_auth = await is_user_client_authorized()

    if not is_auth and not session_alert_sent:
        session_alert_sent = True

        await stop_monitor()

        try:
            await bot.send_message(
                ADMIN_ID,
                "🚨 <b>СЕССИЯ TELEGRAM ПОТЕРЯНА</b>\n\n"
                "Аккаунт разлогинился.\n"
                "Восстановите сессию: /add_session",
                parse_mode="HTML",
            )

            log.warning(
                "Session loss alert sent to admin."
            )

        except Exception as e:
            log.error(
                "Failed to send session alert: %s",
                e,
            )

        return False

    if is_auth and session_alert_sent:
        session_alert_sent = False

        try:
            await bot.send_message(
                ADMIN_ID,
                "✅ <b>СЕССИЯ ВОССТАНОВЛЕНА</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    return is_auth


async def session_monitor():
    while True:
        await asyncio.sleep(60)

        try:
            await check_session_and_alert()
        except Exception as e:
            log.error(
                "Session monitor error: %s",
                e,
            )


# ============================================================
# STARTUP
# ============================================================

def validate_config() -> None:
    missing = []

    if not API_ID:
        missing.append("API_ID")

    if not API_HASH:
        missing.append("API_HASH")

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if not ADMIN_ID:
        missing.append("ADMIN_ID")

    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
        )


async def main():
    global OWNERS_BLACKLIST
    global OWNER_CACHE
    global SENT_GIFTS

    validate_config()

    OWNERS_BLACKLIST = load_owner_blacklist()
    OWNER_CACHE = load_owner_cache()
    SENT_GIFTS = load_sent_gifts()

    load_monitor_settings()

    log.info(
        "Loaded: blacklist=%s | owner_cache=%s | sent_gifts=%s",
        len(OWNERS_BLACKLIST),
        len(OWNER_CACHE),
        len(SENT_GIFTS),
    )

    await ensure_user_client_connected()

    if await is_user_client_authorized():
        me = await user_client.get_me()

        log.info(
            "Telethon signed in as %s | @%s | id=%s",
            me.first_name,
            me.username,
            me.id,
        )

        try:
            await ensure_models_loaded()

            log.info(
                "Models loaded: %s",
                len(BASE_GIFTS),
            )

        except Exception as e:
            log.error(
                "Models load error: %s",
                e,
            )

        if (
            MONITOR_SETTINGS.get("enabled")
            and get_target_chat_id()
        ):
            await start_monitor()

    else:
        log.info(
            "Telethon not authorized. "
            "Admin must run /add_session"
        )

        try:
            await bot.send_message(
                ADMIN_ID,
                "🚨 <b>СЕССИЯ НЕ АКТИВНА</b>\n\n"
                "Используйте /add_session",
                parse_mode="HTML",
            )
        except Exception as e:
            log.error(
                "Failed to send session alert: %s",
                e,
            )

    asyncio.create_task(
        session_monitor()
    )

    try:
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            log.warning(
                "Active webhook found (%s). Deleting it before polling...",
                webhook_info.url,
            )
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.error("Failed to delete webhook before polling: %s", e)
        raise

    log.info("Bot polling started.")

    try:
        await dp.start_polling(bot)
    finally:
        await stop_monitor()

        if user_client.is_connected():
            await user_client.disconnect()

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
