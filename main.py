import asyncio
import datetime
import logging
import os
import random
import re
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroupimport asyncio
import datetime
import logging
import os
import random
import re
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    FSInputFile,
    InputMediaPhoto,
)
from aiohttp import ClientSession, web
import asyncpg
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

admin_env = (os.getenv("ADMIN_ID") or "").strip()
ADMIN_ID = int(admin_env) if admin_env.isdigit() else None

manager_env = (os.getenv("MANAGER_ID") or "").strip()
MANAGER_ID = int(manager_env) if manager_env.isdigit() else None


def is_admin(user_id: int | None) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


def is_manager(user_id: int | None) -> bool:
    return MANAGER_ID is not None and user_id == MANAGER_ID


def is_staff(user_id: int | None) -> bool:
    if user_id is None:
        return False
    allowed = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}
    return bool(allowed) and user_id in allowed


def get_staff_recipients() -> set[int]:
    return {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}


class OrderForm(StatesGroup):
    service = State()
    debt_info = State()
    debt_wallet = State()
    special_task = State()
    country = State()
    target_location = State()
    target_data = State()
    photos = State()
    urgency = State()
    summary = State()
    promo_code = State()
    payment = State()
    receipt = State()


class SupportForm(StatesGroup):
    reason = State()
    photo = State()


class ClientReplyForm(StatesGroup):
    waiting_message = State()


class StaffDirectForm(StatesGroup):
    waiting_reply = State()
    waiting_bill = State()
    waiting_reply_and_bill = State()


# Базы данных в памяти (синхронизируются с PostgreSQL при наличии DATABASE_URL)
USER_ORDERS = {}
USER_LAST_COUNTRY = {}
USER_BILLS = {}
KNOWN_USERS = {}          # user_id -> last_seen
USER_LAST_BROADCAST = {}  # user_id -> last_broadcast
ABANDONED_CHECKOUTS = {}  # user_id -> asyncio.Task
ABANDONED_DEADLINES = {}  # user_id -> datetime.datetime (remind_at)
CARD_DETAILS = []
CARD_POINTERS = {}
CRYPTO_DETAILS = {}
CRYPTO_POINTERS = {}
BANNED_USERS = set()
BANNER_CACHE = {}  # Кэш file_id баннеров для мгновенной загрузки

# Промокоды: одноразовое применение на первый заказ, ручное одобрение стафом
USED_PROMO_USERS = set()         # user_id, которым уже подтверждён и применён промокод
USER_APPLIED_PROMOS = {}       # user_id -> {"code": str, "discount_pct": int, "original_price": float, "discounted_price": float}
PROMO_REQUESTS = {}            # req_id -> dict
PROMO_REQUEST_COUNTER = 0
PROMO_DECIDED_REQUESTS = set()   # req_id уже обработанных заявок (защита от повторного нажатия)

# Трекинг активности клиентов, обращений и диалогов
USER_PROFILES = {}        # user_id -> {"username": str|None, "first_seen": datetime|None, "last_seen": datetime|None, "last_service": str|None, "max_step": str}
USER_STEP_RANK = {}       # user_id -> int
ACTIVE_DIALOGS = {}       # user_id -> {"closed": bool, "task": asyncio.Task|None, "expires_at": datetime|None, "last_staff_id": int|None}
TICKET_COUNTER = 0
TICKETS = {}              # ticket_id -> {"user_id": int, "kind": str, "status": str, "staff_msgs": dict[int, int], "extra_data": dict}
PROBLEM_USERS = {}        # user_id -> {"kind": str, "step": str, "query": str, "data": dict, "updated_at": datetime}
ABANDONED_STAFF_NOTIFIED = set()  # user_id для которых уже отправлено уведомление о неоплаченном заказе
BACK_AFTER_SERVICE_NOTIFIED = set()  # (user_id, service_name) — чтобы не дублировать при повторных «Назад»

# Строгий контроль уведомления о неоплаченном заказе на шаге выбора способа оплаты
ON_PAYMENT_METHOD_STEP = set()    # user_id сейчас на этапе выбора способа оплаты
PAYMENT_STEP_NOTIFIED = set()     # user_id уже получил уведомление в текущем заходе на шаг способа оплаты
PAYMENT_STEP_TASKS = {}           # user_id -> asyncio.Task (15 минут бездействия)

DB_POOL = None
DB_WRITE_QUEUE = None
BOT_INSTANCE: Bot | None = None

router = Router()

STEP_LABELS = {
    "catalog": "1. Каталог услуг",
    "debt_info": "2. Информация о долге",
    "debt_wallet": "2. Кошелек для возврата долга",
    "special_task": "2. Задача особого заказа",
    "country": "3. Выбор страны",
    "target_location": "4. Локация цели",
    "target_data": "5. Данные цели",
    "photos": "6. Фото цели",
    "urgency": "7. Срочность",
    "summary": "8. Сводка заказа",
    "payment": "9. Переход к оплате",
    "payment_method": "10. Выбор способа оплаты / реквизитов",
    "promo": "10. Запросил промокод",
    "receipt": "11. Отправка чека",
    "paid": "12. Оплачено / заявка отправлена",
}

STEP_RANKS = {
    "catalog": 1,
    "debt_info": 2,
    "debt_wallet": 2,
    "special_task": 2,
    "country": 3,
    "target_location": 4,
    "target_data": 5,
    "photos": 6,
    "urgency": 7,
    "summary": 8,
    "payment": 9,
    "payment_method": 10,
    "promo": 10,
    "receipt": 11,
    "paid": 12,
}


async def _push_nav_step(state: FSMContext, step_name: str):
    data = await state.get_data()
    history = list(data.get("nav_history") or [])
    if not history or history[-1] != step_name:
        history.append(step_name)
    await state.update_data(nav_history=history)


async def _pop_nav_step(state: FSMContext, current_step_name: str | None = None) -> str | None:
    data = await state.get_data()
    history = list(data.get("nav_history") or [])
    while history and history[-1] == current_step_name:
        history.pop()
    prev_step = history.pop() if history else None
    await state.update_data(nav_history=history)
    return prev_step


def _format_num(val: float | int) -> str:
    num = float(val)
    if num.is_integer():
        return str(int(num))
    return f"{round(num, 2):.2f}".rstrip("0").rstrip(".")


def _apply_discount_once(base_price: float | int, discount_pct: int) -> float | int:
    base = float(base_price or 0)
    if base <= 0 or discount_pct <= 0:
        return int(base) if base.is_integer() else round(base, 2)
    discounted = base * (100 - discount_pct) / 100.0
    if discounted.is_integer():
        return int(discounted)
    return round(discounted, 2)


def _is_paid_client_order(order_data: dict) -> bool:
    status_low = (order_data.get("status") or "").strip().lower()
    if not status_low:
        return False
    if any(unpaid_mark in status_low for unpaid_mark in ("счет", "счёт", "расчет", "расчёт", "не оплач", "брошен")):
        return False
    paid_marks = (
        "оплачено",
        "оплачен",
        "проверки чека",
        "в работе",
        "выполнен",
        "оформлен",
    )
    return any(mark in status_low for mark in paid_marks)


def _get_client_paid_orders(user_id: int) -> list[dict]:
    raw_orders = USER_ORDERS.get(user_id) or []
    return [o for o in raw_orders if _is_paid_client_order(o)]


def _track_user_step(
    user_id: int,
    step_key: str,
    username: str | None = None,
    service_name: str | None = None,
):
    if user_id in BANNED_USERS:
        return
    now_dt = datetime.datetime.now()
    KNOWN_USERS[user_id] = now_dt
    queue_db(("known_user", user_id, now_dt, username))

    prof = USER_PROFILES.setdefault(
        user_id,
        {
            "username": username,
            "first_seen": now_dt,
            "last_seen": now_dt,
            "last_service": service_name,
            "max_step": STEP_LABELS.get(step_key, step_key),
        },
    )
    if username:
        prof["username"] = username
    if not prof.get("first_seen"):
        prof["first_seen"] = now_dt
    prof["last_seen"] = now_dt
    if service_name:
        prof["last_service"] = service_name

    new_rank = STEP_RANKS.get(step_key, 0)
    old_rank = USER_STEP_RANK.get(user_id, 0)
    if new_rank >= old_rank:
        USER_STEP_RANK[user_id] = new_rank
        prof["max_step"] = STEP_LABELS.get(step_key, step_key)


def _record_problem_user(
    user_id: int,
    kind: str,
    step_label: str,
    query_text: str = "",
    data: dict | None = None,
):
    if user_id in BANNED_USERS:
        return
    clean_data = {}
    if data:
        for k, v in data.items():
            if k in ("bot_msg_id", "invoice_id", "photos", "nav_history") or not v:
                continue
            clean_data[k] = v
    PROBLEM_USERS[user_id] = {
        "kind": kind,
        "step": step_label,
        "query": query_text,
        "data": clean_data,
        "updated_at": datetime.datetime.now(),
    }


def _get_latest_order_status(user_id: int) -> str:
    orders = USER_ORDERS.get(user_id) or []
    if orders:
        return orders[-1].get("status") or "— Нет заказов"
    if user_id in USER_BILLS:
        return "🧾 Выставлен счет (ожидает оплаты)"
    return "— Нет заказов"


def _format_user_summary_block(user_id: int, data: dict | None = None) -> str:
    prof = USER_PROFILES.get(user_id, {})
    uname = prof.get("username")
    user_tag = f"@{uname}" if uname else "без @username"
    first_seen = prof.get("first_seen")
    last_seen = prof.get("last_seen") or KNOWN_USERS.get(user_id)
    first_str = (
        first_seen.strftime("%d.%m.%Y %H:%M")
        if isinstance(first_seen, datetime.datetime)
        else "неизвестно"
    )
    last_str = (
        last_seen.strftime("%d.%m.%Y %H:%M")
        if isinstance(last_seen, datetime.datetime)
        else "сейчас"
    )
    bill = USER_BILLS.get(user_id) or {}
    last_order = USER_ORDERS.get(user_id)[-1] if USER_ORDERS.get(user_id) else {}
    service_val = (
        (data or {}).get("service_name")
        or prof.get("last_service")
        or bill.get("service_name")
        or last_order.get("service_name")
        or "не выбрана"
    )
    max_step = prof.get("max_step") or STEP_LABELS.get("catalog")
    order_status = _get_latest_order_status(user_id)
    orders_count = len(USER_ORDERS.get(user_id, []))

    price_val = (data or {}).get("total_price")
    if price_val in (None, 0):
        price_val = (data or {}).get("service_price") or bill.get("total_price") or last_order.get("total_price")

    lines = [
        "📋 <b>Сводка по клиенту и заказу:</b>",
        f"• Клиент: {user_tag} | ID: <code>{user_id}</code>",
        f"• Услуга: <b>{service_val}</b>",
        f"• Этап: <b>{max_step}</b> | Статус заказа: <b>{order_status}</b>",
        f"• Первый вход: <b>{first_str}</b> | Активность: <b>{last_str}</b>",
        f"• Заказов в истории: <b>{orders_count}</b>",
    ]
    if data:
        if data.get("country"):
            lines.append(f"• Страна: <b>{data.get('country')}</b>")
        if data.get("target_location"):
            lines.append(f"• Локация: {data.get('target_location')}")
        if data.get("target_data"):
            lines.append(f"• Данные цели: {data.get('target_data')}")
        if data.get("debt_info"):
            lines.append(f"• Детали возврата долга: {data.get('debt_info')}")
        if data.get("debt_wallet"):
            lines.append(f"• Кошелек возврата: {data.get('debt_wallet')}")
        if data.get("special_task"):
            lines.append(f"• Детали особого заказа: {data.get('special_task')}")
        if data.get("urgency"):
            lines.append(f"• Срочность: <b>{data.get('urgency')}</b>")
    if price_val and float(price_val) > 0:
        lines.append(f"• Сумма: <b>{_format_num(price_val)}$</b>")
    return "\n".join(lines)


# === ЗАПРЕТ РЕКВИЗИТОВ И ССЫЛОК ДЛЯ МЕНЕДЖЕРА ===
_URL_PATTERN = re.compile(
    r"(?:https?://|www\.|t\.me/|telegram\.me/|telegra\.ph/|[a-zA-Z0-9-]+\.(?:ru|com|net|org|io|me|to|cc|gg|pro|info|biz|xyz|ua|kz|by|uz|md)(?:/[^\s]*)?)",
    re.IGNORECASE,
)
_CRYPTO_PATTERNS = [
    re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b"),
    re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    re.compile(r"\b(?:bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}\b"),
    re.compile(r"\b(?:UQ|EQ|kQ|0:)[a-zA-Z0-9_-]{46,48}\b"),
    re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"),
]
_CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_PHONE_PATTERN = re.compile(
    r"(?:(?:\+|00)?\d{1,3}[\s\-\.]?)?(?:\(?\d{2,4}\)?[\s\-\.]?)?\d{2,4}[\s\-\.]?\d{2,4}(?:[\s\-\.]?\d{2,4})?"
)


def _normalize_for_requisite_check(text: str) -> str:
    collapsed = re.sub(r"[\s\-\._,:/\\|()\[\]]+", "", text)
    return f"{text}\n{collapsed}"


def find_forbidden_requisites_for_manager(text: str) -> str | None:
    if not text:
        return None
    if _URL_PATTERN.search(text):
        return "ссылка"

    combined_text = _normalize_for_requisite_check(text)

    for pat in _CRYPTO_PATTERNS:
        m = pat.search(combined_text)
        if m:
            return f"адрес криптокошелька ({m.group(0)})"

    for m in _CARD_PATTERN.finditer(combined_text):
        digits_only = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits_only) <= 19:
            return f"номер карты ({m.group(0).strip()})"

    for m in _PHONE_PATTERN.finditer(text):
        raw_match = m.group(0).strip()
        digits_only = re.sub(r"\D", "", raw_match)
        has_phone_prefix = raw_match.startswith("+") or bool(re.search(r"[\(\)\-\s]", raw_match))
        if len(digits_only) >= 10 and (has_phone_prefix or len(digits_only) in (10, 11, 12)):
            return f"номер телефона ({raw_match})"

    collapsed_digits = re.sub(r"\D", "", re.sub(r"[\s\-\._,:/\\|()\[\]]+", "", text))
    if 13 <= len(collapsed_digits) <= 19:
        return f"номер карты ({collapsed_digits})"
    if len(collapsed_digits) >= 10 and ("+" in text or "тел" in text.lower() or "phone" in text.lower()):
        return f"номер телефона ({collapsed_digits})"

    return None


def _resolve_target_user_id(raw_target: str) -> int | None:
    cleaned = raw_target.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)
    uname = cleaned.lstrip("@").strip().lower()
    if not uname:
        return None
    for uid, prof in USER_PROFILES.items():
        prof_uname = (prof.get("username") or "").strip().lower()
        if prof_uname and prof_uname == uname:
            return int(uid)
    return None


def _cancel_payment_step_timer(user_id: int):
    task = PAYMENT_STEP_TASKS.pop(user_id, None)
    if task:
        task.cancel()


def _reset_payment_step_tracking(user_id: int, leave_step: bool = True):
    _cancel_payment_step_timer(user_id)
    if leave_step:
        ON_PAYMENT_METHOD_STEP.discard(user_id)
        PAYMENT_STEP_NOTIFIED.discard(user_id)


async def _send_unpaid_payment_step_notice(bot: Bot, user_id: int, reason_text: str):
    if user_id in BANNED_USERS:
        return
    if user_id in PAYMENT_STEP_NOTIFIED:
        return
    PAYMENT_STEP_NOTIFIED.add(user_id)
    ON_PAYMENT_METHOD_STEP.discard(user_id)
    _cancel_payment_step_timer(user_id)

    prof = USER_PROFILES.get(user_id, {})
    user_tag = f"@{prof.get('username')}" if prof.get("username") else "без @username"
    bill = USER_BILLS.get(user_id) or {}
    extra = {}
    if prof.get("last_service"):
        extra["service_name"] = prof["last_service"]
    if bill.get("service_name"):
        extra["service_name"] = bill["service_name"]
    if bill.get("total_price"):
        extra["total_price"] = bill["total_price"]
    if bill.get("country"):
        extra["country"] = bill["country"]

    _record_problem_user(
        user_id=user_id,
        kind="unpaid_checkout",
        step_label=STEP_LABELS["payment"],
        query_text=reason_text,
        data=extra,
    )

    body = (
        f"• Клиент: {user_tag} (ID: <code>{user_id}</code>)\n"
        f"• Этап: <b>{STEP_LABELS['payment']}</b>\n"
        f"• Причина: <b>{reason_text}</b>"
    )
    await _send_staff_ticket_menu(
        bot=bot,
        user_id=user_id,
        kind="unpaid_checkout",
        header_title="⚠️ <b>Неоплаченный заказ — требуется ведение клиента</b>",
        body_text=body,
        extra_data=extra,
    )


async def _payment_step_inactivity_worker(bot: Bot, user_id: int):
    try:
        await asyncio.sleep(15 * 60)
        PAYMENT_STEP_TASKS.pop(user_id, None)
        if user_id in BANNED_USERS:
            return
        if user_id not in ON_PAYMENT_METHOD_STEP or user_id in PAYMENT_STEP_NOTIFIED:
            return
        await _send_unpaid_payment_step_notice(
            bot=bot,
            user_id=user_id,
            reason_text="15 минут бездействия на этапе выбора способа оплаты",
        )
    except asyncio.CancelledError:
        pass


def _enter_payment_method_step(bot: Bot, user_id: int):
    if user_id in BANNED_USERS:
        return
    _cancel_payment_step_timer(user_id)
    ON_PAYMENT_METHOD_STEP.add(user_id)
    PAYMENT_STEP_NOTIFIED.discard(user_id)
    PAYMENT_STEP_TASKS[user_id] = asyncio.create_task(
        _payment_step_inactivity_worker(bot, user_id)
    )


def _schedule_abandoned_reminder(bot: Bot, user_id: int, remind_at: datetime.datetime):
    if user_id in BANNED_USERS:
        return
    old_task = ABANDONED_CHECKOUTS.pop(user_id, None)
    if old_task:
        old_task.cancel()
    ABANDONED_DEADLINES[user_id] = remind_at
    ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(
        delayed_abandoned_cart_reminder(bot, user_id, remind_at)
    )


def _cancel_abandoned_reminder(user_id: int):
    task = ABANDONED_CHECKOUTS.pop(user_id, None)
    if task:
        task.cancel()
    ABANDONED_DEADLINES.pop(user_id, None)
    ABANDONED_STAFF_NOTIFIED.discard(user_id)
    queue_db(("abandoned_delete", user_id))


def _clear_back_after_service_flags(user_id: int):
    for key in [k for k in BACK_AFTER_SERVICE_NOTIFIED if k[0] == user_id]:
        BACK_AFTER_SERVICE_NOTIFIED.discard(key)


def _has_active_unpaid_item(user_id: int) -> bool:
    if user_id in BANNED_USERS:
        return False
    if user_id in USER_BILLS:
        return True
    if user_id in ABANDONED_CHECKOUTS or user_id in ABANDONED_DEADLINES:
        return True
    orders = USER_ORDERS.get(user_id) or []
    if orders:
        last_status = (orders[-1].get("status") or "").lower()
        if any(marker in last_status for marker in ("счет", "счёт", "ожидает", "не оплач", "брошен")):
            return True
    return False


# === АВТОЗАКРЫТИЕ ОБРАЩЕНИЯ У КЛИЕНТА ЧЕРЕЗ 1 ЧАС ===
def _cancel_dialog_timer(user_id: int):
    dlg = ACTIVE_DIALOGS.get(user_id)
    if dlg and dlg.get("task"):
        dlg["task"].cancel()
        dlg["task"] = None


def _close_dialog_state(user_id: int):
    _cancel_dialog_timer(user_id)
    ACTIVE_DIALOGS[user_id] = {
        "closed": True,
        "task": None,
        "expires_at": None,
        "last_staff_id": None,
    }


async def _auto_close_dialog_after_hour(bot: Bot, user_id: int, expires_at: datetime.datetime):
    try:
        delay = max(0.0, (expires_at - datetime.datetime.now()).total_seconds())
        if delay > 0:
            await asyncio.sleep(delay)

        if user_id in BANNED_USERS:
            _close_dialog_state(user_id)
            return

        dlg = ACTIVE_DIALOGS.get(user_id)
        if not dlg or dlg.get("closed"):
            return
        if dlg.get("expires_at") and dlg["expires_at"] > datetime.datetime.now():
            return

        dlg["closed"] = True
        dlg["task"] = None

        try:
            await bot.send_message(
                chat_id=user_id,
                text="⏳ <b>Обращение автоматически закрыто по истечении 1 часа.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    except asyncio.CancelledError:
        pass


def _arm_dialog_timer(bot: Bot, user_id: int, staff_id: int | None = None):
    if user_id in BANNED_USERS:
        return
    _cancel_dialog_timer(user_id)
    expires_at = datetime.datetime.now() + datetime.timedelta(hours=1)
    task = asyncio.create_task(_auto_close_dialog_after_hour(bot, user_id, expires_at))
    ACTIVE_DIALOGS[user_id] = {
        "closed": False,
        "task": task,
        "expires_at": expires_at,
        "last_staff_id": staff_id,
    }


def _build_client_send_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️💬 Ответить менеджеру",
                    callback_data="client_reply_manager",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🔒🏠 Закрыть обращение",
                    callback_data="client_close_dialog",
                    style="danger",
                ),
            ]
        ]
    )


async def _deliver_send_message_to_client(
    bot: Bot,
    target_user_id: int,
    text_to_send: str,
    prefix: str = "💬 <b>Сообщение от менеджера:</b>",
    staff_id: int | None = None,
) -> tuple[bool, str]:
    if target_user_id in BANNED_USERS:
        return False, "Пользователь заблокирован"
    payload = f"{prefix}\n\n{text_to_send}"
    keyboard = _build_client_send_keyboard()
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=payload,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        _arm_dialog_timer(bot, target_user_id, staff_id=staff_id)
        return True, ""
    except Exception as first_err:
        logger.warning(f"Повторная попытка отправки клиенту {target_user_id} после ошибки: {first_err}")
        await asyncio.sleep(0.4)
        try:
            await bot.send_message(
                chat_id=target_user_id,
                text=payload,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            _arm_dialog_timer(bot, target_user_id, staff_id=staff_id)
            return True, ""
        except Exception as second_err:
            return False, str(second_err)


# === ВЫСТАВЛЕНИЕ СЧЕТА КЛИЕНТУ (ИСПОЛЬЗУЕТСЯ КОМАНДОЙ /bill И КНОПКАМИ СТАФА) ===
async def _issue_bill_to_client(
    bot: Bot,
    caller_id: int,
    caller_state: FSMContext,
    target_user_id: int,
    service_name: str,
    amount_usd: float,
) -> tuple[bool, str]:
    if target_user_id in BANNED_USERS:
        return False, "Пользователь заблокирован"

    amount_val = (
        int(amount_usd) if amount_usd.is_integer() else round(amount_usd, 2)
    )
    applied_promo = USER_APPLIED_PROMOS.get(target_user_id)
    effective_price = amount_val
    original_price = amount_val
    discount_pct = 0
    if applied_promo and not _get_client_paid_orders(target_user_id):
        discount_pct = int(applied_promo.get("discount_pct", 0) or 0)
        if discount_pct > 0:
            effective_price = _apply_discount_once(amount_val, discount_pct)
            applied_promo["original_price"] = original_price
            applied_promo["discounted_price"] = effective_price

    amount_str = _format_num(effective_price)
    country = USER_LAST_COUNTRY.get(target_user_id, "Россия")
    curr_info = COUNTRY_CURRENCIES.get(
        country, {"code": "USD", "symbol": "USD"}
    )
    rate = await get_exchange_rate(curr_info["code"])
    local_amount = round(float(effective_price) * rate)

    USER_BILLS[target_user_id] = {
        "service_name": service_name,
        "total_price": effective_price,
        "original_total_price": original_price,
        "promo_discount_pct": discount_pct,
        "country": country,
        "curr_code": curr_info["code"],
        "curr_symbol": curr_info["symbol"],
    }
    if caller_id == target_user_id:
        await caller_state.update_data(
            service_name=service_name,
            total_price=effective_price,
            original_total_price=original_price,
            promo_discount_pct=discount_pct,
            country=country,
            curr_code=curr_info["code"],
            curr_symbol=curr_info["symbol"],
        )

    if discount_pct > 0:
        sum_line = (
            f"Сумма: <s>{_format_num(original_price)}$</s> → <b>{amount_str}$</b> "
            f"<b>(-{discount_pct}%)</b> <i>(≈ {local_amount:,} {curr_info['symbol']})</i>"
        )
    else:
        sum_line = (
            f"Сумма: <b>{amount_str}$</b> <i>(≈ {local_amount:,}"
            f" {curr_info['symbol']})</i>"
        )

    bill_text = (
        "⏳ <b>Оплата заказа</b>\n"
        "Время на завершение: <b><u>20:00</u></b>\n\n"
        f"🧾 <b>Счет на {service_name}</b>\n"
        f"💳 <b>Способ оплаты ({country})</b>\n"
        f"{sum_line}"
    )

    remind_at = datetime.datetime.now() + datetime.timedelta(minutes=30)
    queue_db(("abandoned_set", target_user_id, remind_at))
    _schedule_abandoned_reminder(bot, target_user_id, remind_at)

    sent_bill = None
    last_err = ""
    for _ in range(2):
        try:
            banner_name = "payment.jpg"
            banner_path = get_banner_path(banner_name)
            if banner_name in BANNER_CACHE:
                sent_bill = await bot.send_photo(
                    chat_id=target_user_id,
                    photo=BANNER_CACHE[banner_name],
                    caption=bill_text,
                    reply_markup=build_payment_keyboard(country),
                    parse_mode="HTML",
                )
            elif banner_path and os.path.exists(banner_path):
                sent_bill = await bot.send_photo(
                    chat_id=target_user_id,
                    photo=FSInputFile(banner_path),
                    caption=bill_text,
                    reply_markup=build_payment_keyboard(country),
                    parse_mode="HTML",
                )
                if sent_bill.photo:
                    BANNER_CACHE[banner_name] = sent_bill.photo[-1].file_id
            else:
                sent_bill = await bot.send_message(
                    chat_id=target_user_id,
                    text=bill_text,
                    reply_markup=build_payment_keyboard(country),
                    parse_mode="HTML",
                )
            last_err = ""
            break
        except Exception as e:
            last_err = str(e)
            await asyncio.sleep(0.4)

    if sent_bill is not None:
        USER_BILLS[target_user_id]["bill_msg_id"] = sent_bill.message_id
        queue_db(("bill_set", target_user_id, dict(USER_BILLS[target_user_id])))
        if caller_id == target_user_id:
            await caller_state.update_data(bot_msg_id=sent_bill.message_id)
        _arm_dialog_timer(bot, target_user_id, staff_id=caller_id)
        return True, (
            f"✅ Счет на <b>{service_name}</b> (<b>{amount_str}$</b>) отправлен"
            f" пользователю <code>{target_user_id}</code> (страна:"
            f" <b>{country}</b>, валюта: <b>{curr_info['symbol']}</b>)."
        )
    return False, f"❌ Не удалось отправить счет пользователю <code>{target_user_id}</code>: {last_err}"


def _parse_bill_input(raw_text: str, default_service: str = "Особый заказ") -> tuple[str, float] | tuple[None, str]:
    parts = raw_text.strip().split()
    if not parts:
        return None, "❌ Укажите услугу и сумму. Пример: <code>особый заказ 220$</code>"

    raw_amount = (
        parts[-1]
        .replace("$", "")
        .replace("usd", "")
        .replace("USD", "")
        .replace(",", ".")
        .strip()
    )
    try:
        amount_usd = float(raw_amount)
        if amount_usd <= 0:
            raise ValueError()
    except ValueError:
        return None, "❌ Некорректная сумма. Пример: <code>особый заказ 220$</code>"

    service_name = " ".join(parts[:-1]).strip() if len(parts) > 1 else default_service
    if not service_name:
        service_name = default_service
    return service_name, amount_usd


def _build_staff_action_guide(mode: str, target_user_id: int) -> str:
    if mode == "reply":
        return (
            f"✍️ <b>Режим ответа клиенту (ID: <code>{target_user_id}</code>)</b>\n\n"
            "<b>Как заполнить:</b> отправьте следующим сообщением текст ответа клиенту.\n"
            "• Можно задать уточнения, согласовать детали или направить клиента.\n"
            "• Для отмены нажмите кнопку <b>«Отмена»</b> ниже.\n\n"
            "<i>Шпаргалка ручных команд:</i>\n"
            f"• Сообщение: <code>/send {target_user_id} текст</code>\n"
            f"• Счет: <code>/bill {target_user_id} услуга 220$</code>"
        )
    if mode == "bill":
        return (
            f"🧾 <b>Выставление счета клиенту (ID: <code>{target_user_id}</code>)</b>\n\n"
            "<b>Как заполнить:</b> отправьте одним сообщением <code>&lt;услуга&gt; &lt;сумма$&gt;</code>\n"
            "• Пример: <code>особый заказ 220$</code> или <code>Избить 160$</code>\n"
            "• Валюта страны клиента и примерный курс подставятся автоматически.\n"
            "• Для отмены нажмите кнопку <b>«Отмена»</b> ниже.\n\n"
            "<i>Шпаргалка ручных команд:</i>\n"
            f"• Счет: <code>/bill {target_user_id} особый заказ 220$</code>\n"
            f"• Сообщение: <code>/send {target_user_id} текст</code>"
        )
    return (
        f"💬🧾 <b>Ответ и выставление счета (ID: <code>{target_user_id}</code>)</b>\n\n"
        "<b>Как заполнить:</b> отправьте сообщение в формате:\n"
        "<code>Текст сообщения клиенту | услуга сумма$</code>\n"
        "• Пример: <code>Расчет готов, можно оплачивать. | особый заказ 220$</code>\n"
        "• Клиент получит ваш текст и сразу следом — выставленный счет.\n"
        "• Для отмены нажмите кнопку <b>«Отмена»</b> ниже.\n\n"
        "<i>Шпаргалка ручных команд:</i>\n"
        f"• Сообщение: <code>/send {target_user_id} текст</code>\n"
        f"• Счет: <code>/bill {target_user_id} особый заказ 220$</code>"
    )


def _build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="staff_cancel_mode",
                style="danger",
            )
        ]]
    )


# === МЕНЮШКИ ОБРАЩЕНИЙ И ЗАКАЗОВ ДЛЯ МЕНЕДЖЕРА / АДМИНА ===
def _register_ticket(user_id: int, kind: str, extra_data: dict | None = None) -> int:
    global TICKET_COUNTER
    TICKET_COUNTER += 1
    tid = TICKET_COUNTER
    TICKETS[tid] = {
        "user_id": user_id,
        "kind": kind,
        "status": "🟢 Открыто",
        "staff_msgs": {},
        "extra_data": dict(extra_data or {}),
    }
    return tid


def _build_ticket_keyboard(ticket_id: int, user_id: int, kind: str, closed: bool = False) -> InlineKeyboardMarkup:
    if closed:
        return InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="🔒 Обращение закрыто",
                    callback_data=f"tkt_noop_{ticket_id}",
                )
            ]]
        )
    if kind == "payment_help":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Написать клиенту",
                        callback_data=f"tkt_reply_{ticket_id}",
                        style="primary",
                    ),
                    InlineKeyboardButton(
                        text="🔒 Закрыть обращение",
                        callback_data=f"tkt_close_{ticket_id}",
                        style="danger",
                    ),
                ]
            ]
        )
    if kind == "support":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Ответить клиенту",
                        callback_data=f"tkt_reply_{ticket_id}",
                        style="primary",
                    ),
                    InlineKeyboardButton(
                        text="🧾 Выставить счёт",
                        callback_data=f"tkt_bill_{ticket_id}",
                        style="primary",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔒 Закрыть обращение",
                        callback_data=f"tkt_close_{ticket_id}",
                        style="danger",
                    )
                ],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Ответить клиенту",
                    callback_data=f"tkt_reply_{ticket_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🧾 Выставить счёт",
                    callback_data=f"tkt_bill_{ticket_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬🧾 Ответить и выставить счёт",
                    callback_data=f"tkt_replybill_{ticket_id}",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ В работе",
                    callback_data=f"tkt_work_{ticket_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="✅ Выполнен",
                    callback_data=f"tkt_done_{ticket_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Закрыть обращение",
                    callback_data=f"tkt_close_{ticket_id}",
                    style="danger",
                )
            ],
        ]
    )


def _build_paid_order_keyboard(
    user_id: int,
    order_index: int | None = None,
    in_work: bool = False,
    is_done: bool = False,
) -> InlineKeyboardMarkup:
    idx_suffix = f"_{order_index}" if order_index is not None else ""
    work_text = "⚙️ В работе ✓" if in_work else "⚙️ В работе"
    done_text = "✅ Выполнен ✓" if is_done else "✅ Выполнен"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Ответить клиенту",
                    callback_data=f"order_msg_{user_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🧾 Выставить счёт",
                    callback_data=f"order_bill_{user_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬🧾 Ответить и выставить счёт",
                    callback_data=f"order_replybill_{user_id}",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text=work_text,
                    callback_data=f"order_work_{user_id}{idx_suffix}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text=done_text,
                    callback_data=f"order_done_{user_id}{idx_suffix}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Закрыть обращение",
                    callback_data=f"order_close_{user_id}",
                    style="danger",
                )
            ],
        ]
    )


async def _send_staff_ticket_menu(
    bot: Bot,
    user_id: int,
    kind: str,
    header_title: str,
    body_text: str,
    extra_data: dict | None = None,
    photo_id: str | None = None,
    document_id: str | None = None,
):
    if user_id in BANNED_USERS:
        return
    tid = _register_ticket(user_id, kind, extra_data=extra_data)
    summary = _format_user_summary_block(user_id, extra_data)
    full_text = (
        f"{header_title}\n"
        f"🗂 Обращение #{tid} | Статус: <b>🟢 Открыто</b>\n\n"
        f"{body_text}\n\n"
        f"{summary}"
    )
    kb = _build_ticket_keyboard(tid, user_id=user_id, kind=kind, closed=False)

    for staff_id in get_staff_recipients():
        try:
            if photo_id:
                sent = await bot.send_photo(
                    chat_id=staff_id,
                    photo=photo_id,
                    caption=full_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            elif document_id:
                sent = await bot.send_document(
                    chat_id=staff_id,
                    document=document_id,
                    caption=full_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            else:
                sent = await bot.send_message(
                    chat_id=staff_id,
                    text=full_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            TICKETS[tid]["staff_msgs"][staff_id] = sent.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки карточки обращения #{tid} сотруднику {staff_id}: {e}")


# === POSTGRESQL СЛОЙ ===
async def init_db():
    global DB_POOL, DB_WRITE_QUEUE
    if not DATABASE_URL:
        logger.warning(
            "⚠️ DATABASE_URL не задан — бот работает только в оперативной памяти."
        )
        return

    dsn = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    DB_POOL = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)

    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS card_details (
                id SERIAL PRIMARY KEY,
                details TEXT NOT NULL,
                bank TEXT NOT NULL,
                country TEXT NOT NULL,
                frozen_until TIMESTAMP NULL
            );
            CREATE TABLE IF NOT EXISTS crypto_details (
                id SERIAL PRIMARY KEY,
                network_key TEXT NOT NULL,
                address TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id BIGINT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS known_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT NULL,
                last_seen TIMESTAMP NULL,
                last_broadcast TIMESTAMP NULL
            );
            CREATE TABLE IF NOT EXISTS user_last_country (
                user_id BIGINT PRIMARY KEY,
                country TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                service_name TEXT NOT NULL,
                total_price INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_bills (
                user_id BIGINT PRIMARY KEY,
                service_name TEXT NOT NULL,
                total_price INTEGER NOT NULL,
                country TEXT NOT NULL,
                curr_code TEXT NOT NULL,
                curr_symbol TEXT NOT NULL,
                bill_msg_id BIGINT NULL
            );
            CREATE TABLE IF NOT EXISTS abandoned_checkouts (
                user_id BIGINT PRIMARY KEY,
                remind_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS used_promo_users (
                user_id BIGINT PRIMARY KEY,
                promo_code TEXT NOT NULL,
                discount_pct INTEGER NOT NULL
            );
        """)

        await conn.execute("""
            ALTER TABLE known_users
            ADD COLUMN IF NOT EXISTS last_broadcast TIMESTAMP NULL
        """)
        await conn.execute("""
            ALTER TABLE known_users
            ADD COLUMN IF NOT EXISTS username TEXT NULL
        """)

        pk_col = await conn.fetchval("""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'crypto_details'::regclass AND i.indisprimary
            LIMIT 1
        """)
        has_id_col = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'crypto_details' AND column_name = 'id'
            )
        """)
        if pk_col == "network_key" or not has_id_col:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS crypto_details_new (
                    id SERIAL PRIMARY KEY,
                    network_key TEXT NOT NULL,
                    address TEXT NOT NULL
                );
                INSERT INTO crypto_details_new (network_key, address)
                SELECT network_key, address FROM crypto_details;
                DROP TABLE crypto_details;
                ALTER TABLE crypto_details_new RENAME TO crypto_details;
            """)

        CARD_DETAILS.clear()
        for row in await conn.fetch(
            "SELECT details, bank, country, frozen_until FROM card_details ORDER BY id ASC"
        ):
            CARD_DETAILS.append({
                "details": row["details"],
                "bank": row["bank"],
                "country": row["country"],
                "frozen_until": row["frozen_until"],
            })

        CRYPTO_DETAILS.clear()
        for row in await conn.fetch(
            "SELECT network_key, address FROM crypto_details ORDER BY id ASC"
        ):
            CRYPTO_DETAILS.setdefault(row["network_key"], []).append(row["address"])

        BANNED_USERS.clear()
        for row in await conn.fetch("SELECT user_id FROM banned_users"):
            BANNED_USERS.add(int(row["user_id"]))

        USED_PROMO_USERS.clear()
        for row in await conn.fetch("SELECT user_id, promo_code, discount_pct FROM used_promo_users"):
            uid = int(row["user_id"])
            USED_PROMO_USERS.add(uid)
            USER_APPLIED_PROMOS.setdefault(
                uid,
                {
                    "code": row["promo_code"],
                    "discount_pct": int(row["discount_pct"]),
                    "original_price": 0,
                    "discounted_price": 0,
                },
            )

        KNOWN_USERS.clear()
        USER_LAST_BROADCAST.clear()
        for row in await conn.fetch(
            "SELECT user_id, username, last_seen, last_broadcast FROM known_users"
        ):
            uid = int(row["user_id"])
            uname = row["username"]
            KNOWN_USERS[uid] = row["last_seen"]
            USER_PROFILES.setdefault(
                uid,
                {
                    "username": uname,
                    "first_seen": row["last_seen"],
                    "last_seen": row["last_seen"],
                    "last_service": None,
                    "max_step": STEP_LABELS.get("catalog"),
                },
            )
            if uname:
                USER_PROFILES[uid]["username"] = uname
            if row["last_broadcast"] is not None:
                USER_LAST_BROADCAST[uid] = row["last_broadcast"]

        USER_LAST_COUNTRY.clear()
        for row in await conn.fetch(
            "SELECT user_id, country FROM user_last_country"
        ):
            USER_LAST_COUNTRY[int(row["user_id"])] = row["country"]

        USER_ORDERS.clear()
        for row in await conn.fetch(
            "SELECT user_id, service_name, total_price, status FROM user_orders ORDER BY id ASC"
        ):
            uid = int(row["user_id"])
            USER_ORDERS.setdefault(uid, []).append({
                "service_name": row["service_name"],
                "total_price": row["total_price"],
                "status": row["status"],
            })

        USER_BILLS.clear()
        for row in await conn.fetch("""
            SELECT user_id, service_name, total_price, country, curr_code, curr_symbol, bill_msg_id
            FROM user_bills
        """):
            uid = int(row["user_id"])
            USER_BILLS[uid] = {
                "service_name": row["service_name"],
                "total_price": row["total_price"],
                "country": row["country"],
                "curr_code": row["curr_code"],
                "curr_symbol": row["curr_symbol"],
                "bill_msg_id": row["bill_msg_id"],
            }

        ABANDONED_DEADLINES.clear()
        for row in await conn.fetch(
            "SELECT user_id, remind_at FROM abandoned_checkouts"
        ):
            ABANDONED_DEADLINES[int(row["user_id"])] = row["remind_at"]

    DB_WRITE_QUEUE = asyncio.Queue()
    asyncio.create_task(db_writer_worker())
    logger.info("✅ PostgreSQL подключен и синхронизирован.")


async def _exec_db_action(action: tuple):
    if not DB_POOL:
        return
    kind = action[0]
    try:
        async with DB_POOL.acquire() as conn:
            if kind == "ban_add":
                _, user_id = action
                await conn.execute(
                    "INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    user_id,
                )
            elif kind == "ban_remove":
                _, user_id = action
                await conn.execute(
                    "DELETE FROM banned_users WHERE user_id = $1",
                    user_id,
                )
            elif kind == "card_add":
                _, details, bank, country = action
                await conn.execute(
                    "INSERT INTO card_details (details, bank, country, frozen_until) VALUES ($1, $2, $3, NULL)",
                    details,
                    bank,
                    country,
                )
            elif kind == "card_delete":
                _, details, bank, country = action
                await conn.execute(
                    "DELETE FROM card_details WHERE details = $1 AND bank = $2 AND LOWER(country) = LOWER($3)",
                    details,
                    bank,
                    country,
                )
            elif kind == "card_freeze":
                _, details, bank, country, frozen_until = action
                await conn.execute(
                    "UPDATE card_details SET frozen_until = $4 WHERE details = $1 AND bank = $2 AND LOWER(country) = LOWER($3)",
                    details,
                    bank,
                    country,
                    frozen_until,
                )
            elif kind == "crypto_add":
                _, net_key, address = action
                await conn.execute(
                    "INSERT INTO crypto_details (network_key, address) VALUES ($1, $2)",
                    net_key,
                    address,
                )
            elif kind == "crypto_delete_addr":
                _, net_key, address = action
                await conn.execute(
                    "DELETE FROM crypto_details WHERE network_key = $1 AND address = $2",
                    net_key,
                    address,
                )
            elif kind == "crypto_delete":
                _, net_key = action
                await conn.execute(
                    "DELETE FROM crypto_details WHERE network_key = $1",
                    net_key,
                )
            elif kind == "known_user":
                _, user_id, last_seen, username = action[0], action[1], action[2], (action[3] if len(action) > 3 else None)
                await conn.execute(
                    """
                    INSERT INTO known_users (user_id, username, last_seen) VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO UPDATE SET
                        last_seen = EXCLUDED.last_seen,
                        username = COALESCE(EXCLUDED.username, known_users.username)
                    """,
                    user_id,
                    username,
                    last_seen,
                )
            elif kind == "last_broadcast":
                _, user_id, last_broadcast = action
                await conn.execute(
                    """
                    INSERT INTO known_users (user_id, last_seen, last_broadcast) VALUES ($1, $2, $2)
                    ON CONFLICT (user_id) DO UPDATE SET last_broadcast = EXCLUDED.last_broadcast
                    """,
                    user_id,
                    last_broadcast,
                )
            elif kind == "last_country":
                _, user_id, country = action
                await conn.execute(
                    """
                    INSERT INTO user_last_country (user_id, country) VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE SET country = EXCLUDED.country
                    """,
                    user_id,
                    country,
                )
            elif kind == "order_add":
                _, user_id, service_name, total_price, status = action
                await conn.execute(
                    "INSERT INTO user_orders (user_id, service_name, total_price, status) VALUES ($1, $2, $3, $4)",
                    user_id,
                    service_name,
                    int(round(float(total_price or 0))),
                    status,
                )
            elif kind == "order_set_last_status":
                _, user_id, status = action
                row = await conn.fetchrow(
                    "SELECT id FROM user_orders WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
                    user_id,
                )
                if row:
                    await conn.execute(
                        "UPDATE user_orders SET status = $1 WHERE id = $2",
                        status,
                        row["id"],
                    )
            elif kind == "order_set_index_status":
                _, user_id, order_idx, status = action
                rows = await conn.fetch(
                    "SELECT id FROM user_orders WHERE user_id = $1 ORDER BY id ASC",
                    user_id,
                )
                if 0 <= order_idx < len(rows):
                    await conn.execute(
                        "UPDATE user_orders SET status = $1 WHERE id = $2",
                        status,
                        rows[order_idx]["id"],
                    )
            elif kind == "bill_set":
                _, user_id, bill_data = action
                await conn.execute(
                    """
                    INSERT INTO user_bills (user_id, service_name, total_price, country, curr_code, curr_symbol, bill_msg_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (user_id) DO UPDATE SET
                        service_name = EXCLUDED.service_name,
                        total_price = EXCLUDED.total_price,
                        country = EXCLUDED.country,
                        curr_code = EXCLUDED.curr_code,
                        curr_symbol = EXCLUDED.curr_symbol,
                        bill_msg_id = EXCLUDED.bill_msg_id
                    """,
                    user_id,
                    bill_data.get("service_name", "Услуга"),
                    int(round(float(bill_data.get("total_price", 0) or 0))),
                    bill_data.get("country", "Россия"),
                    bill_data.get("curr_code", "RUB"),
                    bill_data.get("curr_symbol", "₽"),
                    bill_data.get("bill_msg_id"),
                )
            elif kind == "bill_delete":
                _, user_id = action
                await conn.execute(
                    "DELETE FROM user_bills WHERE user_id = $1",
                    user_id,
                )
            elif kind == "abandoned_set":
                _, user_id, remind_at = action
                await conn.execute(
                    """
                    INSERT INTO abandoned_checkouts (user_id, remind_at) VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE SET remind_at = EXCLUDED.remind_at
                    """,
                    user_id,
                    remind_at,
                )
            elif kind == "abandoned_delete":
                _, user_id = action
                await conn.execute(
                    "DELETE FROM abandoned_checkouts WHERE user_id = $1",
                    user_id,
                )
            elif kind == "promo_used_add":
                _, user_id, promo_code, discount_pct = action
                await conn.execute(
                    """
                    INSERT INTO used_promo_users (user_id, promo_code, discount_pct)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    user_id,
                    promo_code,
                    int(discount_pct),
                )
    except Exception as e:
        logger.error(f"Ошибка записи в PostgreSQL ({kind}): {e}")


async def db_writer_worker():
    while True:
        action = await DB_WRITE_QUEUE.get()
        try:
            await _exec_db_action(action)
        finally:
            DB_WRITE_QUEUE.task_done()


def queue_db(action: tuple):
    if not DATABASE_URL:
        return
    if DB_WRITE_QUEUE is not None:
        DB_WRITE_QUEUE.put_nowait(action)
    else:
        asyncio.create_task(_exec_db_action(action))


def _touch_user_activity(user_id: int, username: str | None = None):
    if user_id in BANNED_USERS:
        return
    now_dt = datetime.datetime.now()
    KNOWN_USERS[user_id] = now_dt
    queue_db(("known_user", user_id, now_dt, username))

    prof = USER_PROFILES.setdefault(
        user_id,
        {
            "username": username,
            "first_seen": now_dt,
            "last_seen": now_dt,
            "last_service": None,
            "max_step": STEP_LABELS.get("catalog"),
        },
    )
    if username:
        prof["username"] = username
    if not prof.get("first_seen"):
        prof["first_seen"] = now_dt
    prof["last_seen"] = now_dt


# === MIDDLEWARE ДЛЯ ПРОВЕРКИ БАНА ===
@router.message.middleware()
async def ban_middleware(handler, event, data):
    if event.from_user and event.from_user.id in BANNED_USERS:
        return
    return await handler(event, data)


@router.callback_query.middleware()
async def ban_callback_middleware(handler, event, data):
    if event.from_user and event.from_user.id in BANNED_USERS:
        return
    return await handler(event, data)


# === СЛОВАРЬ ВАЛЮТ ДЛЯ СТРАН (БЕЗ КЫРГЫЗСТАНА, АЗЕРБАЙДЖАНА, АРМЕНИИ, ГРУЗИИ) ===
COUNTRY_CURRENCIES = {
    "Россия": {"code": "RUB", "symbol": "₽"},
    "Беларусь": {"code": "BYN", "symbol": "BYN"},
    "Украина": {"code": "UAH", "symbol": "грн."},
    "Казахстан": {"code": "KZT", "symbol": "₸"},
    "Узбекистан": {"code": "UZS", "symbol": "сум"},
    "Молдова": {"code": "MDL", "symbol": "лей"},
}

# === КРИПТОСЕТИ ===
CRYPTO_NETWORKS = {
    "trc20": {
        "name": "USDT (TRC-20)",
        "network": "TRC-20",
        "keys": ["usdt trc-20", "usdt trc20", "trc-20", "trc20"],
    },
    "bep20": {
        "name": "USDT (BEP-20)",
        "network": "BEP-20",
        "keys": ["usdt bep-20", "usdt bep20", "bep-20", "bep20"],
    },
    "ton": {
        "name": "USDT (TON)",
        "network": "TON",
        "keys": ["usdt ton", "usdt (ton)", "ton"],
    },
    "btc": {
        "name": "Bitcoin (BTC)",
        "network": "Bitcoin",
        "keys": ["bitcoin (btc)", "bitcoin", "btc"],
    },
}


def normalize_crypto_key(raw_network: str):
    cleaned = " ".join(
        raw_network.lower().replace("(", " ").replace(")", " ").split()
    )
    for net_key, info in CRYPTO_NETWORKS.items():
        if cleaned == net_key or cleaned in info["keys"]:
            return net_key
    return None


# === ФУНКЦИЯ ЧЕРЕДОВАНИЯ РЕКВИЗИТОВ ===
def get_next_card(country: str):
    now = datetime.datetime.now()
    active = []
    for card in CARD_DETAILS:
        if card["country"].lower() == country.lower():
            if card["frozen_until"] and card["frozen_until"] > now:
                continue
            elif card["frozen_until"] and card["frozen_until"] <= now:
                card["frozen_until"] = None
                queue_db((
                    "card_freeze",
                    card["details"],
                    card["bank"],
                    card["country"],
                    None,
                ))
            active.append(card)

    if not active:
        return None

    if country not in CARD_POINTERS:
        CARD_POINTERS[country] = 0

    index = CARD_POINTERS[country] % len(active)
    chosen_card = active[index]
    CARD_POINTERS[country] = (index + 1) % len(active)
    return chosen_card


def get_next_crypto(net_key: str):
    addresses = CRYPTO_DETAILS.get(net_key) or []
    if not addresses:
        return None

    if net_key not in CRYPTO_POINTERS:
        CRYPTO_POINTERS[net_key] = 0

    index = CRYPTO_POINTERS[net_key] % len(addresses)
    chosen_address = addresses[index]
    CRYPTO_POINTERS[net_key] = (index + 1) % len(addresses)
    return chosen_address


# === ФУНКЦИЯ ПОЛУЧЕНИЯ КУРСОВ ВАЛЮТ ===
async def get_exchange_rate(target_currency: str) -> float:
    url = "https://open.er-api.com/v6/latest/USD"
    try:
        async with ClientSession() as session:
            async with session.get(url, timeout=2) as response:
                if response.status == 200:
                    data = await response.json()
                    rates = data.get("rates", {})
                    if target_currency in rates:
                        return float(rates[target_currency])
    except Exception as e:
        logger.warning(f"Не удалось получить курс для {target_currency}: {e}")

    fallback_rates = {
        "RUB": 95.0,
        "BYN": 3.3,
        "KZT": 480.0,
        "UAH": 41.5,
        "UZS": 12800.0,
        "MDL": 18.0,
    }
    return fallback_rates.get(target_currency, 1.0)


def build_payment_keyboard(country: str, back_callback: str = "go_to_payment") -> InlineKeyboardMarkup:
    card_btn_text_map = {
        "Россия": "💳 Оплатить на СБП",
        "Казахстан": "💳 Оплатить на Kaspi",
        "Украина": "💳 Оплата картой",
        "Беларусь": "💳 Оплата картой",
        "Узбекистан": "💳 Оплата картой",
        "Молдова": "💳 Оплата картой",
    }
    card_btn_text = card_btn_text_map.get(country, "💳 Оплата картой")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=card_btn_text,
                    callback_data="pay_card_option",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 CryptoBot (без комиссии)",
                    callback_data="pay_cryptobot",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Криптовалюта",
                    callback_data="crypto_expert",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎟 Промокод",
                    callback_data="promo_code_entry",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Нужна помощь",
                    callback_data="payment_help_manager",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=back_callback, style="danger"
                )
            ],
        ]
    )


async def get_payment_context(user_id: int, state: FSMContext):
    data = await state.get_data()
    bill = USER_BILLS.get(user_id, {})
    prof = USER_PROFILES.get(user_id, {})
    country = (
        data.get("country")
        or bill.get("country")
        or USER_LAST_COUNTRY.get(user_id, "Россия")
    )
    curr_info = COUNTRY_CURRENCIES.get(
        country, {"code": "USD", "symbol": "USD"}
    )
    curr_code = (
        data.get("curr_code") or bill.get("curr_code") or curr_info["code"]
    )
    curr_symbol = (
        data.get("curr_symbol") or bill.get("curr_symbol") or curr_info["symbol"]
    )

    if bill:
        raw_price = bill.get("total_price", 250)
        raw_orig = bill.get("original_total_price", raw_price)
        discount_pct = int(bill.get("promo_discount_pct", 0) or 0)
    else:
        raw_price = (
            data.get("total_price") if data.get("total_price") is not None else 250
        )
        raw_orig = (
            data.get("original_total_price")
            if data.get("original_total_price") is not None
            else raw_price
        )
        discount_pct = int(data.get("promo_discount_pct", 0) or 0)

    # Применяем подтверждённый промокод ровно один раз на полную сумму первого заказа
    applied = USER_APPLIED_PROMOS.get(user_id)
    if applied and not _get_client_paid_orders(user_id):
        ap_pct = int(applied.get("discount_pct", 0) or 0)
        if ap_pct > 0 and discount_pct == 0:
            discount_pct = ap_pct
            raw_orig = raw_price
            raw_price = _apply_discount_once(raw_orig, discount_pct)
            applied["original_price"] = raw_orig
            applied["discounted_price"] = raw_price
            await state.update_data(
                original_total_price=raw_orig,
                total_price=raw_price,
                promo_discount_pct=discount_pct,
            )
            if user_id in USER_BILLS:
                USER_BILLS[user_id]["original_total_price"] = raw_orig
                USER_BILLS[user_id]["total_price"] = raw_price
                USER_BILLS[user_id]["promo_discount_pct"] = discount_pct
                queue_db(("bill_set", user_id, dict(USER_BILLS[user_id])))

    total_price = raw_price
    service_name = (
        data.get("service_name")
        or bill.get("service_name")
        or prof.get("last_service")
        or "Услуга"
    )
    enriched_data = dict(data)
    enriched_data["country"] = country
    enriched_data["curr_code"] = curr_code
    enriched_data["curr_symbol"] = curr_symbol
    enriched_data["service_name"] = service_name
    enriched_data["original_total_price"] = raw_orig
    enriched_data["total_price"] = total_price
    enriched_data["promo_discount_pct"] = discount_pct
    return enriched_data, country, curr_code, curr_symbol, total_price, service_name


def _build_order_details_text(
    data: dict, country: str, service_desc: str, total_price
) -> str:
    orig_price = data.get("original_total_price")
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    if disc_pct > 0 and orig_price and float(orig_price) > float(total_price or 0):
        price_val = (
            f"<s>{_format_num(orig_price)}$</s> → <b>{_format_num(total_price)}$</b> "
            f"(-{disc_pct}%)"
        )
    else:
        price_val = (
            f"{_format_num(total_price)}$"
            if total_price and float(total_price) > 0
            else "Расчет менеджера"
        )
    details = f"\n• Услуга: {service_desc}\n• Сумма: {price_val}"
    if country:
        details += f"\n• Страна: {country}"
    if data.get("urgency"):
        details += f"\n• Срочность: {data.get('urgency')}"
    if data.get("target_location"):
        details += f"\n• Локация цели: {data.get('target_location')}"
    if data.get("target_data"):
        details += f"\n• Данные цели: {data.get('target_data')}"
    if data.get("debt_info"):
        details += f"\n• Информация о долге: {data.get('debt_info')}"
    if data.get("debt_wallet"):
        details += f"\n• Кошелек для возврата (USDT/TON): {data.get('debt_wallet')}"
    if data.get("special_task"):
        details += f"\n• Задача (Особый заказ): {data.get('special_task')}"
    return details


# === КЛАВИАТУРА КАТАЛОГА УСЛУГ ===
def build_catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧎 Извинение на коленях — 85$",
                    callback_data="srv_Извинение_на_коленях_85",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🥊 Избить — 160$",
                    callback_data="srv_Избить_160",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💸 Вернуть долг — 230$",
                    callback_data="srv_Вернуть_долг_230",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🦴 Сломать пальцы — 310$",
                    callback_data="srv_Сломать_пальцы_310",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🦾 Сломать конечности — 460$",
                    callback_data="srv_Сломать_конечности_460",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪓 Отрубить конечности — 740$",
                    callback_data="srv_Отрубить_конечности_740",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚡️ Убийство — 4600$",
                    callback_data="srv_Убийство_4600",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Особый заказ — Рассчитать сумму",
                    callback_data="srv_Особый_заказ_0",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📖 О услугах",
                    url="https://telegra.ph/Podrobnyj-gajd-po-uslugam-i-oformleniyu-zakaza-11-20",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="back_home", style="primary"
                )
            ],
        ]
    )


# === РАСЧЕТ РЕПУТАЦИИ ===
def get_reputation_stats():
    current_date = datetime.date.today()
    base_date = datetime.date.today()
    days_passed = max(0, (current_date - base_date).days)
    completed = 5173 + days_passed * 312
    rng = random.Random(current_date.toordinal())
    in_progress = rng.randint(175, 225)
    new_requests = rng.randint(256, 409)
    return completed, in_progress, new_requests


# === ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ПОИСКА БАННЕРА ===
def get_banner_path(banner_name: str):
    base_name = os.path.splitext(banner_name)[0]
    if os.path.exists("banners"):
        for filename in os.listdir("banners"):
            name_without_ext = os.path.splitext(filename)[0]
            if name_without_ext.lower() == base_name.lower():
                return os.path.join("banners", filename)
    return None


# === УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ЦЕЛЬНОГО ОБНОВЛЕНИЯ БАННЕРОВ (С КЕШИРОВАНИЕМ FILE_ID) ===
async def render_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    banner_name: str,
    text: str,
    reply_markup: InlineKeyboardMarkup,
):
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass

    bot = event.bot
    chat_id = (
        event.chat.id if isinstance(event, Message) else event.message.chat.id
    )

    data = await state.get_data()
    bot_msg_id = data.get("bot_msg_id")

    photo_source = None
    banner_path = get_banner_path(banner_name)

    if banner_name in BANNER_CACHE:
        photo_source = BANNER_CACHE[banner_name]
    elif banner_path and os.path.exists(banner_path):
        photo_source = FSInputFile(banner_path)

    if isinstance(event, CallbackQuery):
        msg = event.message
        if photo_source:
            try:
                if msg.photo:
                    sent_msg = await msg.edit_media(
                        media=InputMediaPhoto(
                            media=photo_source, caption=text, parse_mode="HTML"
                        ),
                        reply_markup=reply_markup,
                    )
                else:
                    await msg.delete()
                    sent_msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_source,
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )

                if (
                    not banner_name in BANNER_CACHE
                    and hasattr(sent_msg, "photo")
                    and sent_msg.photo
                ):
                    BANNER_CACHE[banner_name] = sent_msg.photo[-1].file_id
            except Exception:
                try:
                    await msg.delete()
                except Exception:
                    pass
                new_msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_source,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
                if not banner_name in BANNER_CACHE and new_msg.photo:
                    BANNER_CACHE[banner_name] = new_msg.photo[-1].file_id
                await state.update_data(bot_msg_id=new_msg.message_id)
                return
        else:
            try:
                await msg.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
            except Exception:
                try:
                    new_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                    await state.update_data(bot_msg_id=new_msg.message_id)
                except Exception:
                    pass
                return
        await state.update_data(bot_msg_id=msg.message_id)
    else:
        try:
            await event.delete()
        except Exception:
            pass

        if bot_msg_id:
            try:
                if photo_source:
                    sent_msg = await bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=bot_msg_id,
                        media=InputMediaPhoto(
                            media=photo_source, caption=text, parse_mode="HTML"
                        ),
                        reply_markup=reply_markup,
                    )
                    if (
                        not banner_name in BANNER_CACHE
                        and hasattr(sent_msg, "photo")
                        and sent_msg.photo
                    ):
                        BANNER_CACHE[banner_name] = sent_msg.photo[-1].file_id
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=bot_msg_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                return
            except Exception:
                pass

        if photo_source:
            new_msg = await bot.send_photo(
                chat_id=chat_id,
                photo=photo_source,
                caption=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            if not banner_name in BANNER_CACHE and new_msg.photo:
                BANNER_CACHE[banner_name] = new_msg.photo[-1].file_id
        else:
            new_msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        await state.update_data(bot_msg_id=new_msg.message_id)


async def _full_reset_user_session(user_id: int, state: FSMContext, username: str | None = None):
    await state.clear()
    _reset_payment_step_tracking(user_id, leave_step=True)
    _cancel_abandoned_reminder(user_id)
    _clear_back_after_service_flags(user_id)
    PROBLEM_USERS.pop(user_id, None)
    _touch_user_activity(user_id, username)
    if user_id in USER_PROFILES:
        USER_PROFILES[user_id]["max_step"] = STEP_LABELS.get("catalog")
    USER_STEP_RANK[user_id] = STEP_RANKS.get("catalog", 1)


# === ГЛАВНОЕ МЕНЮ ===
async def show_main_menu(event: Message | CallbackQuery, state: FSMContext):
    user_id = event.from_user.id
    if user_id in BANNED_USERS:
        return
    await state.clear()
    _reset_payment_step_tracking(user_id, leave_step=True)
    _clear_back_after_service_flags(user_id)
    _touch_user_activity(user_id, event.from_user.username)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Заказать", callback_data="start_order", style="danger"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Мои заказы",
                    callback_data="my_orders",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📊 Репутация",
                    callback_data="reputation",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Поддержка",
                    callback_data="contact_admin",
                    style="danger",
                )
            ],
        ]
    )
    text = (
        "🏆 <b>ARENA SHOP — СПОРТ</b>\n\n"
        "<i>Добро пожаловать в сервис. Все конфиденциально, быстро и безопасно. Работаем 24/7.</i>\n\n"
        "<b>👇 Выберите пункт меню ниже:</b>"
    )
    await render_step(event, state, "main.jpg", text, keyboard)


# === ПОСЛЕДОВАТЕЛЬНЫЙ ВОЗВРАТ НА ПРЕДЫДУЩИЙ ШАГ ОФОРМЛЕНИЯ ЗАКАЗА ===
async def _show_catalog_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(
        event.from_user.id,
        "catalog",
        event.from_user.username,
        service_name=data.get("service_name"),
    )
    await state.set_state(OrderForm.service)
    if record_history:
        await _push_nav_step(state, "service")
    text = (
        "🛒 <b>Каталог услуг</b>\n\nВыберите подходящий вариант из списка ниже:"
    )
    await render_step(event, state, "catalog.jpg", text, build_catalog_keyboard())


async def _show_debt_info_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "debt_info", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.debt_info)
    if record_history:
        await _push_nav_step(state, "debt_info")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['debt_info']}</code>" if data.get("debt_info") else ""
    text = (
        "💸 <b>Вернуть долг</b>\n\n"
        "Укажите информацию о долге: какая сумма долга, данные должника и т.д.:"
        f"{prev_val}"
    )
    await render_step(event, state, "catalog.jpg", text, keyboard)


async def _show_debt_wallet_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "debt_wallet", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.debt_wallet)
    if record_history:
        await _push_nav_step(state, "debt_wallet")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['debt_wallet']}</code>" if data.get("debt_wallet") else ""
    text = (
        "💸 <b>Вернуть долг</b>\n\n"
        "Теперь укажите свой криптокошелек <b>USDT (TRC-20 / BEP-20)</b> или"
        " <b>TON</b>, чтобы получить назад деньги с должника:"
        f"{prev_val}"
    )
    await render_step(event, state, "catalog.jpg", text, keyboard)


async def _show_special_task_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "special_task", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.special_task)
    if record_history:
        await _push_nav_step(state, "special_task")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['special_task']}</code>" if data.get("special_task") else ""
    text = (
        "🎯 <b>Особый заказ</b>\n\n"
        "Опишите вашу задачу, что нужно будет сделать (с вами свяжется менеджер"
        " для расчета стоимости):"
        f"{prev_val}"
    )
    await render_step(event, state, "catalog.jpg", text, keyboard)


async def _show_target_location_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    selected = data.get("country") or USER_LAST_COUNTRY.get(event.from_user.id, "Россия")
    _track_user_step(event.from_user.id, "target_location", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.target_location)
    if record_history:
        await _push_nav_step(state, "target_location")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['target_location']}</code>" if data.get("target_location") else ""
    text = (
        f"📍 Страна: <b>{selected}</b>\n\n"
        "🏢 <b>Локация цели</b>\n"
        "Укажите город, адрес, дом или квартиру следующим сообщением:"
        f"{prev_val}"
    )
    await render_step(event, state, "location.jpg", text, keyboard)


async def _show_target_data_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "target_data", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.target_data)
    if record_history:
        await _push_nav_step(state, "target_data")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['target_data']}</code>" if data.get("target_data") else ""
    text = (
        "🪪 <b>Данные цели</b>\n\n"
        "Укажите ФИО или другую информацию о цели (если неизвестно, напишите"
        " «неизвестно»):"
        f"{prev_val}"
    )
    await render_step(event, state, "target_data.jpg", text, keyboard)


async def _show_photos_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    photos = data.get("photos") or []
    _track_user_step(event.from_user.id, "photos", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.photos)
    if record_history:
        await _push_nav_step(state, "photos")
    if photos:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"✅ Готово ({len(photos)}/3)",
                        callback_data="finish_photos",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="order_back_step", style="danger"
                    )
                ],
            ]
        )
        text = (
            f"📸 Фото ({len(photos)}/3) сохранены. Можете прикрепить еще или"
            " завершить:"
        )
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏩ Пропустить",
                        callback_data="finish_photos",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="order_back_step", style="danger"
                    )
                ],
            ]
        )
        text = (
            "📸 Прикрепите фото цели (до 3 шт.) или нажмите кнопку пропуска:"
        )
    await render_step(event, state, "photos.jpg", text, keyboard)


async def _show_urgency_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "urgency", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.urgency)
    if record_history:
        await _push_nav_step(state, "urgency")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡ За 2-3 часа (+80$)",
                    callback_data="urg_fast_80",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🕐 Завтра (+40$)",
                    callback_data="urg_tomorrow_40",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 Стандартно (2-5 дней)",
                    callback_data="urg_standard_0",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="order_back_step", style="danger"
                )
            ],
        ]
    )
    text = "⏱ <b>Срочность выполнения</b>\n\nВыберите желаемый вариант:"
    await render_step(event, state, "urgency.jpg", text, keyboard)


async def _show_summary_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    user_id = event.from_user.id
    data = await state.get_data()
    _track_user_step(user_id, "summary", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.summary)
    if record_history:
        await _push_nav_step(state, "summary")

    base_service_price = data.get("service_price", 0)
    urg_price = data.get("urgency_price", 0)
    urgency = data.get("urgency") or "📅 2-5 дней"
    raw_total_price = (base_service_price + urg_price) if base_service_price > 0 else 0

    discount_pct = int(data.get("promo_discount_pct", 0) or 0)
    applied = USER_APPLIED_PROMOS.get(user_id)
    if applied and not _get_client_paid_orders(user_id):
        ap_pct = int(applied.get("discount_pct", 0) or 0)
        if ap_pct > 0:
            discount_pct = ap_pct

    if raw_total_price > 0 and discount_pct > 0:
        total_price = _apply_discount_once(raw_total_price, discount_pct)
    else:
        total_price = raw_total_price

    await state.update_data(
        original_total_price=raw_total_price,
        total_price=total_price,
        promo_discount_pct=discount_pct,
        urgency=urgency,
    )

    rate = await get_exchange_rate(data.get("curr_code", "USD"))
    local_amount = round(float(total_price) * rate) if float(total_price) > 0 else 0

    if float(raw_total_price) > 0 and discount_pct > 0:
        price_display = (
            f"<s>{_format_num(raw_total_price)}$</s> → <b>{_format_num(total_price)}$</b> "
            f"<b>(-{discount_pct}%)</b> <i>(≈ {local_amount:,} {data.get('curr_symbol')})</i>"
        )
    elif float(total_price) > 0:
        price_display = (
            f"<b>{_format_num(total_price)}$</b> <i>(≈ {local_amount:,}"
            f" {data.get('curr_symbol')})</i>"
        )
    else:
        price_display = "<b>Рассчитывается менеджером</b>"

    is_special = (data.get("service_name") or "").strip().lower() == "особый заказ"
    action_btn_text = "✅ Отправить заявку" if is_special else "💳 Перейти к оплате"
    action_callback = "submit_special_order" if is_special else "go_to_payment"

    summary_text = (
        "✨ <b>Сводка заказа:</b>\n\n"
        f"• Услуга: <b>{data.get('service_name')}</b>\n"
        f"• Срочность: {urgency}\n"
        f"• Страна: {data.get('country')}\n\n"
        f"💵 <b>Сумма к оплате: {price_display}</b>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=action_btn_text,
                    callback_data=action_callback,
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="order_back_step", style="primary"
                )
            ],
        ]
    )
    await render_step(event, state, "summary.jpg", summary_text, keyboard)


async def _navigate_to_step_key(
    callback: CallbackQuery, state: FSMContext, step_key: str
):
    data = await state.get_data()
    service_name = data.get("service_name") or ""
    if step_key == "service":
        await _show_catalog_step(callback, state, record_history=False)
    elif step_key == "debt_info":
        await _show_debt_info_step(callback, state, record_history=False)
    elif step_key == "debt_wallet":
        await _show_debt_wallet_step(callback, state, record_history=False)
    elif step_key == "special_task":
        await _show_special_task_step(callback, state, record_history=False)
    elif step_key == "country":
        await show_country_selection(
            callback, state, service_name, data.get("service_price", 0), record_history=False
        )
    elif step_key == "target_location":
        await _show_target_location_step(callback, state, record_history=False)
    elif step_key == "target_data":
        await _show_target_data_step(callback, state, record_history=False)
    elif step_key == "photos":
        await _show_photos_step(callback, state, record_history=False)
    elif step_key == "urgency":
        await _show_urgency_step(callback, state, record_history=False)
    elif step_key == "summary":
        await _show_summary_step(callback, state, record_history=False)
    else:
        await _show_catalog_step(callback, state, record_history=False)


@router.callback_query(F.data == "order_back_step")
async def cb_order_back_step(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    current_state = await state.get_state()
    data = await state.get_data()
    service_name = data.get("service_name") or ""

    if user_id in ON_PAYMENT_METHOD_STEP:
        asyncio.create_task(
            _send_unpaid_payment_step_notice(
                bot=callback.bot,
                user_id=user_id,
                reason_text="Нажал «Назад» на этапе выбора способа оплаты",
            )
        )

    state_to_key = {
        OrderForm.service.state: "service",
        OrderForm.debt_info.state: "debt_info",
        OrderForm.debt_wallet.state: "debt_wallet",
        OrderForm.special_task.state: "special_task",
        OrderForm.country.state: "country",
        OrderForm.target_location.state: "target_location",
        OrderForm.target_data.state: "target_data",
        OrderForm.photos.state: "photos",
        OrderForm.urgency.state: "urgency",
        OrderForm.summary.state: "summary",
        OrderForm.payment.state: "payment",
        OrderForm.receipt.state: "receipt",
    }
    current_key = state_to_key.get(current_state)
    prev_step = await _pop_nav_step(state, current_key)

    if prev_step and prev_step != current_key:
        await _navigate_to_step_key(callback, state, prev_step)
        return

    # Резервный детерминированный переход назад, если стек пуст
    if current_state == OrderForm.debt_info.state:
        await _show_catalog_step(callback, state, record_history=False)
    elif current_state == OrderForm.debt_wallet.state:
        await _show_debt_info_step(callback, state, record_history=False)
    elif current_state == OrderForm.special_task.state:
        await _show_catalog_step(callback, state, record_history=False)
    elif current_state == OrderForm.country.state:
        if service_name == "Вернуть долг":
            await _show_debt_wallet_step(callback, state, record_history=False)
        elif service_name == "Особый заказ":
            await _show_special_task_step(callback, state, record_history=False)
        else:
            await _show_catalog_step(callback, state, record_history=False)
    elif current_state == OrderForm.target_location.state:
        await show_country_selection(
            callback, state, service_name, data.get("service_price", 0), record_history=False
        )
    elif current_state == OrderForm.target_data.state:
        await _show_target_location_step(callback, state, record_history=False)
    elif current_state == OrderForm.photos.state:
        await _show_target_data_step(callback, state, record_history=False)
    elif current_state == OrderForm.urgency.state:
        await _show_photos_step(callback, state, record_history=False)
    elif current_state == OrderForm.summary.state:
        await _show_urgency_step(callback, state, record_history=False)
    elif current_state == OrderForm.payment.state:
        if service_name and (service_name or "").strip().lower() != "особый заказ":
            await _show_summary_step(callback, state, record_history=False)
        elif data.get("country"):
            await _show_urgency_step(callback, state, record_history=False)
        else:
            await _show_catalog_step(callback, state, record_history=False)
    else:
        await _show_catalog_step(callback, state, record_history=False)


# === АДМИНСКИЕ И МЕНЕДЖЕРСКИЕ КОМАНДЫ ===
@router.message(Command("ban"))
async def cmd_ban_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование: `/ban <user_id>`", parse_mode="Markdown"
        )
        return
    target = args[1].strip()
    if target.isdigit():
        user_id = int(target)
        BANNED_USERS.add(user_id)
        queue_db(("ban_add", user_id))
        _reset_payment_step_tracking(user_id, leave_step=True)
        _cancel_abandoned_reminder(user_id)
        _clear_back_after_service_flags(user_id)
        _close_dialog_state(user_id)
        USER_LAST_BROADCAST.pop(user_id, None)
        PROBLEM_USERS.pop(user_id, None)
        USER_BILLS.pop(user_id, None)
        queue_db(("bill_delete", user_id))
        for t in TICKETS.values():
            if t.get("user_id") == user_id:
                t["status"] = "🔒 Закрыто (бан)"
        if message.bot and message.from_user.id == user_id:
            await state.clear()
        await message.reply(
            f"✅ Пользователь с ID <code>{user_id}</code> заблокирован. Все напоминания, диалоги, рассылки и активные состояния остановлены.",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            "❌ Укажите корректный числовой ID.", parse_mode="Markdown"
        )


@router.message(Command("unban"))
async def cmd_unban_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование: `/unban <user_id>`", parse_mode="Markdown"
        )
        return
    target = args[1].strip()
    if target.isdigit():
        user_id = int(target)
        BANNED_USERS.discard(user_id)
        queue_db(("ban_remove", user_id))
        await message.reply(
            f"✅ Пользователь с ID <code>{user_id}</code> разблокирован.",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            "❌ Укажите корректный числовой ID.", parse_mode="Markdown"
        )


async def _set_order_status_and_notify(
    bot: Bot,
    target_user_id: int,
    new_status: str,
    order_idx: int | None = None,
) -> tuple[bool, str]:
    if target_user_id in BANNED_USERS:
        return False, "Пользователь заблокирован"

    orders = USER_ORDERS.get(target_user_id, [])
    service_title = "Ваш заказ"

    if orders:
        if order_idx is not None and 0 <= order_idx < len(orders):
            orders[order_idx]["status"] = new_status
            service_title = orders[order_idx].get("service_name") or service_title
            queue_db(("order_set_index_status", target_user_id, order_idx, new_status))
        else:
            orders[-1]["status"] = new_status
            service_title = orders[-1].get("service_name") or service_title
            queue_db(("order_set_last_status", target_user_id, new_status))
    else:
        bill = USER_BILLS.get(target_user_id) or {}
        if bill.get("service_name"):
            service_title = bill["service_name"]
        elif USER_PROFILES.get(target_user_id, {}).get("last_service"):
            service_title = USER_PROFILES[target_user_id]["last_service"]

    if "выполнен" in new_status.lower():
        _reset_payment_step_tracking(target_user_id, leave_step=True)
        _cancel_abandoned_reminder(target_user_id)
        _clear_back_after_service_flags(target_user_id)
        USER_BILLS.pop(target_user_id, None)
        queue_db(("bill_delete", target_user_id))

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                f"📌 <b>Статус вашего заказа обновлен: {new_status}</b>\n\n"
                f"• Услуга / заказ: <b>{service_title}</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    return True, service_title


@router.message(Command("work"))
async def cmd_work_order(message: Message):
    if not is_staff(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply(
            "❌ Использование: `/work <user_id>`\nПример: `/work 4324324`",
            parse_mode="Markdown",
        )
        return

    target_user_id = int(args[1].strip())
    if target_user_id in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return
    if not USER_ORDERS.get(target_user_id):
        await message.reply(
            f"❌ У пользователя <code>{target_user_id}</code> нет заказов.",
            parse_mode="HTML",
        )
        return

    ok, service_title = await _set_order_status_and_notify(
        message.bot, target_user_id, "⚙️ В работе"
    )
    if ok:
        await message.reply(
            f"✅ Заказ пользователя <code>{target_user_id}</code> "
            f"(<b>{service_title}</b>) переведен в статус <b>В работе</b>.",
            parse_mode="HTML",
        )


@router.message(Command("bill"))
async def cmd_bill_user(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование: `/bill <user_id или @username> <услуга> <сумма$>`\nПример: `/bill"
            " 282810030 особый заказ 220$`",
            parse_mode="Markdown",
        )
        return

    parts = args[1].strip().split()
    if len(parts) < 2:
        await message.reply(
            "❌ Укажите корректный `user_id` или `@username`, название услуги и сумму.\nПример:"
            " `/bill 282810030 особый заказ 220$`",
            parse_mode="Markdown",
        )
        return

    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        bill_body_text = " ".join(parts[1:])
        forbidden = find_forbidden_requisites_for_manager(bill_body_text)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    target_user_id = _resolve_target_user_id(parts[0])
    if target_user_id is None:
        await message.reply(
            "❌ Укажите корректный числовой `user_id` или известный боту `@username` клиента.",
            parse_mode="Markdown",
        )
        return

    if target_user_id in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    raw_amount = (
        parts[-1]
        .replace("$", "")
        .replace("usd", "")
        .replace("USD", "")
        .replace(",", ".")
        .strip()
    )
    try:
        amount_usd = float(raw_amount)
        if amount_usd <= 0:
            raise ValueError()
    except ValueError:
        await message.reply(
            "❌ Некорректная сумма. Пример: `/bill 282810030 особый заказ 220$`",
            parse_mode="Markdown",
        )
        return

    service_name = " ".join(parts[1:-1]).strip() if len(parts) > 2 else "Особый заказ"
    if not service_name:
        service_name = "Особый заказ"

    _, reply_msg = await _issue_bill_to_client(
        bot=message.bot,
        caller_id=message.from_user.id,
        caller_state=state,
        target_user_id=target_user_id,
        service_name=service_name,
        amount_usd=amount_usd,
    )
    await message.reply(reply_msg, parse_mode="HTML")


@router.message(Command("send"))
async def cmd_send_to_user(message: Message):
    if not is_staff(message.from_user.id):
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply(
            "❌ Использование: `/send <user_id или @username> <текст сообщения>`",
            parse_mode="Markdown",
        )
        return
    target_raw = args[1].strip()
    text_to_send = args[2].strip()
    target_user_id = _resolve_target_user_id(target_raw)
    if target_user_id is None:
        await message.reply(
            "❌ Укажите корректный числовой ID или известный боту `@username` пользователя.",
            parse_mode="Markdown",
        )
        return

    if target_user_id in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        forbidden = find_forbidden_requisites_for_manager(text_to_send)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    ok, err_text = await _deliver_send_message_to_client(
        message.bot,
        target_user_id=target_user_id,
        text_to_send=text_to_send,
        prefix="💬 <b>Сообщение от менеджера:</b>",
        staff_id=message.from_user.id,
    )
    if ok:
        await message.reply(
            f"✅ Сообщение отправлено пользователю <code>{target_user_id}</code>.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ Не удалось отправить сообщение: {err_text}")


@router.message(Command("roll"))
async def cmd_roll_funnel(message: Message):
    if not is_staff(message.from_user.id):
        return

    active_uids = [uid for uid in KNOWN_USERS.keys() if uid not in BANNED_USERS]
    if not active_uids:
        await message.reply("📊 <b>Воронка и активность:</b> пока нет активных клиентов.", parse_mode="HTML")
        return

    active_uids.sort(
        key=lambda u: (USER_PROFILES.get(u, {}).get("last_seen") or KNOWN_USERS.get(u) or datetime.datetime.min),
        reverse=True,
    )

    lines = [f"📊 <b>Воронка и активность клиентов ({len(active_uids)} чел.):</b>\n"]
    for idx, uid in enumerate(active_uids[:30], 1):
        prof = USER_PROFILES.get(uid, {})
        uname = prof.get("username")
        tag = f"@{uname}" if uname else "без @username"
        step = prof.get("max_step") or STEP_LABELS.get("catalog")
        service = prof.get("last_service") or (USER_BILLS.get(uid) or {}).get("service_name") or "не выбрана"
        status = _get_latest_order_status(uid)
        lines.append(
            f"{idx}. {tag} | ID: <code>{uid}</code>\n"
            f"   • Этап: <b>{step}</b>\n"
            f"   • Услуга / заполнялось: <b>{service}</b>\n"
            f"   • Статус заказа: <b>{status}</b>"
        )
    if len(active_uids) > 30:
        lines.append(f"\n<i>Показаны 30 самых свежих клиентов из {len(active_uids)}.</i>")

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("aid"))
async def cmd_aid_problem_users(message: Message):
    if not is_staff(message.from_user.id):
        return

    problem_entries = []
    for uid, info in PROBLEM_USERS.items():
        if uid in BANNED_USERS:
            continue
        problem_entries.append((uid, info))

    known_problem_uids = {u for u, _ in problem_entries}
    for t in TICKETS.values():
        uid = t.get("user_id")
        if not uid or uid in BANNED_USERS or uid in known_problem_uids:
            continue
        if t.get("kind") in ("support", "payment_help", "special_order", "unpaid_checkout", "promo_request") and t.get("status") != "🔒 Закрыто":
            prof = USER_PROFILES.get(uid, {})
            problem_entries.append((
                uid,
                {
                    "kind": t.get("kind"),
                    "step": prof.get("max_step") or "Обращение",
                    "query": "",
                    "data": t.get("extra_data") or {},
                    "updated_at": datetime.datetime.now(),
                },
            ))
            known_problem_uids.add(uid)

    if not problem_entries:
        await message.reply("🆘 <b>Проблемные клиенты:</b> сейчас нет клиентов в особых ветках, проблемах с оплатой или поддержке.", parse_mode="HTML")
        return

    kind_titles = {
        "special_order": "🎯 Особый заказ",
        "payment_help": "💳 Проблема с оплатой",
        "support": "💬 Поддержка",
        "unpaid_checkout": "⏳ Неоплаченный заказ",
        "send_reply": "↩️ Переписка по обращению",
        "promo_request": "🎟 Запрос промокода",
    }

    lines = [f"🆘 <b>Проблемные клиенты ({len(problem_entries)}):</b>\n"]
    for idx, (uid, info) in enumerate(problem_entries[:30], 1):
        prof = USER_PROFILES.get(uid, {})
        uname = prof.get("username")
        tag = f"@{uname}" if uname else "без @username"
        k_title = kind_titles.get(info.get("kind", ""), info.get("kind", "Обращение"))
        step = info.get("step") or prof.get("max_step") or "—"
        query_str = info.get("query") or "—"
        d = info.get("data") or {}
        filled_parts = []
        for field_key, label in (
            ("service_name", "услуга"),
            ("country", "страна"),
            ("target_location", "локация"),
            ("target_data", "цель"),
            ("special_task", "задача"),
            ("debt_info", "долг"),
            ("debt_wallet", "кошелек"),
            ("urgency", "срочность"),
            ("total_price", "сумма$"),
        ):
            if d.get(field_key):
                filled_parts.append(f"{label}: {d[field_key]}")
        filled_str = ", ".join(filled_parts) if filled_parts else "нет доп. данных"

        lines.append(
            f"{idx}. <b>{k_title}</b> — {tag} | ID: <code>{uid}</code>\n"
            f"   • Этап: <b>{step}</b>\n"
            f"   • Запрос: {query_str}\n"
            f"   • Данные: {filled_str}"
        )

    await message.reply("\n".join(lines), parse_mode="HTML")


# === ПРОМОКОДЫ: ВВОД КЛИЕНТОМ И РУЧНОЕ ПОДТВЕРЖДЕНИЕ АДМИНОМ / МЕНЕДЖЕРОМ ===
_PROMO_WORD_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_-]{2,32}$")
PROMO_BANNER_NAME = "promocode.jpg"


def _build_promo_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Вернуться к заказу",
                callback_data="promo_back_to_order",
                style="primary",
            )
        ]]
    )


def _build_promo_staff_keyboard(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"promo_approve_{req_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"promo_reject_{req_id}",
                    style="danger",
                ),
            ]
        ]
    )


async def _send_promo_return_notice(bot: Bot, user_id: int, text: str):
    if user_id in BANNED_USERS:
        return
    banner_path = get_banner_path(PROMO_BANNER_NAME)
    kb = _build_promo_back_keyboard()
    if PROMO_BANNER_NAME in BANNER_CACHE:
        await bot.send_photo(
            chat_id=user_id,
            photo=BANNER_CACHE[PROMO_BANNER_NAME],
            caption=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    elif banner_path and os.path.exists(banner_path):
        sent = await bot.send_photo(
            chat_id=user_id,
            photo=FSInputFile(banner_path),
            caption=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
        if sent.photo:
            BANNER_CACHE[PROMO_BANNER_NAME] = sent.photo[-1].file_id
    else:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
        )


async def _restore_or_go_order_on_back(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id in BANNED_USERS:
        return
    try:
        await callback.answer()
    except Exception:
        pass

    _touch_user_activity(user_id, callback.from_user.username)
    data = await state.get_data()
    bill = USER_BILLS.get(user_id) or {}
    prof = USER_PROFILES.get(user_id) or {}

    has_order_ctx = bool(
        data.get("service_name")
        or data.get("total_price") is not None
        or data.get("service_price") is not None
        or data.get("country")
        or bill
        or prof.get("last_service")
    )

    if has_order_ctx:
        restored_country = (
            data.get("country")
            or bill.get("country")
            or USER_LAST_COUNTRY.get(user_id, "Россия")
        )
        curr_info = COUNTRY_CURRENCIES.get(
            restored_country, {"code": "USD", "symbol": "USD"}
        )
        restored_service = (
            data.get("service_name")
            or bill.get("service_name")
            or prof.get("last_service")
            or "Услуга"
        )
        if bill:
            restored_price = bill.get("total_price", 250)
            restored_orig = bill.get("original_total_price", restored_price)
            restored_disc = int(bill.get("promo_discount_pct", 0) or 0)
        else:
            raw_fallback = (
                data.get("total_price")
                if data.get("total_price") is not None
                else data.get("service_price")
            )
            restored_price = raw_fallback if raw_fallback is not None else 250
            restored_orig = (
                data.get("original_total_price")
                if data.get("original_total_price") is not None
                else restored_price
            )
            restored_disc = int(data.get("promo_discount_pct", 0) or 0)

        await state.update_data(
            service_name=restored_service,
            country=restored_country,
            curr_code=data.get("curr_code") or bill.get("curr_code") or curr_info["code"],
            curr_symbol=data.get("curr_symbol") or bill.get("curr_symbol") or curr_info["symbol"],
            total_price=restored_price,
            original_total_price=restored_orig,
            promo_discount_pct=restored_disc,
        )
        _track_user_step(
            user_id,
            "payment",
            callback.from_user.username,
            service_name=restored_service,
        )
        await state.set_state(OrderForm.payment)
        await process_go_to_payment(callback, state)
    else:
        _reset_payment_step_tracking(user_id, leave_step=True)
        await _show_catalog_step(callback, state, record_history=True)


@router.callback_query(
    F.data.in_({"promo_back_to_order", "promo_code_back_to_order", "back_to_order"})
    | F.data.startswith("promo_back")
)
async def cb_promo_back_to_order(callback: CallbackQuery, state: FSMContext):
    await _restore_or_go_order_on_back(callback, state)


@router.callback_query(F.data == "promo_code_entry")
async def cb_promo_code_entry(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _touch_user_activity(user_id, callback.from_user.username)

    if _get_client_paid_orders(user_id):
        await callback.answer(
            "❌ Промокод доступен только на первый заказ.", show_alert=True
        )
        return

    if user_id in USED_PROMO_USERS or user_id in USER_APPLIED_PROMOS:
        await callback.answer(
            "❌ Вы уже использовали промокод (он применяется только один раз).",
            show_alert=True,
        )
        return

    _reset_payment_step_tracking(user_id, leave_step=True)
    await state.set_state(OrderForm.promo_code)

    text = (
        "🎟 <b>Активация промокода</b>\n\n"
        "Впиши промокод одним словом следующим сообщением.\n\n"
        "💡 <i>Не знаешь как получить — пиши менеджеру (актуальный юзернейм менеджера "
        "вы всегда можете найти в нашем телеграм канале) и получи секретный промокод на скидку от -10 до -50% на первый заказ!</i>"
    )
    await render_step(callback, state, PROMO_BANNER_NAME, text, _build_promo_back_keyboard())


@router.message(OrderForm.promo_code, F.text)
async def process_promo_code_input(message: Message, state: FSMContext):
    global PROMO_REQUEST_COUNTER
    user = message.from_user
    user_id = user.id
    raw_code = message.text.strip()
    cmd_head = raw_code.split()[0].lower().split("@")[0] if raw_code else ""
    if cmd_head in ("/start", "/restart"):
        await _full_reset_user_session(user_id, state, user.username)
        if cmd_head == "/restart":
            try:
                await message.reply("🔄 Бот перезапущен, состояние очищено.")
            except Exception:
                pass
        await show_main_menu(message, state)
        return

    _touch_user_activity(user_id, user.username)
    words = raw_code.split()
    if len(words) != 1 or not _PROMO_WORD_RE.match(words[0]):
        await _send_promo_return_notice(
            message.bot,
            user_id,
            "❌ Неправильный промокод, АРЕНУ не обманешь)\n\n"
            "Введите промокод <b>одним словом</b> или нажмите кнопку возврата:",
        )
        return

    code_upper = words[0].upper()

    if _get_client_paid_orders(user_id):
        await _send_promo_return_notice(
            message.bot,
            user_id,
            "❌ Промокод применяется только на первый заказ.",
        )
        return

    if user_id in USED_PROMO_USERS or user_id in USER_APPLIED_PROMOS:
        await _send_promo_return_notice(
            message.bot,
            user_id,
            "❌ Промокод уже был применён ранее (строго один раз на первый заказ).",
        )
        return

    # Определяем процент скидки по промокоду (BLOODYEAR = 30%, для прочих кодов — 10% по умолчанию)
    discount_pct = 30 if code_upper == "BLOODYEAR" else 10

    data, country, curr_code, curr_symbol, current_price, service_name = (
        await get_payment_context(user_id, state)
    )
    base_full_price = data.get("original_total_price") or current_price or 0

    _track_user_step(user_id, "promo", user.username, service_name=service_name)
    _record_problem_user(
        user_id=user_id,
        kind="promo_request",
        step_label="запросил промокод",
        query_text=f"Промокод: {code_upper} (-{discount_pct}%)",
        data=data,
    )

    PROMO_REQUEST_COUNTER += 1
    req_id = PROMO_REQUEST_COUNTER
    PROMO_REQUESTS[req_id] = {
        "req_id": req_id,
        "user_id": user_id,
        "username": user.username,
        "code": code_upper,
        "discount_pct": discount_pct,
        "base_full_price": base_full_price,
        "country": country,
        "curr_code": curr_code,
        "curr_symbol": curr_symbol,
        "service_name": service_name,
        "order_data": dict(data),
        "status": "pending",
        "staff_msgs": {},
    }

    user_tag = f"@{user.username}" if user.username else "без @username"
    order_details = _build_order_details_text(data, country, service_name, base_full_price)
    summary_block = _format_user_summary_block(user_id, data)

    staff_text = (
        f"🎟 <b>Запрос активации промокода #{req_id}</b>\n"
        f"• Статус: <b>запросил промокод</b>\n"
        f"• Клиент: {user_tag} | ID: <code>{user_id}</code>\n"
        f"• Промокод: <code>{code_upper}</code> (скидка <b>-{discount_pct}%</b>)\n"
        f"• Данные заказа:{order_details}\n\n"
        f"{summary_block}"
    )
    kb_staff = _build_promo_staff_keyboard(req_id)

    for staff_id in get_staff_recipients():
        try:
            sent = await message.bot.send_message(
                chat_id=staff_id,
                text=staff_text,
                reply_markup=kb_staff,
                parse_mode="HTML",
            )
            PROMO_REQUESTS[req_id]["staff_msgs"][staff_id] = sent.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки заявки на промокод #{req_id} стафу {staff_id}: {e}")

    await state.set_state(OrderForm.payment)
    await _send_promo_return_notice(
        message.bot,
        user_id,
        "⏳ <b>Заявка на активацию промокода отправлена!</b>\n\n"
        "Промокод пока не применён. Ожидайте проверки менеджером или администратором.",
    )


async def _finalize_promo_staff_message(bot: Bot, req: dict, decision_label: str):
    req_id = req["req_id"]
    uid = req["user_id"]
    uname = req.get("username")
    user_tag = f"@{uname}" if uname else "без @username"
    code_upper = req["code"]
    disc_pct = req["discount_pct"]
    base_full_price = req.get("base_full_price", 0)
    order_details = _build_order_details_text(
        req.get("order_data") or {},
        req.get("country") or "Россия",
        req.get("service_name") or "Услуга",
        base_full_price,
    )
    summary_block = _format_user_summary_block(uid, req.get("order_data") or {})

    updated_text = (
        f"🎟 <b>Запрос активации промокода #{req_id}</b>\n"
        f"• Статус: <b>{decision_label}</b>\n"
        f"• Клиент: {user_tag} | ID: <code>{uid}</code>\n"
        f"• Промокод: <code>{code_upper}</code> (скидка <b>-{disc_pct}%</b>)\n"
        f"• Данные заказа:{order_details}\n\n"
        f"{summary_block}"
    )
    for staff_id, msg_id in (req.get("staff_msgs") or {}).items():
        try:
            await bot.edit_message_text(
                chat_id=staff_id,
                message_id=msg_id,
                text=updated_text,
                reply_markup=None,
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("promo_approve_"))
async def cb_promo_approve(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    try:
        req_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    req = PROMO_REQUESTS.get(req_id)
    if not req:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if req_id in PROMO_DECIDED_REQUESTS:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    uid = req["user_id"]
    if uid in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return

    if _get_client_paid_orders(uid) or uid in USED_PROMO_USERS or uid in USER_APPLIED_PROMOS:
        PROMO_DECIDED_REQUESTS.add(req_id)
        req["status"] = "rejected_already_used"
        await _finalize_promo_staff_message(callback.bot, req, "❌ Отменено (промокод уже использован или заказ оплачен)")
        await callback.answer("У клиента уже оплачен заказ или использован промокод", show_alert=True)
        return

    PROMO_DECIDED_REQUESTS.add(req_id)
    req["status"] = "approved"
    USED_PROMO_USERS.add(uid)
    queue_db(("promo_used_add", uid, req["code"], req["discount_pct"]))

    # Берём актуальную полную сумму заказа или счёта (без повторного дисконтирования)
    bill = USER_BILLS.get(uid) or {}
    raw_full = bill.get("original_total_price") or bill.get("total_price") or req.get("base_full_price") or 0
    if not raw_full and callback.from_user.id == uid:
        st_data = await state.get_data()
        raw_full = st_data.get("original_total_price") or st_data.get("total_price") or 0

    disc_pct = int(req["discount_pct"])
    if float(raw_full) > 0:
        discounted_price = _apply_discount_once(raw_full, disc_pct)
    else:
        discounted_price = 0

    USER_APPLIED_PROMOS[uid] = {
        "code": req["code"],
        "discount_pct": disc_pct,
        "original_price": raw_full,
        "discounted_price": discounted_price,
    }

    if uid in USER_BILLS:
        USER_BILLS[uid]["original_total_price"] = raw_full
        USER_BILLS[uid]["total_price"] = discounted_price
        USER_BILLS[uid]["promo_discount_pct"] = disc_pct
        queue_db(("bill_set", uid, dict(USER_BILLS[uid])))

    if callback.from_user.id == uid:
        await state.update_data(
            original_total_price=raw_full,
            total_price=discounted_price,
            promo_discount_pct=disc_pct,
        )

    PROBLEM_USERS.pop(uid, None)
    await _finalize_promo_staff_message(callback.bot, req, f"✅ Подтверждено (-{disc_pct}%)")

    if float(raw_full) > 0:
        price_line = (
            f"\nСумма заказа: <s>{_format_num(raw_full)}$</s> → "
            f"<b>{_format_num(discounted_price)}$</b> <b>(-{disc_pct}%)</b>"
        )
    else:
        price_line = ""

    try:
        await _send_promo_return_notice(
            callback.bot,
            uid,
            f"🎉 <b>Поздравляем! Вы получили скидку -{disc_pct}% на всю сумму заказа!</b>"
            f"{price_line}",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить клиента {uid} о подтверждении промокода: {e}")

    await callback.answer(f"✅ Промокод подтверждён (-{disc_pct}%)", show_alert=True)


@router.callback_query(F.data.startswith("promo_reject_"))
async def cb_promo_reject(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    try:
        req_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    req = PROMO_REQUESTS.get(req_id)
    if not req:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if req_id in PROMO_DECIDED_REQUESTS:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    PROMO_DECIDED_REQUESTS.add(req_id)
    req["status"] = "rejected"
    uid = req["user_id"]

    await _finalize_promo_staff_message(callback.bot, req, "❌ Отменено")

    if uid not in BANNED_USERS:
        try:
            await _send_promo_return_notice(
                callback.bot,
                uid,
                "❌ <b>Вы не выполнили условия акции для активации скидки.</b>\n\n"
                "Условия не соблюдены — напишите менеджеру (актуальный юзернейм менеджера "
                "вы всегда можете найти в нашем телеграм канале) и выполните условия, чтобы получить персональную скидку.",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить клиента {uid} об отклонении промокода: {e}")

    await callback.answer("❌ Промокод отклонён", show_alert=True)


# === КЛИЕНТСКИЕ КНОПКИ ПОД СООБЩЕНИЕМ ОТ МЕНЕДЖЕРА ===
@router.callback_query(F.data == "client_reply_manager")
async def cb_client_reply_manager(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    _touch_user_activity(user_id, callback.from_user.username)
    dlg = ACTIVE_DIALOGS.get(user_id)
    if not dlg or dlg.get("closed"):
        await callback.answer(
            "🔒 Это обращение уже закрыто.", show_alert=True
        )
        return

    await callback.answer()
    await state.set_state(ClientReplyForm.waiting_message)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🔒🏠 Закрыть обращение",
                callback_data="client_close_dialog",
                style="danger",
            )
        ]]
    )
    await callback.message.answer(
        "✍️ <b>Напишите ваш вопрос или сообщение менеджеру</b> (можно прикрепить фото):",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "client_close_dialog")
async def cb_client_close_dialog(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer("🔒 Обращение закрыто")
    except Exception:
        pass
    user_id = callback.from_user.id
    _close_dialog_state(user_id)
    for t in TICKETS.values():
        if t.get("user_id") == user_id and t.get("status") != "🔒 Закрыто":
            t["status"] = "🔒 Закрыто"
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await show_main_menu(callback, state)


@router.message(ClientReplyForm.waiting_message, F.text | F.photo)
async def process_client_reply_to_manager(message: Message, state: FSMContext):
    user = message.from_user
    _touch_user_activity(user.id, user.username)
    dlg = ACTIVE_DIALOGS.get(user.id)
    if not dlg or dlg.get("closed"):
        await state.clear()
        await show_main_menu(message, state)
        return

    _arm_dialog_timer(message.bot, user.id, dlg.get("last_staff_id"))
    user_tag = f"@{user.username}" if user.username else "без @username"
    photo_id = message.photo[-1].file_id if message.photo else None
    client_text = (message.caption or message.text or "").strip() or "(без текста, фото)"

    prof_data = {
        "service_name": USER_PROFILES.get(user.id, {}).get("last_service"),
        "country": USER_LAST_COUNTRY.get(user.id),
    }
    body = (
        f"• От клиента: {user_tag} (ID: <code>{user.id}</code>)\n"
        f"• Сообщение: {client_text}"
    )
    await _send_staff_ticket_menu(
        bot=message.bot,
        user_id=user.id,
        kind="send_reply",
        header_title="↩️ <b>Ответ клиента по обращению</b>",
        body_text=body,
        extra_data=prof_data,
        photo_id=photo_id,
    )

    await state.clear()
    await message.answer(
        "✅ <b>Сообщение отправлено менеджеру!</b> Вы можете нажать кнопку под прошлым сообщением, чтобы задать еще вопрос, или закрыть обращение.",
        parse_mode="HTML",
    )


# === ОБРАБОТЧИКИ МЕНЮШЕК ОБРАЩЕНИЙ И ЗАКАЗОВ У МЕНЕДЖЕРА / АДМИНА ===
@router.callback_query(F.data == "staff_cancel_mode")
async def cb_staff_cancel_mode(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await callback.answer("❌ Режим ввода отменен")
    try:
        await callback.message.edit_text("❌ Режим ввода отменен.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("tkt_noop_"))
async def cb_ticket_noop(callback: CallbackQuery):
    await callback.answer("🔒 Обращение уже закрыто", show_alert=False)


async def _close_ticket_by_id(bot: Bot, tid: int, target_user_id: int | None = None):
    ticket = TICKETS.get(tid)
    uid = target_user_id or (ticket.get("user_id") if ticket else None)
    if ticket:
        ticket["status"] = "🔒 Закрыто"
        uid = uid or ticket.get("user_id")
    if uid:
        _close_dialog_state(uid)
        PROBLEM_USERS.pop(uid, None)

    closed_kb = _build_ticket_keyboard(tid, user_id=uid or 0, kind=(ticket.get("kind") if ticket else "support"), closed=True)
    if ticket:
        for staff_id, msg_id in ticket.get("staff_msgs", {}).items():
            try:
                await bot.edit_message_reply_markup(
                    chat_id=staff_id,
                    message_id=msg_id,
                    reply_markup=closed_kb,
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("tkt_close_"))
async def cb_ticket_close(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return

    await _close_ticket_by_id(callback.bot, tid, ticket.get("user_id"))
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_build_ticket_keyboard(tid, user_id=ticket["user_id"], kind=ticket["kind"], closed=True)
        )
    except Exception:
        pass
    await callback.answer("🔒 Обращение закрыто")


@router.callback_query(F.data.startswith("order_close_"))
async def cb_order_close_dialog(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    _close_dialog_state(target_user_id)
    PROBLEM_USERS.pop(target_user_id, None)
    for t in TICKETS.values():
        if t.get("user_id") == target_user_id:
            t["status"] = "🔒 Закрыто"
    await callback.answer("🔒 Обращение / диалог закрыт")


async def _enter_staff_mode(
    callback: CallbackQuery,
    state: FSMContext,
    fsm_state: State,
    mode_key: str,
    target_user_id: int,
    tid: int | None = None,
):
    await callback.answer()
    await state.set_state(fsm_state)
    await state.update_data(reply_target_user_id=target_user_id, reply_ticket_id=tid)
    await callback.message.answer(
        _build_staff_action_guide(mode_key, target_user_id),
        reply_markup=_build_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("tkt_reply_"))
async def cb_ticket_reply(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    if ticket.get("status") == "🔒 Закрыто":
        await callback.answer("🔒 Обращение уже закрыто", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_reply, "reply", ticket["user_id"], tid=tid)


@router.callback_query(F.data.startswith("tkt_bill_"))
async def cb_ticket_bill(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_bill, "bill", ticket["user_id"], tid=tid)


@router.callback_query(F.data.startswith("tkt_replybill_"))
async def cb_ticket_reply_and_bill(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_reply_and_bill, "replybill", ticket["user_id"], tid=tid)


@router.callback_query(F.data.startswith("tkt_work_"))
async def cb_ticket_set_work(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    target_user_id = ticket["user_id"]
    if target_user_id in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return
    await _set_order_status_and_notify(callback.bot, target_user_id, "⚙️ В работе")
    await callback.answer("⚙️ Статус обновлен: В работе", show_alert=True)


@router.callback_query(F.data.startswith("tkt_done_"))
async def cb_ticket_set_done(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    target_user_id = ticket["user_id"]
    if target_user_id in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return
    await _set_order_status_and_notify(callback.bot, target_user_id, "✅ Выполнен")
    await callback.answer("✅ Статус обновлен: Выполнен", show_alert=True)


@router.callback_query(F.data.startswith("order_msg_"))
async def cb_order_message_client(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_reply, "reply", target_user_id, tid=None)


@router.callback_query(F.data.startswith("order_bill_"))
async def cb_order_bill_client(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_bill, "bill", target_user_id, tid=None)


@router.callback_query(F.data.startswith("order_replybill_"))
async def cb_order_reply_and_bill_client(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_reply_and_bill, "replybill", target_user_id, tid=None)


@router.callback_query(F.data.startswith("order_work_"))
async def cb_order_set_in_work(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    parts = callback.data.split("_")
    try:
        target_user_id = int(parts[2])
        order_idx = int(parts[3]) if len(parts) > 3 else None
    except (IndexError, ValueError):
        await callback.answer("Ошибка", show_alert=True)
        return

    if target_user_id in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return

    await _set_order_status_and_notify(callback.bot, target_user_id, "⚙️ В работе", order_idx=order_idx)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=_build_paid_order_keyboard(
                target_user_id, order_index=order_idx, in_work=True, is_done=False
            )
        )
    except Exception:
        pass

    await callback.answer("⚙️ Статус заказа обновлен: В работе", show_alert=True)


@router.callback_query(F.data.startswith("order_done_"))
async def cb_order_set_done(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    parts = callback.data.split("_")
    try:
        target_user_id = int(parts[2])
        order_idx = int(parts[3]) if len(parts) > 3 else None
    except (IndexError, ValueError):
        await callback.answer("Ошибка", show_alert=True)
        return

    if target_user_id in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return

    await _set_order_status_and_notify(callback.bot, target_user_id, "✅ Выполнен", order_idx=order_idx)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=_build_paid_order_keyboard(
                target_user_id, order_index=order_idx, in_work=False, is_done=True
            )
        )
    except Exception:
        pass

    await callback.answer("✅ Статус заказа обновлен: Выполнен", show_alert=True)


@router.message(StaffDirectForm.waiting_reply, F.text)
async def process_staff_direct_reply(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return
    text_to_send = message.text.strip()
    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        forbidden = find_forbidden_requisites_for_manager(text_to_send)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    data = await state.get_data()
    target_user_id = data.get("reply_target_user_id")
    tid = data.get("reply_ticket_id")
    await state.clear()

    if not target_user_id:
        await message.reply("❌ Не найден ID клиента для ответа.")
        return
    if int(target_user_id) in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    ticket = TICKETS.get(tid) if tid else None
    kind = ticket.get("kind") if ticket else ""
    prefix = (
        "💬 <b>Ответ от менеджера по вашему заказу:</b>"
        if kind in ("special_order", "payment_help", "unpaid_checkout")
        else "💬 <b>Ответ от службы поддержки:</b>"
    )
    ok, err_text = await _deliver_send_message_to_client(
        message.bot,
        target_user_id=int(target_user_id),
        text_to_send=text_to_send,
        prefix=prefix,
        staff_id=message.from_user.id,
    )
    if ok:
        await message.reply(
            f"✅ Ответ отправлен клиенту <code>{target_user_id}</code>.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ Не удалось отправить ответ: {err_text}")


@router.message(StaffDirectForm.waiting_bill, F.text)
async def process_staff_direct_bill(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return
    raw_input = message.text.strip()
    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        forbidden = find_forbidden_requisites_for_manager(raw_input)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    service_name, parsed = _parse_bill_input(raw_input, default_service="Особый заказ")
    if service_name is None:
        await message.reply(parsed, parse_mode="HTML")
        return

    data = await state.get_data()
    target_user_id = data.get("reply_target_user_id")
    await state.clear()

    if not target_user_id:
        await message.reply("❌ Не найден ID клиента для выставления счета.")
        return
    if int(target_user_id) in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    _, reply_text = await _issue_bill_to_client(
        bot=message.bot,
        caller_id=message.from_user.id,
        caller_state=state,
        target_user_id=int(target_user_id),
        service_name=service_name,
        amount_usd=parsed,
    )
    await message.reply(reply_text, parse_mode="HTML")


@router.message(StaffDirectForm.waiting_reply_and_bill, F.text)
async def process_staff_direct_reply_and_bill(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return
    raw_input = message.text.strip()
    if "|" not in raw_input:
        await message.reply(
            "❌ Используйте формат: <code>Текст сообщения | услуга сумма$</code>\n"
            "Пример: <code>Расчет готов, можно оплачивать. | особый заказ 220$</code>",
            parse_mode="HTML",
        )
        return

    msg_part, bill_part = [p.strip() for p in raw_input.split("|", 1)]
    if not msg_part:
        await message.reply("❌ Укажите текст сообщения перед символом <code>|</code>.", parse_mode="HTML")
        return

    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        forbidden = find_forbidden_requisites_for_manager(raw_input)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    service_name, parsed = _parse_bill_input(bill_part, default_service="Особый заказ")
    if service_name is None:
        await message.reply(parsed, parse_mode="HTML")
        return

    data = await state.get_data()
    target_user_id = data.get("reply_target_user_id")
    await state.clear()

    if not target_user_id:
        await message.reply("❌ Не найден ID клиента.")
        return
    if int(target_user_id) in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    ok_msg, err_msg = await _deliver_send_message_to_client(
        message.bot,
        target_user_id=int(target_user_id),
        text_to_send=msg_part,
        prefix="💬 <b>Сообщение от менеджера:</b>",
        staff_id=message.from_user.id,
    )
    if not ok_msg:
        await message.reply(f"❌ Не удалось отправить сообщение: {err_msg}")
        return

    _, bill_reply = await _issue_bill_to_client(
        bot=message.bot,
        caller_id=message.from_user.id,
        caller_state=state,
        target_user_id=int(target_user_id),
        service_name=service_name,
        amount_usd=parsed,
    )
    await message.reply(
        f"✅ Сообщение и счет отправлены клиенту <code>{target_user_id}</code>.\n{bill_reply}",
        parse_mode="HTML",
    )


@router.message(F.reply_to_message)
async def admin_reply_handler(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return

    current_staff_state = await state.get_state()
    if current_staff_state in (
        StaffDirectForm.waiting_reply.state,
        StaffDirectForm.waiting_bill.state,
        StaffDirectForm.waiting_reply_and_bill.state,
    ):
        await state.clear()

    reply_msg = message.reply_to_message
    text_source = (reply_msg.text or "") + "\n" + (reply_msg.caption or "")

    match = re.search(r"ID:\s*<code>(\d+)</code>|<code>(\d+)</code>", text_source)
    if not match:
        match = re.search(r"(?:ID:|\b)(\d{6,})", text_source)

    if match:
        target_user_id = int(
            match.group(1)
            if match.lastindex == 1
            else (match.group(1) or match.group(2))
        )
        if target_user_id in BANNED_USERS:
            await message.reply("❌ Пользователь заблокирован.")
            return
        if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
            forbidden = find_forbidden_requisites_for_manager(message.text or "")
            if forbidden:
                await message.reply(
                    f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
                )
                return

        text_lower = text_source.lower()
        is_special = "особый заказ" in text_lower or "заказ" in text_lower
        prefix = (
            "💬 <b>Ответ от менеджера по вашему заказу:</b>"
            if is_special
            else "💬 <b>Ответ от службы поддержки:</b>"
        )

        ok, err_text = await _deliver_send_message_to_client(
            message.bot,
            target_user_id=target_user_id,
            text_to_send=(message.text or "").strip(),
            prefix=prefix,
            staff_id=message.from_user.id,
        )
        if ok:
            await message.reply("✅ Ответ успешно отправлен клиенту.")
        else:
            await message.reply(f"❌ Не удалось отправить сообщение клиенту: {err_text}")


@router.message(Command("clear_cache"))
async def cmd_clear_cache(message: Message):
    if not is_admin(message.from_user.id):
        return
    BANNER_CACHE.clear()
    await message.reply("✅ Кеш баннеров успешно очищен.")


@router.message(Command("add"))
async def cmd_add_card(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование:\n"
            "• Карта: `/add +783839293 ОзонБанк Россия`\n"
            "• Крипта: `/add TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe USDT TRC-20`",
            parse_mode="Markdown",
        )
        return

    raw_arg = args[1].strip()
    parts = raw_arg.split()

    if len(parts) >= 2:
        address = parts[0]
        net_part = " ".join(parts[1:])
        net_key = normalize_crypto_key(net_part)
        if net_key:
            CRYPTO_DETAILS.setdefault(net_key, []).append(address)
            queue_db(("crypto_add", net_key, address))
            net_name = CRYPTO_NETWORKS[net_key]["name"]
            total_in_net = len(CRYPTO_DETAILS[net_key])
            await message.reply(
                f"✅ Криптореквизиты добавлены в список ({total_in_net} шт.)!\n"
                f"🪙 <b>{net_name}</b>\n<code>{address}</code>",
                parse_mode="HTML",
            )
            return

    if len(parts) < 3:
        await message.reply("❌ Неверный формат.")
        return
    country = parts[-1]
    bank = parts[-2]
    details = " ".join(parts[:-2])
    CARD_DETAILS.append({
        "details": details,
        "bank": bank,
        "country": country,
        "frozen_until": None,
    })
    queue_db(("card_add", details, bank, country))
    await message.reply(
        f"✅ Реквизиты добавлены!\n🌍 {country} | 🏦 {bank}\n<code>{details}</code>",
        parse_mode="HTML",
    )


@router.message(Command("delete"))
async def cmd_delete_card(message: Message):
    if not is_admin(message.from_user.id):
        return
    global CARD_DETAILS
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование:\n"
            "• Карта: `/delete <реквизиты> <Банк> <Страна>`\n"
            "• Крипта: `/delete <USDT TRC-20 / USDT TON / BEP-20 / BTC>` или"
            " `/delete <адрес> <USDT TRC-20>`",
            parse_mode="Markdown",
        )
        return

    raw_arg = args[1].strip()
    net_key = normalize_crypto_key(raw_arg)
    if net_key:
        if net_key in CRYPTO_DETAILS and CRYPTO_DETAILS[net_key]:
            CRYPTO_DETAILS.pop(net_key, None)
            CRYPTO_POINTERS.pop(net_key, None)
            queue_db(("crypto_delete", net_key))
            await message.reply(
                f"✅ Все криптореквизиты для <b>{CRYPTO_NETWORKS[net_key]['name']}</b>"
                " удалены.",
                parse_mode="HTML",
            )
        else:
            await message.reply("❌ Криптореквизиты для этой сети не найдены.")
        return

    parts = raw_arg.split()
    if len(parts) >= 2:
        addr_candidate = parts[0]
        net_candidate_key = normalize_crypto_key(" ".join(parts[1:]))
        if net_candidate_key:
            addrs = CRYPTO_DETAILS.get(net_candidate_key) or []
            if addr_candidate in addrs:
                CRYPTO_DETAILS[net_candidate_key] = [
                    a for a in addrs if a != addr_candidate
                ]
                if not CRYPTO_DETAILS[net_candidate_key]:
                    CRYPTO_DETAILS.pop(net_candidate_key, None)
                    CRYPTO_POINTERS.pop(net_candidate_key, None)
                queue_db(("crypto_delete_addr", net_candidate_key, addr_candidate))
                await message.reply(
                    f"✅ Кошелек <code>{addr_candidate}</code> удален из"
                    f" <b>{CRYPTO_NETWORKS[net_candidate_key]['name']}</b>.",
                    parse_mode="HTML",
                )
            else:
                await message.reply("❌ Указанный криптокошелек не найден в списке.")
            return

    if len(parts) < 3:
        await message.reply("❌ Неверный формат.")
        return
    country = parts[-1]
    bank = parts[-2]
    details = " ".join(parts[:-2])
    initial_len = len(CARD_DETAILS)
    CARD_DETAILS = [
        c
        for c in CARD_DETAILS
        if not (
            c["details"] == details
            and c["bank"] == bank
            and c["country"].lower() == country.lower()
        )
    ]
    if len(CARD_DETAILS) < initial_len:
        queue_db(("card_delete", details, bank, country))
        await message.reply("✅ Удалено.")
    else:
        await message.reply("❌ Не найдено.")


@router.message(Command("freeze"))
async def cmd_freeze_card(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование: `/freeze <реквизиты> <Банк> <Страна>`",
            parse_mode="Markdown",
        )
        return
    parts = args[1].split()
    if len(parts) < 3:
        await message.reply("❌ Неверный формат.")
        return
    country = parts[-1]
    bank = parts[-2]
    details = " ".join(parts[:-2])
    found = False
    now = datetime.datetime.now()
    for card in CARD_DETAILS:
        if (
            card["details"] == details
            and card["bank"] == bank
            and card["country"].lower() == country.lower()
        ):
            frozen_until = now + datetime.timedelta(hours=24)
            card["frozen_until"] = frozen_until
            queue_db(("card_freeze", details, bank, country, frozen_until))
            found = True
            break
    if found:
        await message.reply("✅ Зафрижено на 24 часа.")
    else:
        await message.reply("❌ Не найдено.")


@router.message(Command("list"))
async def cmd_list_details(message: Message):
    if not is_admin(message.from_user.id):
        return

    now = datetime.datetime.now()
    active_cards = []
    frozen_cards = []

    for card in CARD_DETAILS:
        frozen_until = card.get("frozen_until")
        if frozen_until and frozen_until > now:
            frozen_cards.append((card, frozen_until))
        else:
            if frozen_until and frozen_until <= now:
                card["frozen_until"] = None
                queue_db((
                    "card_freeze",
                    card["details"],
                    card["bank"],
                    card["country"],
                    None,
                ))
            active_cards.append(card)

    active_crypto = []
    for net_key, addresses in CRYPTO_DETAILS.items():
        net_name = CRYPTO_NETWORKS.get(net_key, {}).get("name", net_key.upper())
        for addr in addresses:
            active_crypto.append((net_name, addr))

    active_users_count = len([u for u in KNOWN_USERS if u not in BANNED_USERS])
    active_bills_count = len([u for u in USER_BILLS if u not in BANNED_USERS])

    lines = [
        "📊 <b>Сводка реквизитов и показателей</b>",
        f"• Активных карт/переводов: <b>{len(active_cards)}</b> | Зафрижено: <b>{len(frozen_cards)}</b>",
        f"• Активных криптокошельков: <b>{len(active_crypto)}</b>",
        f"• Пользователей в базе: <b>{active_users_count}</b> (банов: <b>{len(BANNED_USERS)}</b>) | Счетов: <b>{active_bills_count}</b>\n",
        "━━━━━━━━━━━━━━",
        "💳 <b>Актуальные карты / переводы:</b>",
    ]

    if active_cards:
        for idx, card in enumerate(active_cards, 1):
            lines.append(
                f"  {idx}. 🌍 <b>{card['country']}</b> · 🏦 <b>{card['bank']}</b>\n"
                f"      └ <code>{card['details']}</code>"
            )
    else:
        lines.append("  • Нет активных реквизитов")

    lines.append("\n🪙 <b>Актуальная криптовалюта:</b>")
    if active_crypto:
        for idx, (net_name, address) in enumerate(active_crypto, 1):
            lines.append(
                f"  {idx}. <b>{net_name}</b>\n"
                f"      └ <code>{address}</code>"
            )
    else:
        lines.append("  • Нет активных криптокошельков")

    lines.append("\n❄️ <b>Зафриженные на 24 часа:</b>")
    if frozen_cards:
        for idx, (card, until_dt) in enumerate(frozen_cards, 1):
            remaining = until_dt - now
            total_minutes = max(1, int(remaining.total_seconds() // 60))
            rem_hours, rem_mins = divmod(total_minutes, 60)
            lines.append(
                f"  {idx}. 🌍 <b>{card['country']}</b> · 🏦 <b>{card['bank']}</b>\n"
                f"      └ <code>{card['details']}</code> (еще {rem_hours}ч {rem_mins}м, до {until_dt.strftime('%d.%m %H:%M')})"
            )
    else:
        lines.append("  • Нет зафриженных реквизитов")

    await message.reply("\n".join(lines), parse_mode="HTML")


# === CRYPTOBOT ИНТЕГРАЦИЯ ===
async def create_cryptobot_invoice(amount_usd: float, description: str):
    if not CRYPTO_BOT_TOKEN:
        return "", None
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(_format_num(amount_usd)),
        "description": description,
        "payload": "order_payment",
    }
    try:
        async with ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=10
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        res = data["result"]
                        return res["pay_url"], res["invoice_id"]
    except Exception as e:
        logger.error(f"Ошибка CryptoBot: {e}")
    return "", None


async def check_cryptobot_invoice_status(invoice_id: int) -> bool:
    if not CRYPTO_BOT_TOKEN or not invoice_id:
        return True
    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    params = {"invoice_ids": str(invoice_id)}
    try:
        async with ClientSession() as session:
            async with session.get(
                url, params=params, headers=headers, timeout=10
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        items = data.get("result", {}).get("items", [])
                        if items:
                            return items[0].get("status") == "paid"
    except Exception as e:
        logger.error(f"Ошибка проверки инвойса: {e}")
    return False


# === ФОНОВЫЕ ЗАДАЧИ ===
async def scheduled_broadcast(bot: Bot):
    while True:
        await asyncio.sleep(3600)
        now = datetime.datetime.now()
        banner_path = get_banner_path("special_offer.jpg")
        for user_id, last_seen_time in list(KNOWN_USERS.items()):
            if user_id in BANNED_USERS:
                continue
            ref_time = USER_LAST_BROADCAST.get(user_id, last_seen_time)
            if ref_time is None or (now - ref_time).total_seconds() >= 48 * 3600:
                if user_id in BANNED_USERS:
                    continue
                try:
                    today = datetime.date.today()
                    target_date = today + datetime.timedelta(days=3)
                    months_ru = [
                        "",
                        "января",
                        "февраля",
                        "марта",
                        "апреля",
                        "мая",
                        "июня",
                        "июля",
                        "августа",
                        "сентября",
                        "октября",
                        "ноября",
                        "декабря",
                    ]
                    text = (
                        "✨ <b>Специальное предложение</b>\n\n"
                        "Действует персональная скидка <b>20%</b> до"
                        f" <b>{target_date.day} {months_ru[target_date.month]}</b>.\n\n"
                        "Ждем вас в боте 👇"
                    )
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✨ Оформить со скидкой",
                                    callback_data="start_order",
                                    style="danger",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="🏠 На главную",
                                    callback_data="back_home",
                                    style="primary",
                                )
                            ],
                        ]
                    )

                    if "special_offer.jpg" in BANNER_CACHE:
                        photo = BANNER_CACHE["special_offer.jpg"]
                        await bot.send_photo(
                            chat_id=user_id,
                            photo=photo,
                            caption=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                    elif banner_path and os.path.exists(banner_path):
                        photo = FSInputFile(banner_path)
                        sent = await bot.send_photo(
                            chat_id=user_id,
                            photo=photo,
                            caption=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                        if sent.photo:
                            BANNER_CACHE["special_offer.jpg"] = sent.photo[-1].file_id
                    else:
                        await bot.send_message(
                            chat_id=user_id,
                            text=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )

                    USER_LAST_BROADCAST[user_id] = now
                    queue_db(("last_broadcast", user_id, now))
                except Exception as e:
                    logger.error(f"Ошибка рассылки: {e}")


async def delayed_abandoned_cart_reminder(
    bot: Bot, user_id: int, remind_at: datetime.datetime
):
    try:
        delay = max(0.0, (remind_at - datetime.datetime.now()).total_seconds())
        if delay > 0:
            await asyncio.sleep(delay)

        ABANDONED_CHECKOUTS.pop(user_id, None)
        ABANDONED_DEADLINES.pop(user_id, None)
        queue_db(("abandoned_delete", user_id))

        if user_id in BANNED_USERS:
            return
        if not _has_active_unpaid_item(user_id):
            return

        reminder_text = (
            "👋 <b>Завершить заказ со скидкой -20%?</b>\n\nВы остановились на"
            " этапе оплаты. Возвращайтесь к оформлению 👇"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🛒 Вернуться к заказу",
                        callback_data="start_order",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Поддержка",
                        callback_data="contact_admin",
                        style="danger",
                    )
                ],
            ]
        )

        banner_path = get_banner_path("abandoned_cart.jpg")
        if "abandoned_cart.jpg" in BANNER_CACHE:
            photo = BANNER_CACHE["abandoned_cart.jpg"]
            await bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=reminder_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        elif banner_path and os.path.exists(banner_path):
            photo = FSInputFile(banner_path)
            sent = await bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=reminder_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            if sent.photo:
                BANNER_CACHE["abandoned_cart.jpg"] = sent.photo[-1].file_id
        else:
            await bot.send_message(
                chat_id=user_id,
                text=reminder_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    except asyncio.CancelledError:
        pass


async def restore_abandoned_reminders(bot: Bot):
    for user_id, remind_at in list(ABANDONED_DEADLINES.items()):
        if user_id in BANNED_USERS or not _has_active_unpaid_item(user_id):
            ABANDONED_DEADLINES.pop(user_id, None)
            queue_db(("abandoned_delete", user_id))
            continue
        _schedule_abandoned_reminder(bot, user_id, remind_at)


# === ОБРАБОТЧИКИ КОМАНД ===
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await _full_reset_user_session(message.from_user.id, state, message.from_user.username)
    await show_main_menu(message, state)


@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    await _full_reset_user_session(message.from_user.id, state, message.from_user.username)
    try:
        await message.reply("🔄 Бот перезапущен, состояние очищено.")
    except Exception:
        pass
    await show_main_menu(message, state)


@router.message(Command("order"))
async def cmd_order(message: Message, state: FSMContext):
    await _show_catalog_step(message, state, record_history=True)


@router.message(Command("my_orders"))
async def cmd_my_orders_msg(message: Message, state: FSMContext):
    user_id = message.from_user.id
    _touch_user_activity(user_id, message.from_user.username)
    orders = _get_client_paid_orders(user_id)
    if not orders:
        text = (
            "📦 <b>Мои заказы</b>\n\nУ вас пока нет оплаченных или оформленных"
            " заказов."
        )
    else:
        text = "📦 <b>История ваших заказов:</b>\n\n"
        for idx, ord_data in enumerate(orders, 1):
            price_str = (
                f"{_format_num(ord_data['total_price'])}$"
                if float(ord_data["total_price"]) > 0
                else "Индивидуально"
            )
            text += (
                f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма:"
                f" {price_str}\n• Статус: {ord_data['status']}\n\n"
            )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_home", style="primary"
            )
        ]]
    )
    await render_step(message, state, "my_orders.jpg", text, keyboard)


@router.message(Command("reputation"))
async def cmd_reputation_msg(message: Message, state: FSMContext):
    _touch_user_activity(message.from_user.id, message.from_user.username)
    completed, in_progress, new_requests = get_reputation_stats()
    text = (
        "📊 <b>Репутация сервиса</b>\n\n"
        f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
        f"• В активной работе: <b>{in_progress}</b>\n"
        f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
        "<i>Конфиденциальность и безопасность гарантированы.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_home", style="primary"
            )
        ]]
    )
    await render_step(message, state, "reputation.jpg", text, keyboard)


@router.callback_query(F.data == "back_home")
async def back_home(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _cancel_abandoned_reminder(user_id)
    await show_main_menu(callback, state)


@router.callback_query(F.data == "reputation")
async def show_reputation(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    _touch_user_activity(callback.from_user.id, callback.from_user.username)
    completed, in_progress, new_requests = get_reputation_stats()
    text = (
        "📊 <b>Репутация сервиса</b>\n\n"
        f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
        f"• В активной работе: <b>{in_progress}</b>\n"
        f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
        "<i>Конфиденциальность и безопасность гарантированы.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_home", style="primary"
            )
        ]]
    )
    await render_step(callback, state, "reputation.jpg", text, keyboard)


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _touch_user_activity(user_id, callback.from_user.username)
    orders = _get_client_paid_orders(user_id)
    if not orders:
        text = (
            "📦 <b>Мои заказы</b>\n\nУ вас пока нет оплаченных или оформленных"
            " заказов."
        )
    else:
        text = "📦 <b>История ваших заказов:</b>\n\n"
        for idx, ord_data in enumerate(orders, 1):
            price_str = (
                f"{_format_num(ord_data['total_price'])}$"
                if float(ord_data["total_price"]) > 0
                else "Индивидуально"
            )
            text += (
                f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма:"
                f" {price_str}\n• Статус: {ord_data['status']}\n\n"
            )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_home", style="primary"
            )
        ]]
    )
    await render_step(callback, state, "my_orders.jpg", text, keyboard)


@router.callback_query(F.data == "contact_admin")
async def contact_admin_info(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    _touch_user_activity(callback.from_user.id, callback.from_user.username)
    await state.set_state(SupportForm.reason)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Отмена", callback_data="back_home", style="danger"
            )
        ]]
    )
    text = (
        "💬 <b>Служба поддержки</b>\n\n"
        "Опишите вашу ситуацию или задайте вопрос (оплата, статус заказа и т.д.):"
    )
    await render_step(callback, state, "support.jpg", text, keyboard)


@router.message(SupportForm.reason, F.text)
async def process_support_reason(message: Message, state: FSMContext):
    user_id = message.from_user.id
    _touch_user_activity(user_id, message.from_user.username)
    reason_text = message.text.strip()
    await state.update_data(reason=reason_text)
    data = await state.get_data()
    _record_problem_user(
        user_id=user_id,
        kind="support",
        step_label="Обращение в поддержку",
        query_text=reason_text,
        data=data,
    )
    await state.set_state(SupportForm.photo)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏩ Пропустить",
                    callback_data="skip_support_photo",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Отмена", callback_data="back_home", style="danger"
                )
            ],
        ]
    )
    text = "📸 Отправьте фото при необходимости или нажмите пропустить:"
    await render_step(message, state, "support.jpg", text, keyboard)


@router.message(SupportForm.photo, F.photo)
async def process_support_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    user = message.from_user
    _touch_user_activity(user.id, user.username)
    _record_problem_user(
        user_id=user.id,
        kind="support",
        step_label="Обращение в поддержку",
        query_text=data.get("reason") or "",
        data=data,
    )
    user_info = f"@{user.username}" if user.username else "без @username"
    body_text = (
        f"• От кого: {user_info} (ID: <code>{user.id}</code>)\n"
        f"• Вопрос: {data.get('reason')}"
    )
    await _send_staff_ticket_menu(
        bot=message.bot,
        user_id=user.id,
        kind="support",
        header_title="📩 <b>Новое обращение в поддержку</b>",
        body_text=body_text,
        extra_data=data,
        photo_id=photo_id,
    )
    text = "✅ Обращение отправлено! Специалист скоро свяжется с вами."
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(message, state, "main.jpg", text, keyboard)
    await state.clear()


@router.callback_query(SupportForm.photo, F.data == "skip_support_photo")
async def skip_support_photo(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    data = await state.get_data()
    user = callback.from_user
    _touch_user_activity(user.id, user.username)
    _record_problem_user(
        user_id=user.id,
        kind="support",
        step_label="Обращение в поддержку",
        query_text=data.get("reason") or "",
        data=data,
    )
    user_info = f"@{user.username}" if user.username else "без @username"
    body_text = (
        f"• От кого: {user_info} (ID: <code>{user.id}</code>)\n"
        f"• Вопрос: {data.get('reason')}"
    )
    await _send_staff_ticket_menu(
        bot=callback.bot,
        user_id=user.id,
        kind="support",
        header_title="📩 <b>Новое обращение в поддержку</b>",
        body_text=body_text,
        extra_data=data,
    )
    text = "✅ Обращение отправлено! Специалист скоро свяжется с вами."
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(callback, state, "main.jpg", text, keyboard)
    await state.clear()


# === КАТАЛОГ И ОФОРМЛЕНИЕ ЗАКАЗА ===
@router.callback_query(F.data == "start_order")
async def process_start_order(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _cancel_abandoned_reminder(user_id)
    await state.update_data(nav_history=["service"])
    await _show_catalog_step(callback, state, record_history=False)


@router.callback_query(OrderForm.service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    parts = callback.data.split("_")
    price = int(parts[-1])
    service_name = " ".join(parts[1:-1]).replace("_", " ")

    _clear_back_after_service_flags(callback.from_user.id)
    _track_user_step(
        callback.from_user.id,
        "catalog",
        callback.from_user.username,
        service_name=service_name,
    )
    await state.update_data(
        service_name=service_name,
        service_price=price,
        nav_history=["service"],
    )

    if service_name == "Вернуть долг":
        _record_problem_user(
            user_id=callback.from_user.id,
            kind="debt_return",
            step_label=STEP_LABELS["debt_info"],
            query_text=service_name,
            data={"service_name": service_name, "service_price": price},
        )
        await _show_debt_info_step(callback, state, record_history=True)
    elif service_name == "Особый заказ":
        _record_problem_user(
            user_id=callback.from_user.id,
            kind="special_order",
            step_label=STEP_LABELS["special_task"],
            query_text=service_name,
            data={"service_name": service_name, "service_price": price},
        )
        await _show_special_task_step(callback, state, record_history=True)
    else:
        await show_country_selection(callback, state, service_name, price, record_history=True)


async def show_country_selection(
    event: Message | CallbackQuery,
    state: FSMContext,
    service_name: str,
    price: int,
    record_history: bool = True,
):
    _track_user_step(
        event.from_user.id,
        "country",
        event.from_user.username,
        service_name=service_name,
    )
    await state.set_state(OrderForm.country)
    if record_history:
        await _push_nav_step(state, "country")
    countries = [
        ("🇷🇺 Россия", "country_Russia"),
        ("🇧🇾 Беларусь", "country_Belarus"),
        ("🇺🇦 Украина", "country_Ukraine"),
        ("🇰🇿 Казахстан", "country_Kazakhstan"),
        ("🇺🇿 Узбекистан", "country_Uzbekistan"),
        ("🇲🇩 Молдова", "country_Moldova"),
    ]
    kb_rows = []
    for i in range(0, len(countries), 2):
        row = [
            InlineKeyboardButton(
                text=countries[i][0], callback_data=countries[i][1], style="primary"
            )
        ]
        if i + 1 < len(countries):
            row.append(
                InlineKeyboardButton(
                    text=countries[i + 1][0],
                    callback_data=countries[i + 1][1],
                    style="danger",
                )
            )
        kb_rows.append(row)
    kb_rows.append([
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data="order_back_step", style="primary"
        )
    ])

    price_str = f"{price}$" if price > 0 else "Расчет менеджера"
    text = (
        f"🎯 Выбрано: <b>{service_name}</b> ({price_str})\n\n🌍 <b>Страна"
        " вашей цели</b>\n\nВыберите страну из списка:"
    )
    await render_step(
        event,
        state,
        "country.jpg",
        text,
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.message(OrderForm.debt_info, F.text)
async def process_debt_info(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "debt_info", message.from_user.username)
    await state.update_data(debt_info=message.text.strip())
    data = await state.get_data()
    _record_problem_user(
        user_id=message.from_user.id,
        kind="debt_return",
        step_label=STEP_LABELS["debt_wallet"],
        query_text=data.get("debt_info") or "",
        data=data,
    )
    await _show_debt_wallet_step(message, state, record_history=True)


@router.message(OrderForm.debt_wallet, F.text)
async def process_debt_wallet(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "debt_wallet", message.from_user.username)
    await state.update_data(debt_wallet=message.text.strip())
    data = await state.get_data()
    _record_problem_user(
        user_id=message.from_user.id,
        kind="debt_return",
        step_label=STEP_LABELS["country"],
        query_text=data.get("debt_info") or "",
        data=data,
    )
    await show_country_selection(
        message, state, data.get("service_name"), data.get("service_price"), record_history=True
    )


@router.message(OrderForm.special_task, F.text)
async def process_special_task(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "special_task", message.from_user.username)
    await state.update_data(special_task=message.text.strip())
    data = await state.get_data()
    _record_problem_user(
        user_id=message.from_user.id,
        kind="special_order",
        step_label=STEP_LABELS["country"],
        query_text=data.get("special_task") or "",
        data=data,
    )
    await show_country_selection(
        message, state, data.get("service_name"), data.get("service_price"), record_history=True
    )


@router.callback_query(OrderForm.country, F.data.startswith("country_"))
async def process_country(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    c_code = callback.data.split("_")[1]
    names = {
        "Russia": "Россия",
        "Belarus": "Беларусь",
        "Ukraine": "Украина",
        "Kazakhstan": "Казахстан",
        "Uzbekistan": "Узбекистан",
        "Moldova": "Молдова",
    }
    selected = names.get(c_code, c_code)
    USER_LAST_COUNTRY[callback.from_user.id] = selected
    queue_db(("last_country", callback.from_user.id, selected))
    curr_info = COUNTRY_CURRENCIES.get(
        selected, {"code": "USD", "symbol": "USD"}
    )
    await state.update_data(
        country=selected,
        curr_code=curr_info["code"],
        curr_symbol=curr_info["symbol"],
    )
    data = await state.get_data()
    if (data.get("service_name") or "").lower() == "особый заказ":
        _record_problem_user(
            user_id=callback.from_user.id,
            kind="special_order",
            step_label=STEP_LABELS["target_location"],
            query_text=data.get("special_task") or "",
            data=data,
        )
    elif data.get("service_name") == "Вернуть долг":
        _record_problem_user(
            user_id=callback.from_user.id,
            kind="debt_return",
            step_label=STEP_LABELS["target_location"],
            query_text=data.get("debt_info") or "",
            data=data,
        )
    await _show_target_location_step(callback, state, record_history=True)


@router.message(OrderForm.target_location, F.text)
async def process_target_location(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "target_location", message.from_user.username)
    await state.update_data(target_location=message.text.strip())
    data = await state.get_data()
    if (data.get("service_name") or "").lower() == "особый заказ":
        _record_problem_user(
            user_id=message.from_user.id,
            kind="special_order",
            step_label=STEP_LABELS["target_data"],
            query_text=data.get("special_task") or "",
            data=data,
        )
    elif data.get("service_name") == "Вернуть долг":
        _record_problem_user(
            user_id=message.from_user.id,
            kind="debt_return",
            step_label=STEP_LABELS["target_data"],
            query_text=data.get("debt_info") or "",
            data=data,
        )
    await _show_target_data_step(message, state, record_history=True)


@router.message(OrderForm.target_data, F.text)
async def process_target_data(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "target_data", message.from_user.username)
    existing_data = await state.get_data()
    existing_photos = existing_data.get("photos") or []
    await state.update_data(target_data=message.text.strip(), photos=existing_photos)
    data = await state.get_data()
    if (data.get("service_name") or "").lower() == "особый заказ":
        _record_problem_user(
            user_id=message.from_user.id,
            kind="special_order",
            step_label=STEP_LABELS["photos"],
            query_text=data.get("special_task") or "",
            data=data,
        )
    elif data.get("service_name") == "Вернуть долг":
        _record_problem_user(
            user_id=message.from_user.id,
            kind="debt_return",
            step_label=STEP_LABELS["photos"],
            query_text=data.get("debt_info") or "",
            data=data,
        )
    await _show_photos_step(message, state, record_history=True)


@router.message(OrderForm.photos, F.photo)
async def process_photo(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "photos", message.from_user.username)
    await asyncio.sleep(0.3)

    data = await state.get_data()
    photos = list(data.get("photos", []))
    if len(photos) >= 3:
        await message.answer("⚠️ Добавлено максимальное количество фото (3 шт.).")
        return

    photo_id = message.photo[-1].file_id
    if photo_id not in photos:
        photos.append(photo_id)
        await state.update_data(photos=photos)

    await _show_photos_step(message, state, record_history=False)


@router.callback_query(OrderForm.photos, F.data == "finish_photos")
async def finish_photos(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    await _show_urgency_step(callback, state, record_history=True)


@router.callback_query(OrderForm.urgency, F.data.startswith("urg_"))
async def process_urgency(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    parts = callback.data.split("_")
    urg_price = int(parts[2])
    urg_map = {
        "fast": "⚡ За 2-3 часа",
        "tomorrow": "🕐 Завтра",
        "standard": "📅 2-5 дней",
    }
    urgency = urg_map.get(parts[1], "📅 2-5 дней")
    await state.update_data(urgency=urgency, urgency_price=urg_price)
    await _show_summary_step(callback, state, record_history=True)


@router.callback_query(F.data == "submit_special_order")
async def process_special_order_submit(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user = callback.from_user
    user_id = user.id
    data = await state.get_data()
    service_name = data.get("service_name") or "Особый заказ"

    _reset_payment_step_tracking(user_id, leave_step=True)
    _cancel_abandoned_reminder(user_id)
    _clear_back_after_service_flags(user_id)
    USER_BILLS.pop(user_id, None)
    queue_db(("bill_delete", user_id))
    _track_user_step(user_id, "paid", user.username, service_name=service_name)
    _record_problem_user(
        user_id=user_id,
        kind="special_order",
        step_label=STEP_LABELS["paid"],
        query_text=data.get("special_task") or service_name,
        data=data,
    )

    order_idx = len(USER_ORDERS.get(user_id) or [])

    user_tag = f"@{user.username}" if user.username else "без @username"
    extra_info = f"\n• Задача: {data.get('special_task', 'Не указано')}"
    if data.get("country"):
        extra_info += f"\n• Страна: {data.get('country')}"
    if data.get("target_location"):
        extra_info += f"\n• Локация: {data.get('target_location')}"
    if data.get("target_data"):
        extra_info += f"\n• Данные цели: {data.get('target_data')}"
    if data.get("urgency"):
        extra_info += f"\n• Срочность: {data.get('urgency')}"
    target_photos = data.get("photos") or []
    order_kb = _build_paid_order_keyboard(user.id, order_index=order_idx, in_work=False)

    for staff_id in get_staff_recipients():
        try:
            if target_photos:
                await callback.bot.send_photo(
                    chat_id=staff_id,
                    photo=target_photos[0],
                    caption=(
                        f"🎯 <b>Заявка на расчет стоимости (Особый заказ) от {user_tag} (ID:"
                        f" <code>{user.id}</code>)</b>{extra_info}"
                    ),
                    reply_markup=order_kb,
                    parse_mode="HTML",
                )
                for p_id in target_photos[1:]:
                    await callback.bot.send_photo(
                        chat_id=staff_id,
                        photo=p_id,
                        caption=(
                            f"📸 Доп. фото цели по особому заказу от {user_tag} (ID:"
                            f" <code>{user.id}</code>)"
                        ),
                        parse_mode="HTML",
                    )
            else:
                await callback.bot.send_message(
                    chat_id=staff_id,
                    text=(
                        f"🎯 <b>Заявка на расчет стоимости (Особый заказ) от {user_tag} (ID:"
                        f" <code>{user.id}</code>)</b>{extra_info}"
                    ),
                    reply_markup=order_kb,
                    parse_mode="HTML",
                )
        except Exception:
            pass

    text = (
        "✅ <b>Заявка отправлена менеджеру!</b>\n\nОн свяжется с вами в течение 25 минут"
        " для расчета стоимости."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(callback, state, "main.jpg", text, keyboard)
    await state.clear()


@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    await state.set_state(OrderForm.payment)
    await _push_nav_step(state, "payment")
    _track_user_step(user_id, "payment", callback.from_user.username)
    data, country, curr_code, curr_symbol, total_price, service_name = (
        await get_payment_context(user_id, state)
    )

    if service_name.strip().lower() == "особый заказ" and user_id not in USER_BILLS:
        await process_special_order_submit(callback, state)
        return

    _enter_payment_method_step(callback.bot, user_id)

    remind_at = datetime.datetime.now() + datetime.timedelta(minutes=30)
    queue_db(("abandoned_set", user_id, remind_at))
    _schedule_abandoned_reminder(callback.bot, user_id, remind_at)

    rate = await get_exchange_rate(curr_code)
    local_amount = round(float(total_price) * rate) if float(total_price) > 0 else 0
    orig_price = data.get("original_total_price") or total_price
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    has_disc = disc_pct > 0 and float(orig_price) > float(total_price or 0)

    is_crypto_only = float(total_price) > 500

    if is_crypto_only:
        if has_disc:
            price_note = (
                f"Сумма: <s>{_format_num(orig_price)} USDT</s> → "
                f"<b>{_format_num(total_price)} USDT</b> <b>(-{disc_pct}%)</b>"
            )
        else:
            price_note = f"Сумма: <b>{_format_num(total_price)} USDT</b>"
        payment_text = (
            "⏳ <b>Оплата заказа</b>\n"
            "Время на завершение: <b><u>20:00</u></b>\n\n"
            "⚠️ <b>Внимание:</b> Для сумм свыше 500$ оплата"
            " картами недоступна. Доступен CryptoBot и криптовалюта.\n\n"
            f"{price_note}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤖 CryptoBot (без комиссии)",
                        callback_data="pay_cryptobot",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🪙 Криптовалюта",
                        callback_data="crypto_expert",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🎟 Промокод",
                        callback_data="promo_code_entry",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Нужна помощь",
                        callback_data="payment_help_manager",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="order_back_step", style="primary"
                    )
                ],
            ]
        )
    else:
        bill_prefix = (
            f"🧾 <b>Счет на {service_name}</b>\n" if user_id in USER_BILLS else ""
        )
        if has_disc:
            sum_line = (
                f"Сумма: <s>{_format_num(orig_price)}$</s> → "
                f"<b>{_format_num(total_price)}$</b> <b>(-{disc_pct}%)</b> "
                f"<i>(≈ {local_amount:,} {curr_symbol})</i>"
            )
        else:
            sum_line = (
                f"Сумма: <b>{_format_num(total_price)}$</b> <i>(≈ {local_amount:,}"
                f" {curr_symbol})</i>"
            )
        payment_text = (
            "⏳ <b>Оплата заказа</b>\n"
            "Время на завершение: <b><u>20:00</u></b>\n\n"
            f"{bill_prefix}"
            f"💳 <b>Способ оплаты ({country})</b>\n"
            f"{sum_line}"
        )
        keyboard = build_payment_keyboard(country, back_callback="order_back_step")

    await render_step(callback, state, "payment.jpg", payment_text, keyboard)


@router.callback_query(F.data == "pay_card_option")
async def process_pay_card_option(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "payment_method", callback.from_user.username)
    await state.update_data(payment_method="card")
    data, country, curr_code, curr_symbol, total_price, _ = (
        await get_payment_context(user_id, state)
    )
    rate = await get_exchange_rate(curr_code)
    local_amount = round(float(total_price) * rate)
    orig_price = data.get("original_total_price") or total_price
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    has_disc = disc_pct > 0 and float(orig_price) > float(total_price or 0)

    card = get_next_card(country)
    if not card:
        text = (
            "❌ <b>Реквизиты временно недоступны</b>\n\n"
            f"В данный момент реквизиты для страны <b>{country}</b> недоступны."
            " Воспользуйтесь CryptoBot или криптовалютой 👇"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤖 CryptoBot",
                        callback_data="pay_cryptobot",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🪙 Криптовалюта",
                        callback_data="crypto_expert",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                    )
                ],
            ]
        )
        await render_step(callback, state, "payment.jpg", text, keyboard)
        return

    header_map = {
        "Россия": "Оплата по СБП",
        "Казахстан": "Оплата на Kaspi",
        "Украина": "Оплата по реквизитам",
        "Беларусь": "Оплата по реквизитам",
        "Узбекистан": "Оплата по реквизитам",
        "Молдова": "Оплата по реквизитам",
    }
    header_title = header_map.get(country, "Оплата")

    if has_disc:
        amount_line = (
            f"• Итого к оплате: <b>{local_amount:,} {curr_symbol}</b> "
            f"(<s>{_format_num(orig_price)}$</s> → <b>{_format_num(total_price)}$</b> "
            f"<b>-{disc_pct}%</b>)"
        )
    else:
        amount_line = (
            f"• Итого к оплате: <b>{local_amount:,} {curr_symbol}</b>"
            f" <i>({_format_num(total_price)}$)</i>"
        )

    text = (
        f"💳 <b>{header_title} ({country})</b>\n\n"
        f"{amount_line}\n\n"
        f"⚠️ <b>Переводите строго на реквизиты ниже:</b>\n\n"
        f"<b>Банк:</b> {card['bank']}\n"
        f"<b>Реквизиты:</b> <code>{card['details']}</code>\n\n"
        "💡 <i>После перевода нажмите кнопку ниже для отправки чека.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Прикрепить чек",
                    callback_data="ask_for_receipt",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                )
            ],
        ]
    )
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "payment_help_manager")
async def process_payment_help_manager(callback: CallbackQuery, state: FSMContext):
    user = callback.from_user
    _touch_user_activity(user.id, user.username)
    data = await state.get_data()
    _record_problem_user(
        user_id=user.id,
        kind="payment_help",
        step_label=STEP_LABELS["payment"],
        query_text="Запрос помощи с оплатой",
        data=data,
    )
    user_tag = f"@{user.username}" if user.username else "без @username"
    body = (
        f"• От кого: {user_tag} (ID: <code>{user.id}</code>)\n"
        "• Ситуация: клиент запросил помощь с оплатой заказа."
    )
    await _send_staff_ticket_menu(
        bot=callback.bot,
        user_id=user.id,
        kind="payment_help",
        header_title="🚨 <b>Проблема / запрос помощи с оплатой</b>",
        body_text=body,
        extra_data=data,
    )
    await callback.answer(
        "✅ Менеджер уведомлен, скоро свяжется с вами.", show_alert=True
    )


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "payment_method", callback.from_user.username)
    data, _, _, _, total_price, _ = await get_payment_context(
        user_id, state
    )
    if float(total_price) <= 0:
        total_price = 100

    orig_price = data.get("original_total_price") or total_price
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    has_disc = disc_pct > 0 and float(orig_price) > float(total_price)

    pay_url, inv_id = await create_cryptobot_invoice(float(total_price), "Оплата услуг")
    if not pay_url:
        await callback.answer("❌ Сервис недоступен.", show_alert=True)
        return

    await state.update_data(invoice_id=inv_id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Оплатить счет", url=pay_url, style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Проверить оплату",
                    callback_data="cryptobot_check_success",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                )
            ],
        ]
    )
    if has_disc:
        sum_txt = (
            f"<s>{_format_num(orig_price)} USDT</s> → "
            f"<b>{_format_num(total_price)} USDT</b> <b>(-{disc_pct}%)</b>"
        )
    else:
        sum_txt = f"<b>{_format_num(total_price)} USDT</b>"

    text = (
        f"🤖 <b>CryptoBot</b>\n\nСумма: {sum_txt}\n\nПосле оплаты"
        " нажмите кнопку проверки:"
    )
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "cryptobot_check_success")
async def process_cryptobot_check(callback: CallbackQuery, state: FSMContext):
    data, country, _, _, total_price, service_desc = await get_payment_context(
        callback.from_user.id, state
    )
    inv_id = data.get("invoice_id")

    is_paid = await check_cryptobot_invoice_status(inv_id)
    if not is_paid:
        await callback.answer(
            "⏳ Платеж еще не поступил. Попробуйте через минуту.", show_alert=True
        )
        return

    try:
        await callback.answer()
    except Exception:
        pass
    user = callback.from_user
    _reset_payment_step_tracking(user.id, leave_step=True)
    _cancel_abandoned_reminder(user.id)
    _clear_back_after_service_flags(user.id)
    PROBLEM_USERS.pop(user.id, None)
    _track_user_step(user.id, "paid", user.username, service_name=service_desc)

    if user.id not in USER_ORDERS:
        USER_ORDERS[user.id] = []

    order_idx = len(USER_ORDERS[user.id])
    USER_ORDERS[user.id].append({
        "service_name": service_desc,
        "total_price": total_price,
        "status": "✅ Оплачено через CryptoBot",
    })
    queue_db((
        "order_add",
        user.id,
        service_desc,
        total_price,
        "✅ Оплачено через CryptoBot",
    ))
    USER_BILLS.pop(user.id, None)
    queue_db(("bill_delete", user.id))

    order_details = _build_order_details_text(
        data, country, service_desc, total_price
    )
    user_tag = f"@{user.username}" if user.username else "без @username"
    staff_text = (
        f"🤖 Успешная оплата CryptoBot от {user_tag} (ID:"
        f" <code>{user.id}</code>){order_details}"
    )
    target_photos = data.get("photos") or []
    order_kb = _build_paid_order_keyboard(user.id, order_index=order_idx, in_work=False)

    for staff_id in get_staff_recipients():
        try:
            await callback.bot.send_message(
                chat_id=staff_id,
                text=staff_text,
                reply_markup=order_kb,
                parse_mode="HTML",
            )
            for p_id in target_photos:
                await callback.bot.send_photo(
                    chat_id=staff_id,
                    photo=p_id,
                    caption=(
                        f"📸 Фото цели по заказу CryptoBot от {user_tag} (ID:"
                        f" <code>{user.id}</code>)"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    text = "✅ <b>Оплата подтверждена!</b>\n\nВаша заявка принята в работу."
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(callback, state, "main.jpg", text, keyboard)
    await state.clear()


@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "payment_method", callback.from_user.username)
    await state.update_data(payment_method="crypto")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 USDT (TRC-20)",
                    callback_data="pay_crypto_trc20",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💵 USDT (BEP-20)",
                    callback_data="pay_crypto_bep20",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 USDT (TON)",
                    callback_data="pay_crypto_ton",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Bitcoin (BTC)",
                    callback_data="pay_crypto_btc",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                )
            ],
        ]
    )
    text = "🪙 Выберите криптовалютную сеть для перевода:"
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data.startswith("pay_crypto_"))
async def process_pay_crypto(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "payment_method", callback.from_user.username)
    await state.update_data(payment_method="crypto")
    net_key = callback.data.split("_")[2]
    net_info = CRYPTO_NETWORKS.get(net_key)
    if not net_info:
        await callback.answer("Сеть не найдена", show_alert=True)
        return

    address = get_next_crypto(net_key)
    if not address:
        text = (
            "❌ <b>Реквизиты временно недоступны</b>\n\n"
            f"В данный момент реквизиты для сети <b>{net_info['name']}</b>"
            " недоступны, мы уже решаем эту проблему. Оплатите другим способом"
            " (например, через CryptoBot или карту) 👇"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤖 CryptoBot",
                        callback_data="pay_cryptobot",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💳 Карта / СБП",
                        callback_data="pay_card_option",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="crypto_expert", style="primary"
                    )
                ],
            ]
        )
        await render_step(callback, state, "payment.jpg", text, keyboard)
        return

    data, _, curr_code, curr_symbol, total_price, _ = await get_payment_context(
        user_id, state
    )
    rate = await get_exchange_rate(curr_code)
    local_amount = round(float(total_price) * rate) if float(total_price) > 0 else 0
    orig_price = data.get("original_total_price") or total_price
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    has_disc = disc_pct > 0 and float(orig_price) > float(total_price or 0)

    if float(total_price) > 0 and has_disc:
        price_str = (
            f"<s>{_format_num(orig_price)}$</s> → <b>{_format_num(total_price)}$</b> "
            f"<b>(-{disc_pct}%)</b> <i>(≈ {local_amount:,} {curr_symbol})</i>"
        )
    elif float(total_price) > 0:
        price_str = (
            f"<b>{_format_num(total_price)}$</b> <i>(≈ {local_amount:,} {curr_symbol})</i>"
        )
    else:
        price_str = "<b>Индивидуальный расчет</b>"

    text = (
        f"💳 <b>Оплата {net_info['name']}</b>\n\n"
        f"• Сумма: {price_str}\n"
        f"• Сеть: <b>{net_info['network']}</b>\n"
        f"• Реквизиты:\n<code>{address}</code>\n\n"
        "💡 <i>После перевода прикрепите фото скриншот оплаты перевода.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Прикрепить чек",
                    callback_data="ask_for_receipt",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="crypto_expert", style="primary"
                )
            ],
        ]
    )
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "receipt", callback.from_user.username)
    data = await state.get_data()
    payment_method = data.get("payment_method", "card")
    await state.set_state(OrderForm.receipt)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="go_to_payment", style="primary"
            )
        ]]
    )
    if payment_method == "crypto":
        text = (
            "🧾 Отправьте фото скриншот оплаты перевода следующим сообщением:"
        )
    else:
        text = (
            "🧾 Отправьте фото чека или документ квитанции об оплате следующим"
            " сообщением:"
        )
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
    user = message.from_user
    _reset_payment_step_tracking(user.id, leave_step=True)
    _cancel_abandoned_reminder(user.id)
    _clear_back_after_service_flags(user.id)
    PROBLEM_USERS.pop(user.id, None)
    data, country, _, _, total_price, service_desc = await get_payment_context(
        user.id, state
    )
    _track_user_step(user.id, "paid", user.username, service_name=service_desc)

    if user.id not in USER_ORDERS:
        USER_ORDERS[user.id] = []

    order_idx = len(USER_ORDERS[user.id])
    USER_ORDERS[user.id].append({
        "service_name": service_desc,
        "total_price": total_price,
        "status": "⏳ Ожидает проверки чека",
    })
    queue_db((
        "order_add",
        user.id,
        service_desc,
        total_price,
        "⏳ Ожидает проверки чека",
    ))
    USER_BILLS.pop(user.id, None)
    queue_db(("bill_delete", user.id))

    order_details = _build_order_details_text(
        data, country, service_desc, total_price
    )
    user_tag = f"@{user.username}" if user.username else "без @username"
    receipt_caption = (
        f"🛒 Новый чек от {user_tag} (ID:"
        f" <code>{user.id}</code>){order_details}"
    )
    target_photos = data.get("photos") or []
    order_kb = _build_paid_order_keyboard(user.id, order_index=order_idx, in_work=False)

    for staff_id in get_staff_recipients():
        try:
            await message.bot.send_photo(
                chat_id=staff_id,
                photo=message.photo[-1].file_id,
                caption=receipt_caption,
                reply_markup=order_kb,
                parse_mode="HTML",
            )
            for p_id in target_photos:
                await message.bot.send_photo(
                    chat_id=staff_id,
                    photo=p_id,
                    caption=(
                        f"📸 Фото цели по чеку от {user_tag} (ID:"
                        f" <code>{user.id}</code>)"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    text = (
        "✅ Чек получен! Менеджер проверит платеж в течение 25 минут."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(message, state, "main.jpg", text, keyboard)
    await state.clear()


@router.message(OrderForm.receipt, F.document)
async def process_receipt_document(message: Message, state: FSMContext):
    data = await state.get_data()
    payment_method = data.get("payment_method", "card")
    if payment_method == "crypto":
        await message.reply(
            "⚠️ Для оплаты криптовалютой отправьте фото скриншот оплаты перевода."
        )
        return

    user = message.from_user
    _reset_payment_step_tracking(user.id, leave_step=True)
    _cancel_abandoned_reminder(user.id)
    _clear_back_after_service_flags(user.id)
    PROBLEM_USERS.pop(user.id, None)
    data, country, _, _, total_price, service_desc = await get_payment_context(
        user.id, state
    )
    _track_user_step(user.id, "paid", user.username, service_name=service_desc)

    if user.id not in USER_ORDERS:
        USER_ORDERS[user.id] = []

    order_idx = len(USER_ORDERS[user.id])
    USER_ORDERS[user.id].append({
        "service_name": service_desc,
        "total_price": total_price,
        "status": "⏳ Ожидает проверки чека",
    })
    queue_db((
        "order_add",
        user.id,
        service_desc,
        total_price,
        "⏳ Ожидает проверки чека",
    ))
    USER_BILLS.pop(user.id, None)
    queue_db(("bill_delete", user.id))

    order_details = _build_order_details_text(
        data, country, service_desc, total_price
    )
    user_tag = f"@{user.username}" if user.username else "без @username"
    receipt_caption = (
        f"🛒 Новая квитанция (документ) от {user_tag} (ID:"
        f" <code>{user.id}</code>){order_details}"
    )
    target_photos = data.get("photos") or []
    order_kb = _build_paid_order_keyboard(user.id, order_index=order_idx, in_work=False)

    for staff_id in get_staff_recipients():
        try:
            await message.bot.send_document(
                chat_id=staff_id,
                document=message.document.file_id,
                caption=receipt_caption,
                reply_markup=order_kb,
                parse_mode="HTML",
            )
            for p_id in target_photos:
                await message.bot.send_photo(
                    chat_id=staff_id,
                    photo=p_id,
                    caption=(
                        f"📸 Фото цели по чеку от {user_tag} (ID:"
                        f" <code>{user.id}</code>)"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    text = (
        "✅ Чек получен! Менеджер проверит платеж в течение 25 минут."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(message, state, "main.jpg", text, keyboard)
    await state.clear()


# === ЗАЩИТНЫЕ (FALLBACK) ХЕНДЛЕРЫ ОТ ЗАВИСАНИЙ И НЕВЕРНОГО ВВОДА ===
@router.message(OrderForm.debt_info)
async def fallback_order_debt_info(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте текстовое сообщение с информацией о долге и"
        " сумме."
    )


@router.message(OrderForm.debt_wallet)
async def fallback_order_debt_wallet(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте текстовое сообщение с вашим криптокошельком"
        " USDT или TON."
    )


@router.message(OrderForm.special_task)
async def fallback_order_special_task(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, опишите вашу задачу текстовым сообщением."
    )


@router.message(OrderForm.target_location)
async def fallback_order_location(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте текстовое сообщение с локацией цели (город,"
        " адрес, дом или квартиру)."
    )


@router.message(OrderForm.target_data)
async def fallback_order_data(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте текстовое сообщение с данными цели (ФИО или"
        " «неизвестно»)."
    )


@router.message(OrderForm.photos)
async def fallback_order_photos(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте фото цели или нажмите кнопку «Готово» /"
        " «Пропустить»."
    )


@router.message(OrderForm.promo_code)
async def fallback_order_promo_code(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    cmd_head = raw.split()[0].lower().split("@")[0] if raw else ""
    if cmd_head in ("/start", "/restart"):
        await _full_reset_user_session(message.from_user.id, state, message.from_user.username)
        if cmd_head == "/restart":
            try:
                await message.reply("🔄 Бот перезапущен, состояние очищено.")
            except Exception:
                pass
        await show_main_menu(message, state)
        return
    await _send_promo_return_notice(
        message.bot,
        message.from_user.id,
        "❌ Неправильный промокод, АРЕНУ не обманешь)\n\n"
        "Введите промокод <b>одним словом</b> или нажмите кнопку возврата:",
    )


@router.message(OrderForm.receipt)
async def fallback_order_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("payment_method") == "crypto":
        await message.reply(
            "⚠️ Пожалуйста, отправьте фото скриншот оплаты перевода."
        )
    else:
        await message.reply(
            "⚠️ Пожалуйста, отправьте фото чека или документ квитанции об оплате."
        )


@router.message(SupportForm.reason)
async def fallback_support_reason(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, опишите вашу ситуацию текстовым сообщением."
    )


@router.message(SupportForm.photo)
async def fallback_support_photo(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте фото или нажмите кнопку «Пропустить»."
    )


# === WEB SERVER FOR RENDER / RAILWAY ===
async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    global BOT_INSTANCE
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    BOT_INSTANCE = bot
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="restart", description="🔄 Перезапуск / Сброс бота"),
            BotCommand(command="order", description="Заказать услугу"),
            BotCommand(command="my_orders", description="Мои заказы"),
            BotCommand(command="reputation", description="Репутация"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось установить команды: {e}")

    await restore_abandoned_reminders(bot)
    asyncio.create_task(start_web_server())
    asyncio.create_task(scheduled_broadcast(bot))
    await bot.delete_webhook(drop_pending_updates=True)

    while True:
        try:
            logger.info("Запуск поллинга бота...")
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"Ошибка поллинга: {e}. Переподключение через 5 секунд...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    FSInputFile,
    InputMediaPhoto,
)
from aiohttp import ClientSession, web
import asyncpg
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

admin_env = (os.getenv("ADMIN_ID") or "").strip()
ADMIN_ID = int(admin_env) if admin_env.isdigit() else None

manager_env = (os.getenv("MANAGER_ID") or "").strip()
MANAGER_ID = int(manager_env) if manager_env.isdigit() else None


def is_admin(user_id: int | None) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


def is_manager(user_id: int | None) -> bool:
    return MANAGER_ID is not None and user_id == MANAGER_ID


def is_staff(user_id: int | None) -> bool:
    if user_id is None:
        return False
    allowed = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}
    return bool(allowed) and user_id in allowed


def get_staff_recipients() -> set[int]:
    return {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}


class OrderForm(StatesGroup):
    service = State()
    debt_info = State()
    debt_wallet = State()
    special_task = State()
    country = State()
    target_location = State()
    target_data = State()
    photos = State()
    urgency = State()
    summary = State()
    promo_code = State()
    payment = State()
    receipt = State()


class SupportForm(StatesGroup):
    reason = State()
    photo = State()


class ClientReplyForm(StatesGroup):
    waiting_message = State()


class StaffDirectForm(StatesGroup):
    waiting_reply = State()
    waiting_bill = State()
    waiting_reply_and_bill = State()


# Базы данных в памяти (синхронизируются с PostgreSQL при наличии DATABASE_URL)
USER_ORDERS = {}
USER_LAST_COUNTRY = {}
USER_BILLS = {}
KNOWN_USERS = {}          # user_id -> last_seen
USER_LAST_BROADCAST = {}  # user_id -> last_broadcast
ABANDONED_CHECKOUTS = {}  # user_id -> asyncio.Task
ABANDONED_DEADLINES = {}  # user_id -> datetime.datetime (remind_at)
CARD_DETAILS = []
CARD_POINTERS = {}
CRYPTO_DETAILS = {}
CRYPTO_POINTERS = {}
BANNED_USERS = set()
BANNER_CACHE = {}  # Кэш file_id баннеров для мгновенной загрузки

# Промокоды: одноразовое применение на первый заказ, ручное одобрение стафом
USED_PROMO_USERS = set()         # user_id, которым уже подтверждён и применён промокод
USER_APPLIED_PROMOS = {}       # user_id -> {"code": str, "discount_pct": int, "original_price": float, "discounted_price": float}
PROMO_REQUESTS = {}            # req_id -> dict
PROMO_REQUEST_COUNTER = 0
PROMO_DECIDED_REQUESTS = set()   # req_id уже обработанных заявок (защита от повторного нажатия)

# Трекинг активности клиентов, обращений и диалогов
USER_PROFILES = {}        # user_id -> {"username": str|None, "first_seen": datetime|None, "last_seen": datetime|None, "last_service": str|None, "max_step": str}
USER_STEP_RANK = {}       # user_id -> int
ACTIVE_DIALOGS = {}       # user_id -> {"closed": bool, "task": asyncio.Task|None, "expires_at": datetime|None, "last_staff_id": int|None}
TICKET_COUNTER = 0
TICKETS = {}              # ticket_id -> {"user_id": int, "kind": str, "status": str, "staff_msgs": dict[int, int], "extra_data": dict}
PROBLEM_USERS = {}        # user_id -> {"kind": str, "step": str, "query": str, "data": dict, "updated_at": datetime}
ABANDONED_STAFF_NOTIFIED = set()  # user_id для которых уже отправлено уведомление о неоплаченном заказе
BACK_AFTER_SERVICE_NOTIFIED = set()  # (user_id, service_name) — чтобы не дублировать при повторных «Назад»

# Строгий контроль уведомления о неоплаченном заказе на шаге выбора способа оплаты
ON_PAYMENT_METHOD_STEP = set()    # user_id сейчас на этапе выбора способа оплаты
PAYMENT_STEP_NOTIFIED = set()     # user_id уже получил уведомление в текущем заходе на шаг способа оплаты
PAYMENT_STEP_TASKS = {}           # user_id -> asyncio.Task (15 минут бездействия)

DB_POOL = None
DB_WRITE_QUEUE = None
BOT_INSTANCE: Bot | None = None

router = Router()

STEP_LABELS = {
    "catalog": "1. Каталог услуг",
    "debt_info": "2. Информация о долге",
    "debt_wallet": "2. Кошелек для возврата долга",
    "special_task": "2. Задача особого заказа",
    "country": "3. Выбор страны",
    "target_location": "4. Локация цели",
    "target_data": "5. Данные цели",
    "photos": "6. Фото цели",
    "urgency": "7. Срочность",
    "summary": "8. Сводка заказа",
    "payment": "9. Переход к оплате",
    "payment_method": "10. Выбор способа оплаты / реквизитов",
    "promo": "10. Запросил промокод",
    "receipt": "11. Отправка чека",
    "paid": "12. Оплачено / заявка отправлена",
}

STEP_RANKS = {
    "catalog": 1,
    "debt_info": 2,
    "debt_wallet": 2,
    "special_task": 2,
    "country": 3,
    "target_location": 4,
    "target_data": 5,
    "photos": 6,
    "urgency": 7,
    "summary": 8,
    "payment": 9,
    "payment_method": 10,
    "promo": 10,
    "receipt": 11,
    "paid": 12,
}


async def _push_nav_step(state: FSMContext, step_name: str):
    data = await state.get_data()
    history = list(data.get("nav_history") or [])
    if not history or history[-1] != step_name:
        history.append(step_name)
    await state.update_data(nav_history=history)


async def _pop_nav_step(state: FSMContext, current_step_name: str | None = None) -> str | None:
    data = await state.get_data()
    history = list(data.get("nav_history") or [])
    while history and history[-1] == current_step_name:
        history.pop()
    prev_step = history.pop() if history else None
    await state.update_data(nav_history=history)
    return prev_step


def _format_num(val: float | int) -> str:
    num = float(val)
    if num.is_integer():
        return str(int(num))
    return f"{round(num, 2):.2f}".rstrip("0").rstrip(".")


def _apply_discount_once(base_price: float | int, discount_pct: int) -> float | int:
    base = float(base_price or 0)
    if base <= 0 or discount_pct <= 0:
        return int(base) if base.is_integer() else round(base, 2)
    discounted = base * (100 - discount_pct) / 100.0
    if discounted.is_integer():
        return int(discounted)
    return round(discounted, 2)


def _is_paid_client_order(order_data: dict) -> bool:
    status_low = (order_data.get("status") or "").strip().lower()
    if not status_low:
        return False
    if any(unpaid_mark in status_low for unpaid_mark in ("счет", "счёт", "расчет", "расчёт", "не оплач", "брошен")):
        return False
    paid_marks = (
        "оплачено",
        "оплачен",
        "проверки чека",
        "в работе",
        "выполнен",
        "оформлен",
    )
    return any(mark in status_low for mark in paid_marks)


def _get_client_paid_orders(user_id: int) -> list[dict]:
    raw_orders = USER_ORDERS.get(user_id) or []
    return [o for o in raw_orders if _is_paid_client_order(o)]


def _track_user_step(
    user_id: int,
    step_key: str,
    username: str | None = None,
    service_name: str | None = None,
):
    if user_id in BANNED_USERS:
        return
    now_dt = datetime.datetime.now()
    KNOWN_USERS[user_id] = now_dt
    queue_db(("known_user", user_id, now_dt, username))

    prof = USER_PROFILES.setdefault(
        user_id,
        {
            "username": username,
            "first_seen": now_dt,
            "last_seen": now_dt,
            "last_service": service_name,
            "max_step": STEP_LABELS.get(step_key, step_key),
        },
    )
    if username:
        prof["username"] = username
    if not prof.get("first_seen"):
        prof["first_seen"] = now_dt
    prof["last_seen"] = now_dt
    if service_name:
        prof["last_service"] = service_name

    new_rank = STEP_RANKS.get(step_key, 0)
    old_rank = USER_STEP_RANK.get(user_id, 0)
    if new_rank >= old_rank:
        USER_STEP_RANK[user_id] = new_rank
        prof["max_step"] = STEP_LABELS.get(step_key, step_key)


def _record_problem_user(
    user_id: int,
    kind: str,
    step_label: str,
    query_text: str = "",
    data: dict | None = None,
):
    if user_id in BANNED_USERS:
        return
    clean_data = {}
    if data:
        for k, v in data.items():
            if k in ("bot_msg_id", "invoice_id", "photos", "nav_history") or not v:
                continue
            clean_data[k] = v
    PROBLEM_USERS[user_id] = {
        "kind": kind,
        "step": step_label,
        "query": query_text,
        "data": clean_data,
        "updated_at": datetime.datetime.now(),
    }


def _get_latest_order_status(user_id: int) -> str:
    orders = USER_ORDERS.get(user_id) or []
    if orders:
        return orders[-1].get("status") or "— Нет заказов"
    if user_id in USER_BILLS:
        return "🧾 Выставлен счет (ожидает оплаты)"
    return "— Нет заказов"


def _format_user_summary_block(user_id: int, data: dict | None = None) -> str:
    prof = USER_PROFILES.get(user_id, {})
    uname = prof.get("username")
    user_tag = f"@{uname}" if uname else "без @username"
    first_seen = prof.get("first_seen")
    last_seen = prof.get("last_seen") or KNOWN_USERS.get(user_id)
    first_str = (
        first_seen.strftime("%d.%m.%Y %H:%M")
        if isinstance(first_seen, datetime.datetime)
        else "неизвестно"
    )
    last_str = (
        last_seen.strftime("%d.%m.%Y %H:%M")
        if isinstance(last_seen, datetime.datetime)
        else "сейчас"
    )
    bill = USER_BILLS.get(user_id) or {}
    last_order = USER_ORDERS.get(user_id)[-1] if USER_ORDERS.get(user_id) else {}
    service_val = (
        (data or {}).get("service_name")
        or prof.get("last_service")
        or bill.get("service_name")
        or last_order.get("service_name")
        or "не выбрана"
    )
    max_step = prof.get("max_step") or STEP_LABELS.get("catalog")
    order_status = _get_latest_order_status(user_id)
    orders_count = len(USER_ORDERS.get(user_id, []))

    price_val = (data or {}).get("total_price")
    if price_val in (None, 0):
        price_val = (data or {}).get("service_price") or bill.get("total_price") or last_order.get("total_price")

    lines = [
        "📋 <b>Сводка по клиенту и заказу:</b>",
        f"• Клиент: {user_tag} | ID: <code>{user_id}</code>",
        f"• Услуга: <b>{service_val}</b>",
        f"• Этап: <b>{max_step}</b> | Статус заказа: <b>{order_status}</b>",
        f"• Первый вход: <b>{first_str}</b> | Активность: <b>{last_str}</b>",
        f"• Заказов в истории: <b>{orders_count}</b>",
    ]
    if data:
        if data.get("country"):
            lines.append(f"• Страна: <b>{data.get('country')}</b>")
        if data.get("target_location"):
            lines.append(f"• Локация: {data.get('target_location')}")
        if data.get("target_data"):
            lines.append(f"• Данные цели: {data.get('target_data')}")
        if data.get("debt_info"):
            lines.append(f"• Детали возврата долга: {data.get('debt_info')}")
        if data.get("debt_wallet"):
            lines.append(f"• Кошелек возврата: {data.get('debt_wallet')}")
        if data.get("special_task"):
            lines.append(f"• Детали особого заказа: {data.get('special_task')}")
        if data.get("urgency"):
            lines.append(f"• Срочность: <b>{data.get('urgency')}</b>")
    if price_val and float(price_val) > 0:
        lines.append(f"• Сумма: <b>{_format_num(price_val)}$</b>")
    return "\n".join(lines)


# === ЗАПРЕТ РЕКВИЗИТОВ И ССЫЛОК ДЛЯ МЕНЕДЖЕРА ===
_URL_PATTERN = re.compile(
    r"(?:https?://|www\.|t\.me/|telegram\.me/|telegra\.ph/|[a-zA-Z0-9-]+\.(?:ru|com|net|org|io|me|to|cc|gg|pro|info|biz|xyz|ua|kz|by)(?:/[^\s]*)?)",
    re.IGNORECASE,
)
_CRYPTO_PATTERNS = [
    re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b"),
    re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    re.compile(r"\b(?:bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}\b"),
    re.compile(r"\b(?:UQ|EQ|kQ|0:)[a-zA-Z0-9_-]{46,48}\b"),
    re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"),
]
_CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_PHONE_PATTERN = re.compile(
    r"(?:(?:\+|00)?\d{1,3}[\s\-\.]?)?(?:\(?\d{2,4}\)?[\s\-\.]?)?\d{2,4}[\s\-\.]?\d{2,4}(?:[\s\-\.]?\d{2,4})?"
)


def _normalize_for_requisite_check(text: str) -> str:
    collapsed = re.sub(r"[\s\-\._,:/\\|()\[\]]+", "", text)
    return f"{text}\n{collapsed}"


def find_forbidden_requisites_for_manager(text: str) -> str | None:
    if not text:
        return None
    if _URL_PATTERN.search(text):
        return "ссылка"

    combined_text = _normalize_for_requisite_check(text)

    for pat in _CRYPTO_PATTERNS:
        m = pat.search(combined_text)
        if m:
            return f"адрес криптокошелька ({m.group(0)})"

    for m in _CARD_PATTERN.finditer(combined_text):
        digits_only = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits_only) <= 19:
            return f"номер карты ({m.group(0).strip()})"

    for m in _PHONE_PATTERN.finditer(text):
        raw_match = m.group(0).strip()
        digits_only = re.sub(r"\D", "", raw_match)
        has_phone_prefix = raw_match.startswith("+") or bool(re.search(r"[\(\)\-\s]", raw_match))
        if len(digits_only) >= 10 and (has_phone_prefix or len(digits_only) in (10, 11, 12)):
            return f"номер телефона ({raw_match})"

    collapsed_digits = re.sub(r"\D", "", re.sub(r"[\s\-\._,:/\\|()\[\]]+", "", text))
    if 13 <= len(collapsed_digits) <= 19:
        return f"номер карты ({collapsed_digits})"
    if len(collapsed_digits) >= 10 and ("+" in text or "тел" in text.lower() or "phone" in text.lower()):
        return f"номер телефона ({collapsed_digits})"

    return None


def _resolve_target_user_id(raw_target: str) -> int | None:
    cleaned = raw_target.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)
    uname = cleaned.lstrip("@").strip().lower()
    if not uname:
        return None
    for uid, prof in USER_PROFILES.items():
        prof_uname = (prof.get("username") or "").strip().lower()
        if prof_uname and prof_uname == uname:
            return int(uid)
    return None


def _cancel_payment_step_timer(user_id: int):
    task = PAYMENT_STEP_TASKS.pop(user_id, None)
    if task:
        task.cancel()


def _reset_payment_step_tracking(user_id: int, leave_step: bool = True):
    _cancel_payment_step_timer(user_id)
    if leave_step:
        ON_PAYMENT_METHOD_STEP.discard(user_id)
        PAYMENT_STEP_NOTIFIED.discard(user_id)


async def _send_unpaid_payment_step_notice(bot: Bot, user_id: int, reason_text: str):
    if user_id in BANNED_USERS:
        return
    if user_id in PAYMENT_STEP_NOTIFIED:
        return
    PAYMENT_STEP_NOTIFIED.add(user_id)
    ON_PAYMENT_METHOD_STEP.discard(user_id)
    _cancel_payment_step_timer(user_id)

    prof = USER_PROFILES.get(user_id, {})
    user_tag = f"@{prof.get('username')}" if prof.get("username") else "без @username"
    bill = USER_BILLS.get(user_id) or {}
    extra = {}
    if prof.get("last_service"):
        extra["service_name"] = prof["last_service"]
    if bill.get("service_name"):
        extra["service_name"] = bill["service_name"]
    if bill.get("total_price"):
        extra["total_price"] = bill["total_price"]
    if bill.get("country"):
        extra["country"] = bill["country"]

    _record_problem_user(
        user_id=user_id,
        kind="unpaid_checkout",
        step_label=STEP_LABELS["payment"],
        query_text=reason_text,
        data=extra,
    )

    body = (
        f"• Клиент: {user_tag} (ID: <code>{user_id}</code>)\n"
        f"• Этап: <b>{STEP_LABELS['payment']}</b>\n"
        f"• Причина: <b>{reason_text}</b>"
    )
    await _send_staff_ticket_menu(
        bot=bot,
        user_id=user_id,
        kind="unpaid_checkout",
        header_title="⚠️ <b>Неоплаченный заказ — требуется ведение клиента</b>",
        body_text=body,
        extra_data=extra,
    )


async def _payment_step_inactivity_worker(bot: Bot, user_id: int):
    try:
        await asyncio.sleep(15 * 60)
        PAYMENT_STEP_TASKS.pop(user_id, None)
        if user_id in BANNED_USERS:
            return
        if user_id not in ON_PAYMENT_METHOD_STEP or user_id in PAYMENT_STEP_NOTIFIED:
            return
        await _send_unpaid_payment_step_notice(
            bot=bot,
            user_id=user_id,
            reason_text="15 минут бездействия на этапе выбора способа оплаты",
        )
    except asyncio.CancelledError:
        pass


def _enter_payment_method_step(bot: Bot, user_id: int):
    if user_id in BANNED_USERS:
        return
    _cancel_payment_step_timer(user_id)
    ON_PAYMENT_METHOD_STEP.add(user_id)
    PAYMENT_STEP_NOTIFIED.discard(user_id)
    PAYMENT_STEP_TASKS[user_id] = asyncio.create_task(
        _payment_step_inactivity_worker(bot, user_id)
    )


def _schedule_abandoned_reminder(bot: Bot, user_id: int, remind_at: datetime.datetime):
    if user_id in BANNED_USERS:
        return
    old_task = ABANDONED_CHECKOUTS.pop(user_id, None)
    if old_task:
        old_task.cancel()
    ABANDONED_DEADLINES[user_id] = remind_at
    ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(
        delayed_abandoned_cart_reminder(bot, user_id, remind_at)
    )


def _cancel_abandoned_reminder(user_id: int):
    task = ABANDONED_CHECKOUTS.pop(user_id, None)
    if task:
        task.cancel()
    ABANDONED_DEADLINES.pop(user_id, None)
    ABANDONED_STAFF_NOTIFIED.discard(user_id)
    queue_db(("abandoned_delete", user_id))


def _clear_back_after_service_flags(user_id: int):
    for key in [k for k in BACK_AFTER_SERVICE_NOTIFIED if k[0] == user_id]:
        BACK_AFTER_SERVICE_NOTIFIED.discard(key)


def _has_active_unpaid_item(user_id: int) -> bool:
    if user_id in BANNED_USERS:
        return False
    if user_id in USER_BILLS:
        return True
    if user_id in ABANDONED_CHECKOUTS or user_id in ABANDONED_DEADLINES:
        return True
    orders = USER_ORDERS.get(user_id) or []
    if orders:
        last_status = (orders[-1].get("status") or "").lower()
        if any(marker in last_status for marker in ("счет", "счёт", "ожидает", "не оплач", "брошен")):
            return True
    return False


# === АВТОЗАКРЫТИЕ ОБРАЩЕНИЯ У КЛИЕНТА ЧЕРЕЗ 1 ЧАС ===
def _cancel_dialog_timer(user_id: int):
    dlg = ACTIVE_DIALOGS.get(user_id)
    if dlg and dlg.get("task"):
        dlg["task"].cancel()
        dlg["task"] = None


def _close_dialog_state(user_id: int):
    _cancel_dialog_timer(user_id)
    ACTIVE_DIALOGS[user_id] = {
        "closed": True,
        "task": None,
        "expires_at": None,
        "last_staff_id": None,
    }


async def _auto_close_dialog_after_hour(bot: Bot, user_id: int, expires_at: datetime.datetime):
    try:
        delay = max(0.0, (expires_at - datetime.datetime.now()).total_seconds())
        if delay > 0:
            await asyncio.sleep(delay)

        if user_id in BANNED_USERS:
            _close_dialog_state(user_id)
            return

        dlg = ACTIVE_DIALOGS.get(user_id)
        if not dlg or dlg.get("closed"):
            return
        if dlg.get("expires_at") and dlg["expires_at"] > datetime.datetime.now():
            return

        dlg["closed"] = True
        dlg["task"] = None

        try:
            await bot.send_message(
                chat_id=user_id,
                text="⏳ <b>Обращение автоматически закрыто по истечении 1 часа.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    except asyncio.CancelledError:
        pass


def _arm_dialog_timer(bot: Bot, user_id: int, staff_id: int | None = None):
    if user_id in BANNED_USERS:
        return
    _cancel_dialog_timer(user_id)
    expires_at = datetime.datetime.now() + datetime.timedelta(hours=1)
    task = asyncio.create_task(_auto_close_dialog_after_hour(bot, user_id, expires_at))
    ACTIVE_DIALOGS[user_id] = {
        "closed": False,
        "task": task,
        "expires_at": expires_at,
        "last_staff_id": staff_id,
    }


def _build_client_send_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️💬 Ответить менеджеру",
                    callback_data="client_reply_manager",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🔒🏠 Закрыть обращение",
                    callback_data="client_close_dialog",
                    style="danger",
                ),
            ]
        ]
    )


async def _deliver_send_message_to_client(
    bot: Bot,
    target_user_id: int,
    text_to_send: str,
    prefix: str = "💬 <b>Сообщение от менеджера:</b>",
    staff_id: int | None = None,
) -> tuple[bool, str]:
    if target_user_id in BANNED_USERS:
        return False, "Пользователь заблокирован"
    payload = f"{prefix}\n\n{text_to_send}"
    keyboard = _build_client_send_keyboard()
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=payload,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        _arm_dialog_timer(bot, target_user_id, staff_id=staff_id)
        return True, ""
    except Exception as first_err:
        logger.warning(f"Повторная попытка отправки клиенту {target_user_id} после ошибки: {first_err}")
        await asyncio.sleep(0.4)
        try:
            await bot.send_message(
                chat_id=target_user_id,
                text=payload,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            _arm_dialog_timer(bot, target_user_id, staff_id=staff_id)
            return True, ""
        except Exception as second_err:
            return False, str(second_err)


# === ВЫСТАВЛЕНИЕ СЧЕТА КЛИЕНТУ (ИСПОЛЬЗУЕТСЯ КОМАНДОЙ /bill И КНОПКАМИ СТАФА) ===
async def _issue_bill_to_client(
    bot: Bot,
    caller_id: int,
    caller_state: FSMContext,
    target_user_id: int,
    service_name: str,
    amount_usd: float,
) -> tuple[bool, str]:
    if target_user_id in BANNED_USERS:
        return False, "Пользователь заблокирован"

    amount_val = (
        int(amount_usd) if amount_usd.is_integer() else round(amount_usd, 2)
    )
    applied_promo = USER_APPLIED_PROMOS.get(target_user_id)
    effective_price = amount_val
    original_price = amount_val
    discount_pct = 0
    if applied_promo and not _get_client_paid_orders(target_user_id):
        discount_pct = int(applied_promo.get("discount_pct", 0) or 0)
        if discount_pct > 0:
            effective_price = _apply_discount_once(amount_val, discount_pct)
            applied_promo["original_price"] = original_price
            applied_promo["discounted_price"] = effective_price

    amount_str = _format_num(effective_price)
    country = USER_LAST_COUNTRY.get(target_user_id, "Россия")
    curr_info = COUNTRY_CURRENCIES.get(
        country, {"code": "USD", "symbol": "USD"}
    )
    rate = await get_exchange_rate(curr_info["code"])
    local_amount = round(float(effective_price) * rate)

    USER_BILLS[target_user_id] = {
        "service_name": service_name,
        "total_price": effective_price,
        "original_total_price": original_price,
        "promo_discount_pct": discount_pct,
        "country": country,
        "curr_code": curr_info["code"],
        "curr_symbol": curr_info["symbol"],
    }
    if caller_id == target_user_id:
        await caller_state.update_data(
            service_name=service_name,
            total_price=effective_price,
            original_total_price=original_price,
            promo_discount_pct=discount_pct,
            country=country,
            curr_code=curr_info["code"],
            curr_symbol=curr_info["symbol"],
        )

    if discount_pct > 0:
        sum_line = (
            f"Сумма: <s>{_format_num(original_price)}$</s> → <b>{amount_str}$</b> "
            f"<b>(-{discount_pct}%)</b> <i>(≈ {local_amount:,} {curr_info['symbol']})</i>"
        )
    else:
        sum_line = (
            f"Сумма: <b>{amount_str}$</b> <i>(≈ {local_amount:,}"
            f" {curr_info['symbol']})</i>"
        )

    bill_text = (
        "⏳ <b>Оплата заказа</b>\n"
        "Время на завершение: <b><u>20:00</u></b>\n\n"
        f"🧾 <b>Счет на {service_name}</b>\n"
        f"💳 <b>Способ оплаты ({country})</b>\n"
        f"{sum_line}"
    )

    remind_at = datetime.datetime.now() + datetime.timedelta(minutes=30)
    queue_db(("abandoned_set", target_user_id, remind_at))
    _schedule_abandoned_reminder(bot, target_user_id, remind_at)

    sent_bill = None
    last_err = ""
    for _ in range(2):
        try:
            banner_name = "payment.jpg"
            banner_path = get_banner_path(banner_name)
            if banner_name in BANNER_CACHE:
                sent_bill = await bot.send_photo(
                    chat_id=target_user_id,
                    photo=BANNER_CACHE[banner_name],
                    caption=bill_text,
                    reply_markup=build_payment_keyboard(country),
                    parse_mode="HTML",
                )
            elif banner_path and os.path.exists(banner_path):
                sent_bill = await bot.send_photo(
                    chat_id=target_user_id,
                    photo=FSInputFile(banner_path),
                    caption=bill_text,
                    reply_markup=build_payment_keyboard(country),
                    parse_mode="HTML",
                )
                if sent_bill.photo:
                    BANNER_CACHE[banner_name] = sent_bill.photo[-1].file_id
            else:
                sent_bill = await bot.send_message(
                    chat_id=target_user_id,
                    text=bill_text,
                    reply_markup=build_payment_keyboard(country),
                    parse_mode="HTML",
                )
            last_err = ""
            break
        except Exception as e:
            last_err = str(e)
            await asyncio.sleep(0.4)

    if sent_bill is not None:
        USER_BILLS[target_user_id]["bill_msg_id"] = sent_bill.message_id
        queue_db(("bill_set", target_user_id, dict(USER_BILLS[target_user_id])))
        if caller_id == target_user_id:
            await caller_state.update_data(bot_msg_id=sent_bill.message_id)
        _arm_dialog_timer(bot, target_user_id, staff_id=caller_id)
        return True, (
            f"✅ Счет на <b>{service_name}</b> (<b>{amount_str}$</b>) отправлен"
            f" пользователю <code>{target_user_id}</code> (страна:"
            f" <b>{country}</b>, валюта: <b>{curr_info['symbol']}</b>)."
        )
    return False, f"❌ Не удалось отправить счет пользователю <code>{target_user_id}</code>: {last_err}"


def _parse_bill_input(raw_text: str, default_service: str = "Особый заказ") -> tuple[str, float] | tuple[None, str]:
    parts = raw_text.strip().split()
    if not parts:
        return None, "❌ Укажите услугу и сумму. Пример: <code>особый заказ 220$</code>"

    raw_amount = (
        parts[-1]
        .replace("$", "")
        .replace("usd", "")
        .replace("USD", "")
        .replace(",", ".")
        .strip()
    )
    try:
        amount_usd = float(raw_amount)
        if amount_usd <= 0:
            raise ValueError()
    except ValueError:
        return None, "❌ Некорректная сумма. Пример: <code>особый заказ 220$</code>"

    service_name = " ".join(parts[:-1]).strip() if len(parts) > 1 else default_service
    if not service_name:
        service_name = default_service
    return service_name, amount_usd


def _build_staff_action_guide(mode: str, target_user_id: int) -> str:
    if mode == "reply":
        return (
            f"✍️ <b>Режим ответа клиенту (ID: <code>{target_user_id}</code>)</b>\n\n"
            "<b>Как заполнить:</b> отправьте следующим сообщением текст ответа клиенту.\n"
            "• Можно задать уточнения, согласовать детали или направить клиента.\n"
            "• Для отмены нажмите кнопку <b>«Отмена»</b> ниже.\n\n"
            "<i>Шпаргалка ручных команд:</i>\n"
            f"• Сообщение: <code>/send {target_user_id} текст</code>\n"
            f"• Счет: <code>/bill {target_user_id} услуга 220$</code>"
        )
    if mode == "bill":
        return (
            f"🧾 <b>Выставление счета клиенту (ID: <code>{target_user_id}</code>)</b>\n\n"
            "<b>Как заполнить:</b> отправьте одним сообщением <code>&lt;услуга&gt; &lt;сумма$&gt;</code>\n"
            "• Пример: <code>особый заказ 220$</code> или <code>Избить 160$</code>\n"
            "• Валюта страны клиента и примерный курс подставятся автоматически.\n"
            "• Для отмены нажмите кнопку <b>«Отмена»</b> ниже.\n\n"
            "<i>Шпаргалка ручных команд:</i>\n"
            f"• Счет: <code>/bill {target_user_id} особый заказ 220$</code>\n"
            f"• Сообщение: <code>/send {target_user_id} текст</code>"
        )
    return (
        f"💬🧾 <b>Ответ и выставление счета (ID: <code>{target_user_id}</code>)</b>\n\n"
        "<b>Как заполнить:</b> отправьте сообщение в формате:\n"
        "<code>Текст сообщения клиенту | услуга сумма$</code>\n"
        "• Пример: <code>Расчет готов, можно оплачивать. | особый заказ 220$</code>\n"
        "• Клиент получит ваш текст и сразу следом — выставленный счет.\n"
        "• Для отмены нажмите кнопку <b>«Отмена»</b> ниже.\n\n"
        "<i>Шпаргалка ручных команд:</i>\n"
        f"• Сообщение: <code>/send {target_user_id} текст</code>\n"
        f"• Счет: <code>/bill {target_user_id} особый заказ 220$</code>"
    )


def _build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="staff_cancel_mode",
                style="danger",
            )
        ]]
    )


# === МЕНЮШКИ ОБРАЩЕНИЙ И ЗАКАЗОВ ДЛЯ МЕНЕДЖЕРА / АДМИНА ===
def _register_ticket(user_id: int, kind: str, extra_data: dict | None = None) -> int:
    global TICKET_COUNTER
    TICKET_COUNTER += 1
    tid = TICKET_COUNTER
    TICKETS[tid] = {
        "user_id": user_id,
        "kind": kind,
        "status": "🟢 Открыто",
        "staff_msgs": {},
        "extra_data": dict(extra_data or {}),
    }
    return tid


def _build_ticket_keyboard(ticket_id: int, user_id: int, kind: str, closed: bool = False) -> InlineKeyboardMarkup:
    if closed:
        return InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="🔒 Обращение закрыто",
                    callback_data=f"tkt_noop_{ticket_id}",
                )
            ]]
        )
    if kind == "payment_help":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Написать клиенту",
                        callback_data=f"tkt_reply_{ticket_id}",
                        style="primary",
                    ),
                    InlineKeyboardButton(
                        text="🔒 Закрыть обращение",
                        callback_data=f"tkt_close_{ticket_id}",
                        style="danger",
                    ),
                ]
            ]
        )
    if kind == "support":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Ответить клиенту",
                        callback_data=f"tkt_reply_{ticket_id}",
                        style="primary",
                    ),
                    InlineKeyboardButton(
                        text="🧾 Выставить счёт",
                        callback_data=f"tkt_bill_{ticket_id}",
                        style="primary",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔒 Закрыть обращение",
                        callback_data=f"tkt_close_{ticket_id}",
                        style="danger",
                    )
                ],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Ответить клиенту",
                    callback_data=f"tkt_reply_{ticket_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🧾 Выставить счёт",
                    callback_data=f"tkt_bill_{ticket_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬🧾 Ответить и выставить счёт",
                    callback_data=f"tkt_replybill_{ticket_id}",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ В работе",
                    callback_data=f"tkt_work_{ticket_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="✅ Выполнен",
                    callback_data=f"tkt_done_{ticket_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Закрыть обращение",
                    callback_data=f"tkt_close_{ticket_id}",
                    style="danger",
                )
            ],
        ]
    )


def _build_paid_order_keyboard(
    user_id: int,
    order_index: int | None = None,
    in_work: bool = False,
    is_done: bool = False,
) -> InlineKeyboardMarkup:
    idx_suffix = f"_{order_index}" if order_index is not None else ""
    work_text = "⚙️ В работе ✓" if in_work else "⚙️ В работе"
    done_text = "✅ Выполнен ✓" if is_done else "✅ Выполнен"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Ответить клиенту",
                    callback_data=f"order_msg_{user_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🧾 Выставить счёт",
                    callback_data=f"order_bill_{user_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬🧾 Ответить и выставить счёт",
                    callback_data=f"order_replybill_{user_id}",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text=work_text,
                    callback_data=f"order_work_{user_id}{idx_suffix}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text=done_text,
                    callback_data=f"order_done_{user_id}{idx_suffix}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Закрыть обращение",
                    callback_data=f"order_close_{user_id}",
                    style="danger",
                )
            ],
        ]
    )


async def _send_staff_ticket_menu(
    bot: Bot,
    user_id: int,
    kind: str,
    header_title: str,
    body_text: str,
    extra_data: dict | None = None,
    photo_id: str | None = None,
    document_id: str | None = None,
):
    if user_id in BANNED_USERS:
        return
    tid = _register_ticket(user_id, kind, extra_data=extra_data)
    summary = _format_user_summary_block(user_id, extra_data)
    full_text = (
        f"{header_title}\n"
        f"🗂 Обращение #{tid} | Статус: <b>🟢 Открыто</b>\n\n"
        f"{body_text}\n\n"
        f"{summary}"
    )
    kb = _build_ticket_keyboard(tid, user_id=user_id, kind=kind, closed=False)

    for staff_id in get_staff_recipients():
        try:
            if photo_id:
                sent = await bot.send_photo(
                    chat_id=staff_id,
                    photo=photo_id,
                    caption=full_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            elif document_id:
                sent = await bot.send_document(
                    chat_id=staff_id,
                    document=document_id,
                    caption=full_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            else:
                sent = await bot.send_message(
                    chat_id=staff_id,
                    text=full_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            TICKETS[tid]["staff_msgs"][staff_id] = sent.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки карточки обращения #{tid} сотруднику {staff_id}: {e}")


# === POSTGRESQL СЛОЙ ===
async def init_db():
    global DB_POOL, DB_WRITE_QUEUE
    if not DATABASE_URL:
        logger.warning(
            "⚠️ DATABASE_URL не задан — бот работает только в оперативной памяти."
        )
        return

    dsn = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    DB_POOL = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)

    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS card_details (
                id SERIAL PRIMARY KEY,
                details TEXT NOT NULL,
                bank TEXT NOT NULL,
                country TEXT NOT NULL,
                frozen_until TIMESTAMP NULL
            );
            CREATE TABLE IF NOT EXISTS crypto_details (
                id SERIAL PRIMARY KEY,
                network_key TEXT NOT NULL,
                address TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id BIGINT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS known_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT NULL,
                last_seen TIMESTAMP NULL,
                last_broadcast TIMESTAMP NULL
            );
            CREATE TABLE IF NOT EXISTS user_last_country (
                user_id BIGINT PRIMARY KEY,
                country TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                service_name TEXT NOT NULL,
                total_price INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_bills (
                user_id BIGINT PRIMARY KEY,
                service_name TEXT NOT NULL,
                total_price INTEGER NOT NULL,
                country TEXT NOT NULL,
                curr_code TEXT NOT NULL,
                curr_symbol TEXT NOT NULL,
                bill_msg_id BIGINT NULL
            );
            CREATE TABLE IF NOT EXISTS abandoned_checkouts (
                user_id BIGINT PRIMARY KEY,
                remind_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS used_promo_users (
                user_id BIGINT PRIMARY KEY,
                promo_code TEXT NOT NULL,
                discount_pct INTEGER NOT NULL
            );
        """)

        await conn.execute("""
            ALTER TABLE known_users
            ADD COLUMN IF NOT EXISTS last_broadcast TIMESTAMP NULL
        """)
        await conn.execute("""
            ALTER TABLE known_users
            ADD COLUMN IF NOT EXISTS username TEXT NULL
        """)

        pk_col = await conn.fetchval("""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'crypto_details'::regclass AND i.indisprimary
            LIMIT 1
        """)
        has_id_col = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'crypto_details' AND column_name = 'id'
            )
        """)
        if pk_col == "network_key" or not has_id_col:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS crypto_details_new (
                    id SERIAL PRIMARY KEY,
                    network_key TEXT NOT NULL,
                    address TEXT NOT NULL
                );
                INSERT INTO crypto_details_new (network_key, address)
                SELECT network_key, address FROM crypto_details;
                DROP TABLE crypto_details;
                ALTER TABLE crypto_details_new RENAME TO crypto_details;
            """)

        CARD_DETAILS.clear()
        for row in await conn.fetch(
            "SELECT details, bank, country, frozen_until FROM card_details ORDER BY id ASC"
        ):
            CARD_DETAILS.append({
                "details": row["details"],
                "bank": row["bank"],
                "country": row["country"],
                "frozen_until": row["frozen_until"],
            })

        CRYPTO_DETAILS.clear()
        for row in await conn.fetch(
            "SELECT network_key, address FROM crypto_details ORDER BY id ASC"
        ):
            CRYPTO_DETAILS.setdefault(row["network_key"], []).append(row["address"])

        BANNED_USERS.clear()
        for row in await conn.fetch("SELECT user_id FROM banned_users"):
            BANNED_USERS.add(int(row["user_id"]))

        USED_PROMO_USERS.clear()
        for row in await conn.fetch("SELECT user_id, promo_code, discount_pct FROM used_promo_users"):
            uid = int(row["user_id"])
            USED_PROMO_USERS.add(uid)
            USER_APPLIED_PROMOS.setdefault(
                uid,
                {
                    "code": row["promo_code"],
                    "discount_pct": int(row["discount_pct"]),
                    "original_price": 0,
                    "discounted_price": 0,
                },
            )

        KNOWN_USERS.clear()
        USER_LAST_BROADCAST.clear()
        for row in await conn.fetch(
            "SELECT user_id, username, last_seen, last_broadcast FROM known_users"
        ):
            uid = int(row["user_id"])
            uname = row["username"]
            KNOWN_USERS[uid] = row["last_seen"]
            USER_PROFILES.setdefault(
                uid,
                {
                    "username": uname,
                    "first_seen": row["last_seen"],
                    "last_seen": row["last_seen"],
                    "last_service": None,
                    "max_step": STEP_LABELS.get("catalog"),
                },
            )
            if uname:
                USER_PROFILES[uid]["username"] = uname
            if row["last_broadcast"] is not None:
                USER_LAST_BROADCAST[uid] = row["last_broadcast"]

        USER_LAST_COUNTRY.clear()
        for row in await conn.fetch(
            "SELECT user_id, country FROM user_last_country"
        ):
            USER_LAST_COUNTRY[int(row["user_id"])] = row["country"]

        USER_ORDERS.clear()
        for row in await conn.fetch(
            "SELECT user_id, service_name, total_price, status FROM user_orders ORDER BY id ASC"
        ):
            uid = int(row["user_id"])
            USER_ORDERS.setdefault(uid, []).append({
                "service_name": row["service_name"],
                "total_price": row["total_price"],
                "status": row["status"],
            })

        USER_BILLS.clear()
        for row in await conn.fetch("""
            SELECT user_id, service_name, total_price, country, curr_code, curr_symbol, bill_msg_id
            FROM user_bills
        """):
            uid = int(row["user_id"])
            USER_BILLS[uid] = {
                "service_name": row["service_name"],
                "total_price": row["total_price"],
                "country": row["country"],
                "curr_code": row["curr_code"],
                "curr_symbol": row["curr_symbol"],
                "bill_msg_id": row["bill_msg_id"],
            }

        ABANDONED_DEADLINES.clear()
        for row in await conn.fetch(
            "SELECT user_id, remind_at FROM abandoned_checkouts"
        ):
            ABANDONED_DEADLINES[int(row["user_id"])] = row["remind_at"]

    DB_WRITE_QUEUE = asyncio.Queue()
    asyncio.create_task(db_writer_worker())
    logger.info("✅ PostgreSQL подключен и синхронизирован.")


async def _exec_db_action(action: tuple):
    if not DB_POOL:
        return
    kind = action[0]
    try:
        async with DB_POOL.acquire() as conn:
            if kind == "ban_add":
                _, user_id = action
                await conn.execute(
                    "INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    user_id,
                )
            elif kind == "ban_remove":
                _, user_id = action
                await conn.execute(
                    "DELETE FROM banned_users WHERE user_id = $1",
                    user_id,
                )
            elif kind == "card_add":
                _, details, bank, country = action
                await conn.execute(
                    "INSERT INTO card_details (details, bank, country, frozen_until) VALUES ($1, $2, $3, NULL)",
                    details,
                    bank,
                    country,
                )
            elif kind == "card_delete":
                _, details, bank, country = action
                await conn.execute(
                    "DELETE FROM card_details WHERE details = $1 AND bank = $2 AND LOWER(country) = LOWER($3)",
                    details,
                    bank,
                    country,
                )
            elif kind == "card_freeze":
                _, details, bank, country, frozen_until = action
                await conn.execute(
                    "UPDATE card_details SET frozen_until = $4 WHERE details = $1 AND bank = $2 AND LOWER(country) = LOWER($3)",
                    details,
                    bank,
                    country,
                    frozen_until,
                )
            elif kind == "crypto_add":
                _, net_key, address = action
                await conn.execute(
                    "INSERT INTO crypto_details (network_key, address) VALUES ($1, $2)",
                    net_key,
                    address,
                )
            elif kind == "crypto_delete_addr":
                _, net_key, address = action
                await conn.execute(
                    "DELETE FROM crypto_details WHERE network_key = $1 AND address = $2",
                    net_key,
                    address,
                )
            elif kind == "crypto_delete":
                _, net_key = action
                await conn.execute(
                    "DELETE FROM crypto_details WHERE network_key = $1",
                    net_key,
                )
            elif kind == "known_user":
                _, user_id, last_seen, username = action[0], action[1], action[2], (action[3] if len(action) > 3 else None)
                await conn.execute(
                    """
                    INSERT INTO known_users (user_id, username, last_seen) VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO UPDATE SET
                        last_seen = EXCLUDED.last_seen,
                        username = COALESCE(EXCLUDED.username, known_users.username)
                    """,
                    user_id,
                    username,
                    last_seen,
                )
            elif kind == "last_broadcast":
                _, user_id, last_broadcast = action
                await conn.execute(
                    """
                    INSERT INTO known_users (user_id, last_seen, last_broadcast) VALUES ($1, $2, $2)
                    ON CONFLICT (user_id) DO UPDATE SET last_broadcast = EXCLUDED.last_broadcast
                    """,
                    user_id,
                    last_broadcast,
                )
            elif kind == "last_country":
                _, user_id, country = action
                await conn.execute(
                    """
                    INSERT INTO user_last_country (user_id, country) VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE SET country = EXCLUDED.country
                    """,
                    user_id,
                    country,
                )
            elif kind == "order_add":
                _, user_id, service_name, total_price, status = action
                await conn.execute(
                    "INSERT INTO user_orders (user_id, service_name, total_price, status) VALUES ($1, $2, $3, $4)",
                    user_id,
                    service_name,
                    int(round(float(total_price or 0))),
                    status,
                )
            elif kind == "order_set_last_status":
                _, user_id, status = action
                row = await conn.fetchrow(
                    "SELECT id FROM user_orders WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
                    user_id,
                )
                if row:
                    await conn.execute(
                        "UPDATE user_orders SET status = $1 WHERE id = $2",
                        status,
                        row["id"],
                    )
            elif kind == "order_set_index_status":
                _, user_id, order_idx, status = action
                rows = await conn.fetch(
                    "SELECT id FROM user_orders WHERE user_id = $1 ORDER BY id ASC",
                    user_id,
                )
                if 0 <= order_idx < len(rows):
                    await conn.execute(
                        "UPDATE user_orders SET status = $1 WHERE id = $2",
                        status,
                        rows[order_idx]["id"],
                    )
            elif kind == "bill_set":
                _, user_id, bill_data = action
                await conn.execute(
                    """
                    INSERT INTO user_bills (user_id, service_name, total_price, country, curr_code, curr_symbol, bill_msg_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (user_id) DO UPDATE SET
                        service_name = EXCLUDED.service_name,
                        total_price = EXCLUDED.total_price,
                        country = EXCLUDED.country,
                        curr_code = EXCLUDED.curr_code,
                        curr_symbol = EXCLUDED.curr_symbol,
                        bill_msg_id = EXCLUDED.bill_msg_id
                    """,
                    user_id,
                    bill_data.get("service_name", "Услуга"),
                    int(round(float(bill_data.get("total_price", 0) or 0))),
                    bill_data.get("country", "Россия"),
                    bill_data.get("curr_code", "RUB"),
                    bill_data.get("curr_symbol", "₽"),
                    bill_data.get("bill_msg_id"),
                )
            elif kind == "bill_delete":
                _, user_id = action
                await conn.execute(
                    "DELETE FROM user_bills WHERE user_id = $1",
                    user_id,
                )
            elif kind == "abandoned_set":
                _, user_id, remind_at = action
                await conn.execute(
                    """
                    INSERT INTO abandoned_checkouts (user_id, remind_at) VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE SET remind_at = EXCLUDED.remind_at
                    """,
                    user_id,
                    remind_at,
                )
            elif kind == "abandoned_delete":
                _, user_id = action
                await conn.execute(
                    "DELETE FROM abandoned_checkouts WHERE user_id = $1",
                    user_id,
                )
            elif kind == "promo_used_add":
                _, user_id, promo_code, discount_pct = action
                await conn.execute(
                    """
                    INSERT INTO used_promo_users (user_id, promo_code, discount_pct)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    user_id,
                    promo_code,
                    int(discount_pct),
                )
    except Exception as e:
        logger.error(f"Ошибка записи в PostgreSQL ({kind}): {e}")


async def db_writer_worker():
    while True:
        action = await DB_WRITE_QUEUE.get()
        try:
            await _exec_db_action(action)
        finally:
            DB_WRITE_QUEUE.task_done()


def queue_db(action: tuple):
    if not DATABASE_URL:
        return
    if DB_WRITE_QUEUE is not None:
        DB_WRITE_QUEUE.put_nowait(action)
    else:
        asyncio.create_task(_exec_db_action(action))


def _touch_user_activity(user_id: int, username: str | None = None):
    if user_id in BANNED_USERS:
        return
    now_dt = datetime.datetime.now()
    KNOWN_USERS[user_id] = now_dt
    queue_db(("known_user", user_id, now_dt, username))

    prof = USER_PROFILES.setdefault(
        user_id,
        {
            "username": username,
            "first_seen": now_dt,
            "last_seen": now_dt,
            "last_service": None,
            "max_step": STEP_LABELS.get("catalog"),
        },
    )
    if username:
        prof["username"] = username
    if not prof.get("first_seen"):
        prof["first_seen"] = now_dt
    prof["last_seen"] = now_dt


# === MIDDLEWARE ДЛЯ ПРОВЕРКИ БАНА ===
@router.message.middleware()
async def ban_middleware(handler, event, data):
    if event.from_user and event.from_user.id in BANNED_USERS:
        return
    return await handler(event, data)


@router.callback_query.middleware()
async def ban_callback_middleware(handler, event, data):
    if event.from_user and event.from_user.id in BANNED_USERS:
        return
    return await handler(event, data)


# === СЛОВАРЬ ВАЛЮТ ДЛЯ СТРАН (РОССИЯ, БЕЛАРУСЬ, УКРАИНА, КАЗАХСТАН) ===
COUNTRY_CURRENCIES = {
    "Россия": {"code": "RUB", "symbol": "₽"},
    "Беларусь": {"code": "BYN", "symbol": "BYN"},
    "Украина": {"code": "UAH", "symbol": "грн."},
    "Казахстан": {"code": "KZT", "symbol": "₸"},
}

# === КРИПТОСЕТИ ===
CRYPTO_NETWORKS = {
    "trc20": {
        "name": "USDT (TRC-20)",
        "network": "TRC-20",
        "keys": ["usdt trc-20", "usdt trc20", "trc-20", "trc20"],
    },
    "bep20": {
        "name": "USDT (BEP-20)",
        "network": "BEP-20",
        "keys": ["usdt bep-20", "usdt bep20", "bep-20", "bep20"],
    },
    "ton": {
        "name": "USDT (TON)",
        "network": "TON",
        "keys": ["usdt ton", "usdt (ton)", "ton"],
    },
    "btc": {
        "name": "Bitcoin (BTC)",
        "network": "Bitcoin",
        "keys": ["bitcoin (btc)", "bitcoin", "btc"],
    },
}


def normalize_crypto_key(raw_network: str):
    cleaned = " ".join(
        raw_network.lower().replace("(", " ").replace(")", " ").split()
    )
    for net_key, info in CRYPTO_NETWORKS.items():
        if cleaned == net_key or cleaned in info["keys"]:
            return net_key
    return None


# === ФУНКЦИЯ ЧЕРЕДОВАНИЯ РЕКВИЗИТОВ ===
def get_next_card(country: str):
    now = datetime.datetime.now()
    active = []
    for card in CARD_DETAILS:
        if card["country"].lower() == country.lower():
            if card["frozen_until"] and card["frozen_until"] > now:
                continue
            elif card["frozen_until"] and card["frozen_until"] <= now:
                card["frozen_until"] = None
                queue_db((
                    "card_freeze",
                    card["details"],
                    card["bank"],
                    card["country"],
                    None,
                ))
            active.append(card)

    if not active:
        return None

    if country not in CARD_POINTERS:
        CARD_POINTERS[country] = 0

    index = CARD_POINTERS[country] % len(active)
    chosen_card = active[index]
    CARD_POINTERS[country] = (index + 1) % len(active)
    return chosen_card


def get_next_crypto(net_key: str):
    addresses = CRYPTO_DETAILS.get(net_key) or []
    if not addresses:
        return None

    if net_key not in CRYPTO_POINTERS:
        CRYPTO_POINTERS[net_key] = 0

    index = CRYPTO_POINTERS[net_key] % len(addresses)
    chosen_address = addresses[index]
    CRYPTO_POINTERS[net_key] = (index + 1) % len(addresses)
    return chosen_address


# === ФУНКЦИЯ ПОЛУЧЕНИЯ КУРСОВ ВАЛЮТ ===
async def get_exchange_rate(target_currency: str) -> float:
    url = "https://open.er-api.com/v6/latest/USD"
    try:
        async with ClientSession() as session:
            async with session.get(url, timeout=2) as response:
                if response.status == 200:
                    data = await response.json()
                    rates = data.get("rates", {})
                    if target_currency in rates:
                        return float(rates[target_currency])
    except Exception as e:
        logger.warning(f"Не удалось получить курс для {target_currency}: {e}")

    fallback_rates = {
        "RUB": 95.0,
        "BYN": 3.3,
        "KZT": 480.0,
        "UAH": 41.5,
    }
    return fallback_rates.get(target_currency, 1.0)


def build_payment_keyboard(country: str, back_callback: str = "go_to_payment") -> InlineKeyboardMarkup:
    card_btn_text = "💳 Оплата картой"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=card_btn_text,
                    callback_data="pay_card_option",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 CryptoBot (без комиссии)",
                    callback_data="pay_cryptobot",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Криптовалюта",
                    callback_data="crypto_expert",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎟 Промокод",
                    callback_data="promo_code_entry",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Нужна помощь",
                    callback_data="payment_help_manager",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=back_callback, style="danger"
                )
            ],
        ]
    )


async def get_payment_context(user_id: int, state: FSMContext):
    data = await state.get_data()
    bill = USER_BILLS.get(user_id, {})
    prof = USER_PROFILES.get(user_id, {})
    country = (
        data.get("country")
        or bill.get("country")
        or USER_LAST_COUNTRY.get(user_id, "Россия")
    )
    curr_info = COUNTRY_CURRENCIES.get(
        country, {"code": "USD", "symbol": "USD"}
    )
    curr_code = (
        data.get("curr_code") or bill.get("curr_code") or curr_info["code"]
    )
    curr_symbol = (
        data.get("curr_symbol") or bill.get("curr_symbol") or curr_info["symbol"]
    )

    if bill:
        raw_price = bill.get("total_price", 250)
        raw_orig = bill.get("original_total_price", raw_price)
        discount_pct = int(bill.get("promo_discount_pct", 0) or 0)
    else:
        raw_price = (
            data.get("total_price") if data.get("total_price") is not None else 250
        )
        raw_orig = (
            data.get("original_total_price")
            if data.get("original_total_price") is not None
            else raw_price
        )
        discount_pct = int(data.get("promo_discount_pct", 0) or 0)

    # Применяем подтверждённый промокод ровно один раз на полную сумму первого заказа
    applied = USER_APPLIED_PROMOS.get(user_id)
    if applied and not _get_client_paid_orders(user_id):
        ap_pct = int(applied.get("discount_pct", 0) or 0)
        if ap_pct > 0 and discount_pct == 0:
            discount_pct = ap_pct
            raw_orig = raw_price
            raw_price = _apply_discount_once(raw_orig, discount_pct)
            applied["original_price"] = raw_orig
            applied["discounted_price"] = raw_price
            await state.update_data(
                original_total_price=raw_orig,
                total_price=raw_price,
                promo_discount_pct=discount_pct,
            )
            if user_id in USER_BILLS:
                USER_BILLS[user_id]["original_total_price"] = raw_orig
                USER_BILLS[user_id]["total_price"] = raw_price
                USER_BILLS[user_id]["promo_discount_pct"] = discount_pct
                queue_db(("bill_set", user_id, dict(USER_BILLS[user_id])))

    total_price = raw_price
    service_name = (
        data.get("service_name")
        or bill.get("service_name")
        or prof.get("last_service")
        or "Услуга"
    )
    enriched_data = dict(data)
    enriched_data["country"] = country
    enriched_data["curr_code"] = curr_code
    enriched_data["curr_symbol"] = curr_symbol
    enriched_data["service_name"] = service_name
    enriched_data["original_total_price"] = raw_orig
    enriched_data["total_price"] = total_price
    enriched_data["promo_discount_pct"] = discount_pct
    return enriched_data, country, curr_code, curr_symbol, total_price, service_name


def _build_order_details_text(
    data: dict, country: str, service_desc: str, total_price
) -> str:
    orig_price = data.get("original_total_price")
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    if disc_pct > 0 and orig_price and float(orig_price) > float(total_price or 0):
        price_val = (
            f"<s>{_format_num(orig_price)}$</s> → <b>{_format_num(total_price)}$</b> "
            f"(-{disc_pct}%)"
        )
    else:
        price_val = (
            f"{_format_num(total_price)}$"
            if total_price and float(total_price) > 0
            else "Расчет менеджера"
        )
    details = f"\n• Услуга: {service_desc}\n• Сумма: {price_val}"
    if country:
        details += f"\n• Страна: {country}"
    if data.get("urgency"):
        details += f"\n• Срочность: {data.get('urgency')}"
    if data.get("target_location"):
        details += f"\n• Локация цели: {data.get('target_location')}"
    if data.get("target_data"):
        details += f"\n• Данные цели: {data.get('target_data')}"
    if data.get("debt_info"):
        details += f"\n• Информация о долге: {data.get('debt_info')}"
    if data.get("debt_wallet"):
        details += f"\n• Кошелек для возврата (USDT/TON): {data.get('debt_wallet')}"
    if data.get("special_task"):
        details += f"\n• Задача (Особый заказ): {data.get('special_task')}"
    return details


# === КЛАВИАТУРА КАТАЛОГА УСЛУГ ===
def build_catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧎 Извинение на коленях — 85$",
                    callback_data="srv_Извинение_на_коленях_85",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🥊 Избить — 160$",
                    callback_data="srv_Избить_160",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💸 Вернуть долг — 230$",
                    callback_data="srv_Вернуть_долг_230",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🦴 Сломать пальцы — 310$",
                    callback_data="srv_Сломать_пальцы_310",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🦾 Сломать конечности — 460$",
                    callback_data="srv_Сломать_конечности_460",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪓 Отрубить конечности — 740$",
                    callback_data="srv_Отрубить_конечности_740",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚡️ Убийство — 4600$",
                    callback_data="srv_Убийство_4600",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Особый заказ — Рассчитать сумму",
                    callback_data="srv_Особый_заказ_0",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📖 О услугах",
                    url="https://telegra.ph/Podrobnyj-gajd-po-uslugam-i-oformleniyu-zakaza-11-20",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="back_home", style="primary"
                )
            ],
        ]
    )


# === РАСЧЕТ РЕПУТАЦИИ ===
def get_reputation_stats():
    current_date = datetime.date.today()
    base_date = datetime.date.today()
    days_passed = max(0, (current_date - base_date).days)
    completed = 5173 + days_passed * 312
    rng = random.Random(current_date.toordinal())
    in_progress = rng.randint(175, 225)
    new_requests = rng.randint(256, 409)
    return completed, in_progress, new_requests


# === ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ПОИСКА БАННЕРА ===
def get_banner_path(banner_name: str):
    base_name = os.path.splitext(banner_name)[0]
    if os.path.exists("banners"):
        for filename in os.listdir("banners"):
            name_without_ext = os.path.splitext(filename)[0]
            if name_without_ext.lower() == base_name.lower():
                return os.path.join("banners", filename)
    return None


# === УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ЦЕЛЬНОГО ОБНОВЛЕНИЯ БАННЕРОВ (С КЕШИРОВАНИЕМ FILE_ID) ===
async def render_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    banner_name: str,
    text: str,
    reply_markup: InlineKeyboardMarkup,
):
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass

    bot = event.bot
    chat_id = (
        event.chat.id if isinstance(event, Message) else event.message.chat.id
    )

    data = await state.get_data()
    bot_msg_id = data.get("bot_msg_id")

    photo_source = None
    banner_path = get_banner_path(banner_name)

    if banner_name in BANNER_CACHE:
        photo_source = BANNER_CACHE[banner_name]
    elif banner_path and os.path.exists(banner_path):
        photo_source = FSInputFile(banner_path)

    if isinstance(event, CallbackQuery):
        msg = event.message
        if photo_source:
            try:
                if msg.photo:
                    sent_msg = await msg.edit_media(
                        media=InputMediaPhoto(
                            media=photo_source, caption=text, parse_mode="HTML"
                        ),
                        reply_markup=reply_markup,
                    )
                else:
                    await msg.delete()
                    sent_msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_source,
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )

                if (
                    not banner_name in BANNER_CACHE
                    and hasattr(sent_msg, "photo")
                    and sent_msg.photo
                ):
                    BANNER_CACHE[banner_name] = sent_msg.photo[-1].file_id
            except Exception:
                try:
                    await msg.delete()
                except Exception:
                    pass
                new_msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_source,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
                if not banner_name in BANNER_CACHE and new_msg.photo:
                    BANNER_CACHE[banner_name] = new_msg.photo[-1].file_id
                await state.update_data(bot_msg_id=new_msg.message_id)
                return
        else:
            try:
                await msg.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
            except Exception:
                try:
                    new_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                    await state.update_data(bot_msg_id=new_msg.message_id)
                except Exception:
                    pass
                return
        await state.update_data(bot_msg_id=msg.message_id)
    else:
        try:
            await event.delete()
        except Exception:
            pass

        if bot_msg_id:
            try:
                if photo_source:
                    sent_msg = await bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=bot_msg_id,
                        media=InputMediaPhoto(
                            media=photo_source, caption=text, parse_mode="HTML"
                        ),
                        reply_markup=reply_markup,
                    )
                    if (
                        not banner_name in BANNER_CACHE
                        and hasattr(sent_msg, "photo")
                        and sent_msg.photo
                    ):
                        BANNER_CACHE[banner_name] = sent_msg.photo[-1].file_id
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=bot_msg_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                return
            except Exception:
                pass

        if photo_source:
            new_msg = await bot.send_photo(
                chat_id=chat_id,
                photo=photo_source,
                caption=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            if not banner_name in BANNER_CACHE and new_msg.photo:
                BANNER_CACHE[banner_name] = new_msg.photo[-1].file_id
        else:
            new_msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        await state.update_data(bot_msg_id=new_msg.message_id)


async def _full_reset_user_session(user_id: int, state: FSMContext, username: str | None = None):
    await state.clear()
    _reset_payment_step_tracking(user_id, leave_step=True)
    _cancel_abandoned_reminder(user_id)
    _clear_back_after_service_flags(user_id)
    PROBLEM_USERS.pop(user_id, None)
    _touch_user_activity(user_id, username)
    if user_id in USER_PROFILES:
        USER_PROFILES[user_id]["max_step"] = STEP_LABELS.get("catalog")
    USER_STEP_RANK[user_id] = STEP_RANKS.get("catalog", 1)


# === ГЛАВНОЕ МЕНЮ ===
async def show_main_menu(event: Message | CallbackQuery, state: FSMContext):
    user_id = event.from_user.id
    if user_id in BANNED_USERS:
        return
    await state.clear()
    _reset_payment_step_tracking(user_id, leave_step=True)
    _clear_back_after_service_flags(user_id)
    _touch_user_activity(user_id, event.from_user.username)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Заказать", callback_data="start_order", style="danger"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Мои заказы",
                    callback_data="my_orders",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📊 Репутация",
                    callback_data="reputation",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Поддержка",
                    callback_data="contact_admin",
                    style="danger",
                )
            ],
        ]
    )
    text = (
        "🏆 <b>ARENA SHOP — СПОРТ</b>\n\n"
        "<i>Добро пожаловать в сервис. Все конфиденциально, быстро и безопасно. Работаем 24/7.</i>\n\n"
        "<b>👇 Выберите пункт меню ниже:</b>"
    )
    await render_step(event, state, "main.jpg", text, keyboard)


# === ПОСЛЕДОВАТЕЛЬНЫЙ ВОЗВРАТ НА ПРЕДЫДУЩИЙ ШАГ ОФОРМЛЕНИЯ ЗАКАЗА ===
async def _show_catalog_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(
        event.from_user.id,
        "catalog",
        event.from_user.username,
        service_name=data.get("service_name"),
    )
    await state.set_state(OrderForm.service)
    if record_history:
        await _push_nav_step(state, "service")
    text = (
        "🛒 <b>Каталог услуг</b>\n\nВыберите подходящий вариант из списка ниже:"
    )
    await render_step(event, state, "catalog.jpg", text, build_catalog_keyboard())


async def _show_debt_info_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "debt_info", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.debt_info)
    if record_history:
        await _push_nav_step(state, "debt_info")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['debt_info']}</code>" if data.get("debt_info") else ""
    text = (
        "💸 <b>Вернуть долг</b>\n\n"
        "Укажите информацию о долге: какая сумма долга, данные должника и т.д.:"
        f"{prev_val}"
    )
    await render_step(event, state, "catalog.jpg", text, keyboard)


async def _show_debt_wallet_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "debt_wallet", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.debt_wallet)
    if record_history:
        await _push_nav_step(state, "debt_wallet")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['debt_wallet']}</code>" if data.get("debt_wallet") else ""
    text = (
        "💸 <b>Вернуть долг</b>\n\n"
        "Теперь укажите свой криптокошелек <b>USDT (TRC-20 / BEP-20)</b> или"
        " <b>TON</b>, чтобы получить назад деньги с должника:"
        f"{prev_val}"
    )
    await render_step(event, state, "catalog.jpg", text, keyboard)


async def _show_special_task_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "special_task", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.special_task)
    if record_history:
        await _push_nav_step(state, "special_task")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['special_task']}</code>" if data.get("special_task") else ""
    text = (
        "🎯 <b>Особый заказ</b>\n\n"
        "Опишите вашу задачу, что нужно будет сделать (с вами свяжется менеджер"
        " для расчета стоимости):"
        f"{prev_val}"
    )
    await render_step(event, state, "catalog.jpg", text, keyboard)


async def _show_target_location_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    selected = data.get("country") or USER_LAST_COUNTRY.get(event.from_user.id, "Россия")
    _track_user_step(event.from_user.id, "target_location", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.target_location)
    if record_history:
        await _push_nav_step(state, "target_location")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['target_location']}</code>" if data.get("target_location") else ""
    text = (
        f"📍 Страна: <b>{selected}</b>\n\n"
        "🏢 <b>Локация цели</b>\n"
        "Укажите город, адрес, дом или квартиру следующим сообщением:"
        f"{prev_val}"
    )
    await render_step(event, state, "location.jpg", text, keyboard)


async def _show_target_data_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "target_data", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.target_data)
    if record_history:
        await _push_nav_step(state, "target_data")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="order_back_step", style="primary"
            )
        ]]
    )
    prev_val = f"\n\n<i>Ранее введено:</i> <code>{data['target_data']}</code>" if data.get("target_data") else ""
    text = (
        "🪪 <b>Данные цели</b>\n\n"
        "Укажите ФИО или другую информацию о цели (если неизвестно, напишите"
        " «неизвестно»):"
        f"{prev_val}"
    )
    await render_step(event, state, "target_data.jpg", text, keyboard)


async def _show_photos_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    photos = data.get("photos") or []
    _track_user_step(event.from_user.id, "photos", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.photos)
    if record_history:
        await _push_nav_step(state, "photos")
    if photos:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"✅ Готово ({len(photos)}/3)",
                        callback_data="finish_photos",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="order_back_step", style="danger"
                    )
                ],
            ]
        )
        text = (
            f"📸 Фото ({len(photos)}/3) сохранены. Можете прикрепить еще или"
            " завершить:"
        )
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏩ Пропустить",
                        callback_data="finish_photos",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="order_back_step", style="danger"
                    )
                ],
            ]
        )
        text = (
            "📸 Прикрепите фото цели (до 3 шт.) или нажмите кнопку пропуска:"
        )
    await render_step(event, state, "photos.jpg", text, keyboard)


async def _show_urgency_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    data = await state.get_data()
    _track_user_step(event.from_user.id, "urgency", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.urgency)
    if record_history:
        await _push_nav_step(state, "urgency")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡ За 2-3 часа (+80$)",
                    callback_data="urg_fast_80",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🕐 Завтра (+40$)",
                    callback_data="urg_tomorrow_40",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 Стандартно (2-5 дней)",
                    callback_data="urg_standard_0",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="order_back_step", style="danger"
                )
            ],
        ]
    )
    text = "⏱ <b>Срочность выполнения</b>\n\nВыберите желаемый вариант:"
    await render_step(event, state, "urgency.jpg", text, keyboard)


async def _show_summary_step(
    event: Message | CallbackQuery, state: FSMContext, record_history: bool = True
):
    user_id = event.from_user.id
    data = await state.get_data()
    _track_user_step(user_id, "summary", event.from_user.username, service_name=data.get("service_name"))
    await state.set_state(OrderForm.summary)
    if record_history:
        await _push_nav_step(state, "summary")

    base_service_price = data.get("service_price", 0)
    urg_price = data.get("urgency_price", 0)
    urgency = data.get("urgency") or "📅 2-5 дней"
    raw_total_price = (base_service_price + urg_price) if base_service_price > 0 else 0

    discount_pct = int(data.get("promo_discount_pct", 0) or 0)
    applied = USER_APPLIED_PROMOS.get(user_id)
    if applied and not _get_client_paid_orders(user_id):
        ap_pct = int(applied.get("discount_pct", 0) or 0)
        if ap_pct > 0:
            discount_pct = ap_pct

    if raw_total_price > 0 and discount_pct > 0:
        total_price = _apply_discount_once(raw_total_price, discount_pct)
    else:
        total_price = raw_total_price

    await state.update_data(
        original_total_price=raw_total_price,
        total_price=total_price,
        promo_discount_pct=discount_pct,
        urgency=urgency,
    )

    rate = await get_exchange_rate(data.get("curr_code", "USD"))
    local_amount = round(float(total_price) * rate) if float(total_price) > 0 else 0

    if float(raw_total_price) > 0 and discount_pct > 0:
        price_display = (
            f"<s>{_format_num(raw_total_price)}$</s> → <b>{_format_num(total_price)}$</b> "
            f"<b>(-{discount_pct}%)</b> <i>(≈ {local_amount:,} {data.get('curr_symbol')})</i>"
        )
    elif float(total_price) > 0:
        price_display = (
            f"<b>{_format_num(total_price)}$</b> <i>(≈ {local_amount:,}"
            f" {data.get('curr_symbol')})</i>"
        )
    else:
        price_display = "<b>Рассчитывается менеджером</b>"

    is_special = (data.get("service_name") or "").strip().lower() == "особый заказ"
    action_btn_text = "✅ Отправить заявку" if is_special else "💳 Перейти к оплате"
    action_callback = "submit_special_order" if is_special else "go_to_payment"

    summary_text = (
        "✨ <b>Сводка заказа:</b>\n\n"
        f"• Услуга: <b>{data.get('service_name')}</b>\n"
        f"• Срочность: {urgency}\n"
        f"• Страна: {data.get('country')}\n\n"
        f"💵 <b>Сумма к оплате: {price_display}</b>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=action_btn_text,
                    callback_data=action_callback,
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="order_back_step", style="primary"
                )
            ],
        ]
    )
    await render_step(event, state, "summary.jpg", summary_text, keyboard)


async def _navigate_to_step_key(
    callback: CallbackQuery, state: FSMContext, step_key: str
):
    data = await state.get_data()
    service_name = data.get("service_name") or ""
    if step_key == "service":
        await _show_catalog_step(callback, state, record_history=False)
    elif step_key == "debt_info":
        await _show_debt_info_step(callback, state, record_history=False)
    elif step_key == "debt_wallet":
        await _show_debt_wallet_step(callback, state, record_history=False)
    elif step_key == "special_task":
        await _show_special_task_step(callback, state, record_history=False)
    elif step_key == "country":
        await show_country_selection(
            callback, state, service_name, data.get("service_price", 0), record_history=False
        )
    elif step_key == "target_location":
        await _show_target_location_step(callback, state, record_history=False)
    elif step_key == "target_data":
        await _show_target_data_step(callback, state, record_history=False)
    elif step_key == "photos":
        await _show_photos_step(callback, state, record_history=False)
    elif step_key == "urgency":
        await _show_urgency_step(callback, state, record_history=False)
    elif step_key == "summary":
        await _show_summary_step(callback, state, record_history=False)
    else:
        await _show_catalog_step(callback, state, record_history=False)


@router.callback_query(F.data == "order_back_step")
async def cb_order_back_step(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    current_state = await state.get_state()
    data = await state.get_data()
    service_name = data.get("service_name") or ""

    if user_id in ON_PAYMENT_METHOD_STEP:
        asyncio.create_task(
            _send_unpaid_payment_step_notice(
                bot=callback.bot,
                user_id=user_id,
                reason_text="Нажал «Назад» на этапе выбора способа оплаты",
            )
        )

    state_to_key = {
        OrderForm.service.state: "service",
        OrderForm.debt_info.state: "debt_info",
        OrderForm.debt_wallet.state: "debt_wallet",
        OrderForm.special_task.state: "special_task",
        OrderForm.country.state: "country",
        OrderForm.target_location.state: "target_location",
        OrderForm.target_data.state: "target_data",
        OrderForm.photos.state: "photos",
        OrderForm.urgency.state: "urgency",
        OrderForm.summary.state: "summary",
        OrderForm.payment.state: "payment",
        OrderForm.receipt.state: "receipt",
    }
    current_key = state_to_key.get(current_state)
    prev_step = await _pop_nav_step(state, current_key)

    if prev_step and prev_step != current_key:
        await _navigate_to_step_key(callback, state, prev_step)
        return

    # Резервный детерминированный переход назад, если стек пуст
    if current_state == OrderForm.debt_info.state:
        await _show_catalog_step(callback, state, record_history=False)
    elif current_state == OrderForm.debt_wallet.state:
        await _show_debt_info_step(callback, state, record_history=False)
    elif current_state == OrderForm.special_task.state:
        await _show_catalog_step(callback, state, record_history=False)
    elif current_state == OrderForm.country.state:
        if service_name == "Вернуть долг":
            await _show_debt_wallet_step(callback, state, record_history=False)
        elif service_name == "Особый заказ":
            await _show_special_task_step(callback, state, record_history=False)
        else:
            await _show_catalog_step(callback, state, record_history=False)
    elif current_state == OrderForm.target_location.state:
        await show_country_selection(
            callback, state, service_name, data.get("service_price", 0), record_history=False
        )
    elif current_state == OrderForm.target_data.state:
        await _show_target_location_step(callback, state, record_history=False)
    elif current_state == OrderForm.photos.state:
        await _show_target_data_step(callback, state, record_history=False)
    elif current_state == OrderForm.urgency.state:
        await _show_photos_step(callback, state, record_history=False)
    elif current_state == OrderForm.summary.state:
        await _show_urgency_step(callback, state, record_history=False)
    elif current_state == OrderForm.payment.state:
        if service_name and (service_name or "").strip().lower() != "особый заказ":
            await _show_summary_step(callback, state, record_history=False)
        elif data.get("country"):
            await _show_urgency_step(callback, state, record_history=False)
        else:
            await _show_catalog_step(callback, state, record_history=False)
    else:
        await _show_catalog_step(callback, state, record_history=False)


# === АДМИНСКИЕ И МЕНЕДЖЕРСКИЕ КОМАНДЫ ===
@router.message(Command("ban"))
async def cmd_ban_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование: `/ban <user_id>`", parse_mode="Markdown"
        )
        return
    target = args[1].strip()
    if target.isdigit():
        user_id = int(target)
        BANNED_USERS.add(user_id)
        queue_db(("ban_add", user_id))
        _reset_payment_step_tracking(user_id, leave_step=True)
        _cancel_abandoned_reminder(user_id)
        _clear_back_after_service_flags(user_id)
        _close_dialog_state(user_id)
        USER_LAST_BROADCAST.pop(user_id, None)
        PROBLEM_USERS.pop(user_id, None)
        USER_BILLS.pop(user_id, None)
        queue_db(("bill_delete", user_id))
        for t in TICKETS.values():
            if t.get("user_id") == user_id:
                t["status"] = "🔒 Закрыто (бан)"
        if message.bot and message.from_user.id == user_id:
            await state.clear()
        await message.reply(
            f"✅ Пользователь с ID <code>{user_id}</code> заблокирован. Все напоминания, диалоги, рассылки и активные состояния остановлены.",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            "❌ Укажите корректный числовой ID.", parse_mode="Markdown"
        )


@router.message(Command("unban"))
async def cmd_unban_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование: `/unban <user_id>`", parse_mode="Markdown"
        )
        return
    target = args[1].strip()
    if target.isdigit():
        user_id = int(target)
        BANNED_USERS.discard(user_id)
        queue_db(("ban_remove", user_id))
        await message.reply(
            f"✅ Пользователь с ID <code>{user_id}</code> разблокирован.",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            "❌ Укажите корректный числовой ID.", parse_mode="Markdown"
        )


async def _set_order_status_and_notify(
    bot: Bot,
    target_user_id: int,
    new_status: str,
    order_idx: int | None = None,
) -> tuple[bool, str]:
    if target_user_id in BANNED_USERS:
        return False, "Пользователь заблокирован"

    orders = USER_ORDERS.get(target_user_id, [])
    service_title = "Ваш заказ"

    if orders:
        if order_idx is not None and 0 <= order_idx < len(orders):
            orders[order_idx]["status"] = new_status
            service_title = orders[order_idx].get("service_name") or service_title
            queue_db(("order_set_index_status", target_user_id, order_idx, new_status))
        else:
            orders[-1]["status"] = new_status
            service_title = orders[-1].get("service_name") or service_title
            queue_db(("order_set_last_status", target_user_id, new_status))
    else:
        bill = USER_BILLS.get(target_user_id) or {}
        if bill.get("service_name"):
            service_title = bill["service_name"]
        elif USER_PROFILES.get(target_user_id, {}).get("last_service"):
            service_title = USER_PROFILES[target_user_id]["last_service"]

    if "выполнен" in new_status.lower():
        _reset_payment_step_tracking(target_user_id, leave_step=True)
        _cancel_abandoned_reminder(target_user_id)
        _clear_back_after_service_flags(target_user_id)
        USER_BILLS.pop(target_user_id, None)
        queue_db(("bill_delete", target_user_id))

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                f"📌 <b>Статус вашего заказа обновлен: {new_status}</b>\n\n"
                f"• Услуга / заказ: <b>{service_title}</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    return True, service_title


@router.message(Command("work"))
async def cmd_work_order(message: Message):
    if not is_staff(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply(
            "❌ Использование: `/work <user_id>`\nПример: `/work 4324324`",
            parse_mode="Markdown",
        )
        return

    target_user_id = int(args[1].strip())
    if target_user_id in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return
    if not USER_ORDERS.get(target_user_id):
        await message.reply(
            f"❌ У пользователя <code>{target_user_id}</code> нет заказов.",
            parse_mode="HTML",
        )
        return

    ok, service_title = await _set_order_status_and_notify(
        message.bot, target_user_id, "⚙️ В работе"
    )
    if ok:
        await message.reply(
            f"✅ Заказ пользователя <code>{target_user_id}</code> "
            f"(<b>{service_title}</b>) переведен в статус <b>В работе</b>.",
            parse_mode="HTML",
        )


@router.message(Command("bill"))
async def cmd_bill_user(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование: `/bill <user_id или @username> <услуга> <сумма$>`\nПример: `/bill"
            " 282810030 особый заказ 220$`",
            parse_mode="Markdown",
        )
        return

    parts = args[1].strip().split()
    if len(parts) < 2:
        await message.reply(
            "❌ Укажите корректный `user_id` или `@username`, название услуги и сумму.\nПример:"
            " `/bill 282810030 особый заказ 220$`",
            parse_mode="Markdown",
        )
        return

    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        bill_body_text = " ".join(parts[1:])
        forbidden = find_forbidden_requisites_for_manager(bill_body_text)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    target_user_id = _resolve_target_user_id(parts[0])
    if target_user_id is None:
        await message.reply(
            "❌ Укажите корректный числовой `user_id` или известный боту `@username` клиента.",
            parse_mode="Markdown",
        )
        return

    if target_user_id in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    raw_amount = (
        parts[-1]
        .replace("$", "")
        .replace("usd", "")
        .replace("USD", "")
        .replace(",", ".")
        .strip()
    )
    try:
        amount_usd = float(raw_amount)
        if amount_usd <= 0:
            raise ValueError()
    except ValueError:
        await message.reply(
            "❌ Некорректная сумма. Пример: `/bill 282810030 особый заказ 220$`",
            parse_mode="Markdown",
        )
        return

    service_name = " ".join(parts[1:-1]).strip() if len(parts) > 2 else "Особый заказ"
    if not service_name:
        service_name = "Особый заказ"

    _, reply_msg = await _issue_bill_to_client(
        bot=message.bot,
        caller_id=message.from_user.id,
        caller_state=state,
        target_user_id=target_user_id,
        service_name=service_name,
        amount_usd=amount_usd,
    )
    await message.reply(reply_msg, parse_mode="HTML")


@router.message(Command("send"))
async def cmd_send_to_user(message: Message):
    if not is_staff(message.from_user.id):
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply(
            "❌ Использование: `/send <user_id или @username> <текст сообщения>`",
            parse_mode="Markdown",
        )
        return
    target_raw = args[1].strip()
    text_to_send = args[2].strip()
    target_user_id = _resolve_target_user_id(target_raw)
    if target_user_id is None:
        await message.reply(
            "❌ Укажите корректный числовой ID или известный боту `@username` пользователя.",
            parse_mode="Markdown",
        )
        return

    if target_user_id in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        forbidden = find_forbidden_requisites_for_manager(text_to_send)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    ok, err_text = await _deliver_send_message_to_client(
        message.bot,
        target_user_id=target_user_id,
        text_to_send=text_to_send,
        prefix="💬 <b>Сообщение от менеджера:</b>",
        staff_id=message.from_user.id,
    )
    if ok:
        await message.reply(
            f"✅ Сообщение отправлено пользователю <code>{target_user_id}</code>.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ Не удалось отправить сообщение: {err_text}")


@router.message(Command("roll"))
async def cmd_roll_funnel(message: Message):
    if not is_staff(message.from_user.id):
        return

    active_uids = [uid for uid in KNOWN_USERS.keys() if uid not in BANNED_USERS]
    if not active_uids:
        await message.reply("📊 <b>Воронка и активность:</b> пока нет активных клиентов.", parse_mode="HTML")
        return

    active_uids.sort(
        key=lambda u: (USER_PROFILES.get(u, {}).get("last_seen") or KNOWN_USERS.get(u) or datetime.datetime.min),
        reverse=True,
    )

    lines = [f"📊 <b>Воронка и активность клиентов ({len(active_uids)} чел.):</b>\n"]
    for idx, uid in enumerate(active_uids[:30], 1):
        prof = USER_PROFILES.get(uid, {})
        uname = prof.get("username")
        tag = f"@{uname}" if uname else "без @username"
        step = prof.get("max_step") or STEP_LABELS.get("catalog")
        service = prof.get("last_service") or (USER_BILLS.get(uid) or {}).get("service_name") or "не выбрана"
        status = _get_latest_order_status(uid)
        lines.append(
            f"{idx}. {tag} | ID: <code>{uid}</code>\n"
            f"   • Этап: <b>{step}</b>\n"
            f"   • Услуга / заполнялось: <b>{service}</b>\n"
            f"   • Статус заказа: <b>{status}</b>"
        )
    if len(active_uids) > 30:
        lines.append(f"\n<i>Показаны 30 самых свежих клиентов из {len(active_uids)}.</i>")

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("aid"))
async def cmd_aid_problem_users(message: Message):
    if not is_staff(message.from_user.id):
        return

    problem_entries = []
    for uid, info in PROBLEM_USERS.items():
        if uid in BANNED_USERS:
            continue
        problem_entries.append((uid, info))

    known_problem_uids = {u for u, _ in problem_entries}
    for t in TICKETS.values():
        uid = t.get("user_id")
        if not uid or uid in BANNED_USERS or uid in known_problem_uids:
            continue
        if t.get("kind") in ("support", "payment_help", "special_order", "unpaid_checkout", "promo_request") and t.get("status") != "🔒 Закрыто":
            prof = USER_PROFILES.get(uid, {})
            problem_entries.append((
                uid,
                {
                    "kind": t.get("kind"),
                    "step": prof.get("max_step") or "Обращение",
                    "query": "",
                    "data": t.get("extra_data") or {},
                    "updated_at": datetime.datetime.now(),
                },
            ))
            known_problem_uids.add(uid)

    if not problem_entries:
        await message.reply("🆘 <b>Проблемные клиенты:</b> сейчас нет клиентов в особых ветках, проблемах с оплатой или поддержке.", parse_mode="HTML")
        return

    kind_titles = {
        "special_order": "🎯 Особый заказ",
        "payment_help": "💳 Проблема с оплатой",
        "support": "💬 Поддержка",
        "unpaid_checkout": "⏳ Неоплаченный заказ",
        "send_reply": "↩️ Переписка по обращению",
        "promo_request": "🎟 Запрос промокода",
    }

    lines = [f"🆘 <b>Проблемные клиенты ({len(problem_entries)}):</b>\n"]
    for idx, (uid, info) in enumerate(problem_entries[:30], 1):
        prof = USER_PROFILES.get(uid, {})
        uname = prof.get("username")
        tag = f"@{uname}" if uname else "без @username"
        k_title = kind_titles.get(info.get("kind", ""), info.get("kind", "Обращение"))
        step = info.get("step") or prof.get("max_step") or "—"
        query_str = info.get("query") or "—"
        d = info.get("data") or {}
        filled_parts = []
        for field_key, label in (
            ("service_name", "услуга"),
            ("country", "страна"),
            ("target_location", "локация"),
            ("target_data", "цель"),
            ("special_task", "задача"),
            ("debt_info", "долг"),
            ("debt_wallet", "кошелек"),
            ("urgency", "срочность"),
            ("total_price", "сумма$"),
        ):
            if d.get(field_key):
                filled_parts.append(f"{label}: {d[field_key]}")
        filled_str = ", ".join(filled_parts) if filled_parts else "нет доп. данных"

        lines.append(
            f"{idx}. <b>{k_title}</b> — {tag} | ID: <code>{uid}</code>\n"
            f"   • Этап: <b>{step}</b>\n"
            f"   • Запрос: {query_str}\n"
            f"   • Данные: {filled_str}"
        )

    await message.reply("\n".join(lines), parse_mode="HTML")


# === ПРОМОКОДЫ: ВВОД КЛИЕНТОМ И РУЧНОЕ ПОДТВЕРЖДЕНИЕ АДМИНОМ / МЕНЕДЖЕРОМ ===
_PROMO_WORD_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_-]{2,32}$")
PROMO_BANNER_NAME = "promocode.jpg"


def _build_promo_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Вернуться к заказу",
                callback_data="promo_back_to_order",
                style="primary",
            )
        ]]
    )


def _build_promo_staff_keyboard(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"promo_approve_{req_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"promo_reject_{req_id}",
                    style="danger",
                ),
            ]
        ]
    )


async def _send_promo_return_notice(bot: Bot, user_id: int, text: str):
    if user_id in BANNED_USERS:
        return
    banner_path = get_banner_path(PROMO_BANNER_NAME)
    kb = _build_promo_back_keyboard()
    if PROMO_BANNER_NAME in BANNER_CACHE:
        await bot.send_photo(
            chat_id=user_id,
            photo=BANNER_CACHE[PROMO_BANNER_NAME],
            caption=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    elif banner_path and os.path.exists(banner_path):
        sent = await bot.send_photo(
            chat_id=user_id,
            photo=FSInputFile(banner_path),
            caption=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
        if sent.photo:
            BANNER_CACHE[PROMO_BANNER_NAME] = sent.photo[-1].file_id
    else:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
        )


async def _restore_or_go_order_on_back(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id in BANNED_USERS:
        return
    try:
        await callback.answer()
    except Exception:
        pass

    _touch_user_activity(user_id, callback.from_user.username)
    data = await state.get_data()
    bill = USER_BILLS.get(user_id) or {}
    prof = USER_PROFILES.get(user_id) or {}

    has_order_ctx = bool(
        data.get("service_name")
        or data.get("total_price") is not None
        or data.get("service_price") is not None
        or data.get("country")
        or bill
        or prof.get("last_service")
    )

    if has_order_ctx:
        restored_country = (
            data.get("country")
            or bill.get("country")
            or USER_LAST_COUNTRY.get(user_id, "Россия")
        )
        curr_info = COUNTRY_CURRENCIES.get(
            restored_country, {"code": "USD", "symbol": "USD"}
        )
        restored_service = (
            data.get("service_name")
            or bill.get("service_name")
            or prof.get("last_service")
            or "Услуга"
        )
        if bill:
            restored_price = bill.get("total_price", 250)
            restored_orig = bill.get("original_total_price", restored_price)
            restored_disc = int(bill.get("promo_discount_pct", 0) or 0)
        else:
            raw_fallback = (
                data.get("total_price")
                if data.get("total_price") is not None
                else data.get("service_price")
            )
            restored_price = raw_fallback if raw_fallback is not None else 250
            restored_orig = (
                data.get("original_total_price")
                if data.get("original_total_price") is not None
                else restored_price
            )
            restored_disc = int(data.get("promo_discount_pct", 0) or 0)

        await state.update_data(
            service_name=restored_service,
            country=restored_country,
            curr_code=data.get("curr_code") or bill.get("curr_code") or curr_info["code"],
            curr_symbol=data.get("curr_symbol") or bill.get("curr_symbol") or curr_info["symbol"],
            total_price=restored_price,
            original_total_price=restored_orig,
            promo_discount_pct=restored_disc,
        )
        _track_user_step(
            user_id,
            "payment",
            callback.from_user.username,
            service_name=restored_service,
        )
        await state.set_state(OrderForm.payment)
        await process_go_to_payment(callback, state)
    else:
        _reset_payment_step_tracking(user_id, leave_step=True)
        await _show_catalog_step(callback, state, record_history=True)


@router.callback_query(
    F.data.in_({"promo_back_to_order", "promo_code_back_to_order", "back_to_order"})
    | F.data.startswith("promo_back")
)
async def cb_promo_back_to_order(callback: CallbackQuery, state: FSMContext):
    await _restore_or_go_order_on_back(callback, state)


@router.callback_query(F.data == "promo_code_entry")
async def cb_promo_code_entry(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _touch_user_activity(user_id, callback.from_user.username)

    if _get_client_paid_orders(user_id):
        await callback.answer(
            "❌ Промокод доступен только на первый заказ.", show_alert=True
        )
        return

    if user_id in USED_PROMO_USERS or user_id in USER_APPLIED_PROMOS:
        await callback.answer(
            "❌ Вы уже использовали промокод (он применяется только один раз).",
            show_alert=True,
        )
        return

    _reset_payment_step_tracking(user_id, leave_step=True)
    await state.set_state(OrderForm.promo_code)

    text = (
        "🎟 <b>Активация промокода</b>\n\n"
        "Впиши промокод одним словом следующим сообщением.\n\n"
        "💡 <i>Не знаешь как получить — пиши менеджеру (актуальный юзернейм менеджера "
        "вы всегда можете найти в нашем телеграм канале) и получи секретный промокод на скидку от -10 до -50% на первый заказ!</i>"
    )
    await render_step(callback, state, PROMO_BANNER_NAME, text, _build_promo_back_keyboard())


@router.message(OrderForm.promo_code, F.text)
async def process_promo_code_input(message: Message, state: FSMContext):
    global PROMO_REQUEST_COUNTER
    user = message.from_user
    user_id = user.id
    raw_code = message.text.strip()
    cmd_head = raw_code.split()[0].lower().split("@")[0] if raw_code else ""
    if cmd_head in ("/start", "/restart"):
        await _full_reset_user_session(user_id, state, user.username)
        if cmd_head == "/restart":
            try:
                await message.reply("🔄 Бот перезапущен, состояние очищено.")
            except Exception:
                pass
        await show_main_menu(message, state)
        return

    _touch_user_activity(user_id, user.username)
    words = raw_code.split()
    if len(words) != 1 or not _PROMO_WORD_RE.match(words[0]):
        await _send_promo_return_notice(
            message.bot,
            user_id,
            "❌ Неправильный промокод, АРЕНУ не обманешь)\n\n"
            "Введите промокод <b>одним словом</b> или нажмите кнопку возврата:",
        )
        return

    code_upper = words[0].upper()

    if _get_client_paid_orders(user_id):
        await _send_promo_return_notice(
            message.bot,
            user_id,
            "❌ Промокод применяется только на первый заказ.",
        )
        return

    if user_id in USED_PROMO_USERS or user_id in USER_APPLIED_PROMOS:
        await _send_promo_return_notice(
            message.bot,
            user_id,
            "❌ Промокод уже был применён ранее (строго один раз на первый заказ).",
        )
        return

    # Определяем процент скидки по промокоду (BLOODYEAR = 30%, для прочих кодов — 10% по умолчанию)
    discount_pct = 30 if code_upper == "BLOODYEAR" else 10

    data, country, curr_code, curr_symbol, current_price, service_name = (
        await get_payment_context(user_id, state)
    )
    base_full_price = data.get("original_total_price") or current_price or 0

    _track_user_step(user_id, "promo", user.username, service_name=service_name)
    _record_problem_user(
        user_id=user_id,
        kind="promo_request",
        step_label="запросил промокод",
        query_text=f"Промокод: {code_upper} (-{discount_pct}%)",
        data=data,
    )

    PROMO_REQUEST_COUNTER += 1
    req_id = PROMO_REQUEST_COUNTER
    PROMO_REQUESTS[req_id] = {
        "req_id": req_id,
        "user_id": user_id,
        "username": user.username,
        "code": code_upper,
        "discount_pct": discount_pct,
        "base_full_price": base_full_price,
        "country": country,
        "curr_code": curr_code,
        "curr_symbol": curr_symbol,
        "service_name": service_name,
        "order_data": dict(data),
        "status": "pending",
        "staff_msgs": {},
    }

    user_tag = f"@{user.username}" if user.username else "без @username"
    order_details = _build_order_details_text(data, country, service_name, base_full_price)
    summary_block = _format_user_summary_block(user_id, data)

    staff_text = (
        f"🎟 <b>Запрос активации промокода #{req_id}</b>\n"
        f"• Статус: <b>запросил промокод</b>\n"
        f"• Клиент: {user_tag} | ID: <code>{user_id}</code>\n"
        f"• Промокод: <code>{code_upper}</code> (скидка <b>-{discount_pct}%</b>)\n"
        f"• Данные заказа:{order_details}\n\n"
        f"{summary_block}"
    )
    kb_staff = _build_promo_staff_keyboard(req_id)

    for staff_id in get_staff_recipients():
        try:
            sent = await message.bot.send_message(
                chat_id=staff_id,
                text=staff_text,
                reply_markup=kb_staff,
                parse_mode="HTML",
            )
            PROMO_REQUESTS[req_id]["staff_msgs"][staff_id] = sent.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки заявки на промокод #{req_id} стафу {staff_id}: {e}")

    await state.set_state(OrderForm.payment)
    await _send_promo_return_notice(
        message.bot,
        user_id,
        "⏳ <b>Заявка на активацию промокода отправлена!</b>\n\n"
        "Промокод пока не применён. Ожидайте проверки менеджером или администратором.",
    )


async def _finalize_promo_staff_message(bot: Bot, req: dict, decision_label: str):
    req_id = req["req_id"]
    uid = req["user_id"]
    uname = req.get("username")
    user_tag = f"@{uname}" if uname else "без @username"
    code_upper = req["code"]
    disc_pct = req["discount_pct"]
    base_full_price = req.get("base_full_price", 0)
    order_details = _build_order_details_text(
        req.get("order_data") or {},
        req.get("country") or "Россия",
        req.get("service_name") or "Услуга",
        base_full_price,
    )
    summary_block = _format_user_summary_block(uid, req.get("order_data") or {})

    updated_text = (
        f"🎟 <b>Запрос активации промокода #{req_id}</b>\n"
        f"• Статус: <b>{decision_label}</b>\n"
        f"• Клиент: {user_tag} | ID: <code>{uid}</code>\n"
        f"• Промокод: <code>{code_upper}</code> (скидка <b>-{disc_pct}%</b>)\n"
        f"• Данные заказа:{order_details}\n\n"
        f"{summary_block}"
    )
    for staff_id, msg_id in (req.get("staff_msgs") or {}).items():
        try:
            await bot.edit_message_text(
                chat_id=staff_id,
                message_id=msg_id,
                text=updated_text,
                reply_markup=None,
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("promo_approve_"))
async def cb_promo_approve(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    try:
        req_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    req = PROMO_REQUESTS.get(req_id)
    if not req:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if req_id in PROMO_DECIDED_REQUESTS:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    uid = req["user_id"]
    if uid in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return

    if _get_client_paid_orders(uid) or uid in USED_PROMO_USERS or uid in USER_APPLIED_PROMOS:
        PROMO_DECIDED_REQUESTS.add(req_id)
        req["status"] = "rejected_already_used"
        await _finalize_promo_staff_message(callback.bot, req, "❌ Отменено (промокод уже использован или заказ оплачен)")
        await callback.answer("У клиента уже оплачен заказ или использован промокод", show_alert=True)
        return

    PROMO_DECIDED_REQUESTS.add(req_id)
    req["status"] = "approved"
    USED_PROMO_USERS.add(uid)
    queue_db(("promo_used_add", uid, req["code"], req["discount_pct"]))

    # Берём актуальную полную сумму заказа или счёта (без повторного дисконтирования)
    bill = USER_BILLS.get(uid) or {}
    raw_full = bill.get("original_total_price") or bill.get("total_price") or req.get("base_full_price") or 0
    if not raw_full and callback.from_user.id == uid:
        st_data = await state.get_data()
        raw_full = st_data.get("original_total_price") or st_data.get("total_price") or 0

    disc_pct = int(req["discount_pct"])
    if float(raw_full) > 0:
        discounted_price = _apply_discount_once(raw_full, disc_pct)
    else:
        discounted_price = 0

    USER_APPLIED_PROMOS[uid] = {
        "code": req["code"],
        "discount_pct": disc_pct,
        "original_price": raw_full,
        "discounted_price": discounted_price,
    }

    if uid in USER_BILLS:
        USER_BILLS[uid]["original_total_price"] = raw_full
        USER_BILLS[uid]["total_price"] = discounted_price
        USER_BILLS[uid]["promo_discount_pct"] = disc_pct
        queue_db(("bill_set", uid, dict(USER_BILLS[uid])))

    if callback.from_user.id == uid:
        await state.update_data(
            original_total_price=raw_full,
            total_price=discounted_price,
            promo_discount_pct=disc_pct,
        )

    PROBLEM_USERS.pop(uid, None)
    await _finalize_promo_staff_message(callback.bot, req, f"✅ Подтверждено (-{disc_pct}%)")

    if float(raw_full) > 0:
        price_line = (
            f"\nСумма заказа: <s>{_format_num(raw_full)}$</s> → "
            f"<b>{_format_num(discounted_price)}$</b> <b>(-{disc_pct}%)</b>"
        )
    else:
        price_line = ""

    try:
        await _send_promo_return_notice(
            callback.bot,
            uid,
            f"🎉 <b>Поздравляем! Вы получили скидку -{disc_pct}% на всю сумму заказа!</b>"
            f"{price_line}",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить клиента {uid} о подтверждении промокода: {e}")

    await callback.answer(f"✅ Промокод подтверждён (-{disc_pct}%)", show_alert=True)


@router.callback_query(F.data.startswith("promo_reject_"))
async def cb_promo_reject(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    try:
        req_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    req = PROMO_REQUESTS.get(req_id)
    if not req:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if req_id in PROMO_DECIDED_REQUESTS:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    PROMO_DECIDED_REQUESTS.add(req_id)
    req["status"] = "rejected"
    uid = req["user_id"]

    await _finalize_promo_staff_message(callback.bot, req, "❌ Отменено")

    if uid not in BANNED_USERS:
        try:
            await _send_promo_return_notice(
                callback.bot,
                uid,
                "❌ <b>Вы не выполнили условия акции для активации скидки.</b>\n\n"
                "Условия не соблюдены — напишите менеджеру (актуальный юзернейм менеджера "
                "вы всегда можете найти в нашем телеграм канале) и выполните условия, чтобы получить персональную скидку.",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить клиента {uid} об отклонении промокода: {e}")

    await callback.answer("❌ Промокод отклонён", show_alert=True)


# === КЛИЕНТСКИЕ КНОПКИ ПОД СООБЩЕНИЕМ ОТ МЕНЕДЖЕРА ===
@router.callback_query(F.data == "client_reply_manager")
async def cb_client_reply_manager(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    _touch_user_activity(user_id, callback.from_user.username)
    dlg = ACTIVE_DIALOGS.get(user_id)
    if not dlg or dlg.get("closed"):
        await callback.answer(
            "🔒 Это обращение уже закрыто.", show_alert=True
        )
        return

    await callback.answer()
    await state.set_state(ClientReplyForm.waiting_message)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🔒🏠 Закрыть обращение",
                callback_data="client_close_dialog",
                style="danger",
            )
        ]]
    )
    await callback.message.answer(
        "✍️ <b>Напишите ваш вопрос или сообщение менеджеру</b> (можно прикрепить фото):",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "client_close_dialog")
async def cb_client_close_dialog(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer("🔒 Обращение закрыто")
    except Exception:
        pass
    user_id = callback.from_user.id
    _close_dialog_state(user_id)
    for t in TICKETS.values():
        if t.get("user_id") == user_id and t.get("status") != "🔒 Закрыто":
            t["status"] = "🔒 Закрыто"
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await show_main_menu(callback, state)


@router.message(ClientReplyForm.waiting_message, F.text | F.photo)
async def process_client_reply_to_manager(message: Message, state: FSMContext):
    user = message.from_user
    _touch_user_activity(user.id, user.username)
    dlg = ACTIVE_DIALOGS.get(user.id)
    if not dlg or dlg.get("closed"):
        await state.clear()
        await show_main_menu(message, state)
        return

    _arm_dialog_timer(message.bot, user.id, dlg.get("last_staff_id"))
    user_tag = f"@{user.username}" if user.username else "без @username"
    photo_id = message.photo[-1].file_id if message.photo else None
    client_text = (message.caption or message.text or "").strip() or "(без текста, фото)"

    prof_data = {
        "service_name": USER_PROFILES.get(user.id, {}).get("last_service"),
        "country": USER_LAST_COUNTRY.get(user.id),
    }
    body = (
        f"• От клиента: {user_tag} (ID: <code>{user.id}</code>)\n"
        f"• Сообщение: {client_text}"
    )
    await _send_staff_ticket_menu(
        bot=message.bot,
        user_id=user.id,
        kind="send_reply",
        header_title="↩️ <b>Ответ клиента по обращению</b>",
        body_text=body,
        extra_data=prof_data,
        photo_id=photo_id,
    )

    await state.clear()
    await message.answer(
        "✅ <b>Сообщение отправлено менеджеру!</b> Вы можете нажать кнопку под прошлым сообщением, чтобы задать еще вопрос, или закрыть обращение.",
        parse_mode="HTML",
    )


# === ОБРАБОТЧИКИ МЕНЮШЕК ОБРАЩЕНИЙ И ЗАКАЗОВ У МЕНЕДЖЕРА / АДМИНА ===
@router.callback_query(F.data == "staff_cancel_mode")
async def cb_staff_cancel_mode(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await callback.answer("❌ Режим ввода отменен")
    try:
        await callback.message.edit_text("❌ Режим ввода отменен.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("tkt_noop_"))
async def cb_ticket_noop(callback: CallbackQuery):
    await callback.answer("🔒 Обращение уже закрыто", show_alert=False)


async def _close_ticket_by_id(bot: Bot, tid: int, target_user_id: int | None = None):
    ticket = TICKETS.get(tid)
    uid = target_user_id or (ticket.get("user_id") if ticket else None)
    if ticket:
        ticket["status"] = "🔒 Закрыто"
        uid = uid or ticket.get("user_id")
    if uid:
        _close_dialog_state(uid)
        PROBLEM_USERS.pop(uid, None)

    closed_kb = _build_ticket_keyboard(tid, user_id=uid or 0, kind=(ticket.get("kind") if ticket else "support"), closed=True)
    if ticket:
        for staff_id, msg_id in ticket.get("staff_msgs", {}).items():
            try:
                await bot.edit_message_reply_markup(
                    chat_id=staff_id,
                    message_id=msg_id,
                    reply_markup=closed_kb,
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("tkt_close_"))
async def cb_ticket_close(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return

    await _close_ticket_by_id(callback.bot, tid, ticket.get("user_id"))
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_build_ticket_keyboard(tid, user_id=ticket["user_id"], kind=ticket["kind"], closed=True)
        )
    except Exception:
        pass
    await callback.answer("🔒 Обращение закрыто")


@router.callback_query(F.data.startswith("order_close_"))
async def cb_order_close_dialog(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    _close_dialog_state(target_user_id)
    PROBLEM_USERS.pop(target_user_id, None)
    for t in TICKETS.values():
        if t.get("user_id") == target_user_id:
            t["status"] = "🔒 Закрыто"
    await callback.answer("🔒 Обращение / диалог закрыт")


async def _enter_staff_mode(
    callback: CallbackQuery,
    state: FSMContext,
    fsm_state: State,
    mode_key: str,
    target_user_id: int,
    tid: int | None = None,
):
    await callback.answer()
    await state.set_state(fsm_state)
    await state.update_data(reply_target_user_id=target_user_id, reply_ticket_id=tid)
    await callback.message.answer(
        _build_staff_action_guide(mode_key, target_user_id),
        reply_markup=_build_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("tkt_reply_"))
async def cb_ticket_reply(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    if ticket.get("status") == "🔒 Закрыто":
        await callback.answer("🔒 Обращение уже закрыто", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_reply, "reply", ticket["user_id"], tid=tid)


@router.callback_query(F.data.startswith("tkt_bill_"))
async def cb_ticket_bill(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_bill, "bill", ticket["user_id"], tid=tid)


@router.callback_query(F.data.startswith("tkt_replybill_"))
async def cb_ticket_reply_and_bill(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_reply_and_bill, "replybill", ticket["user_id"], tid=tid)


@router.callback_query(F.data.startswith("tkt_work_"))
async def cb_ticket_set_work(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    target_user_id = ticket["user_id"]
    if target_user_id in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return
    await _set_order_status_and_notify(callback.bot, target_user_id, "⚙️ В работе")
    await callback.answer("⚙️ Статус обновлен: В работе", show_alert=True)


@router.callback_query(F.data.startswith("tkt_done_"))
async def cb_ticket_set_done(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        tid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    ticket = TICKETS.get(tid)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    target_user_id = ticket["user_id"]
    if target_user_id in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return
    await _set_order_status_and_notify(callback.bot, target_user_id, "✅ Выполнен")
    await callback.answer("✅ Статус обновлен: Выполнен", show_alert=True)


@router.callback_query(F.data.startswith("order_msg_"))
async def cb_order_message_client(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_reply, "reply", target_user_id, tid=None)


@router.callback_query(F.data.startswith("order_bill_"))
async def cb_order_bill_client(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_bill, "bill", target_user_id, tid=None)


@router.callback_query(F.data.startswith("order_replybill_"))
async def cb_order_reply_and_bill_client(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    await _enter_staff_mode(callback, state, StaffDirectForm.waiting_reply_and_bill, "replybill", target_user_id, tid=None)


@router.callback_query(F.data.startswith("order_work_"))
async def cb_order_set_in_work(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    parts = callback.data.split("_")
    try:
        target_user_id = int(parts[2])
        order_idx = int(parts[3]) if len(parts) > 3 else None
    except (IndexError, ValueError):
        await callback.answer("Ошибка", show_alert=True)
        return

    if target_user_id in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return

    await _set_order_status_and_notify(callback.bot, target_user_id, "⚙️ В работе", order_idx=order_idx)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=_build_paid_order_keyboard(
                target_user_id, order_index=order_idx, in_work=True, is_done=False
            )
        )
    except Exception:
        pass

    await callback.answer("⚙️ Статус заказа обновлен: В работе", show_alert=True)


@router.callback_query(F.data.startswith("order_done_"))
async def cb_order_set_done(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    parts = callback.data.split("_")
    try:
        target_user_id = int(parts[2])
        order_idx = int(parts[3]) if len(parts) > 3 else None
    except (IndexError, ValueError):
        await callback.answer("Ошибка", show_alert=True)
        return

    if target_user_id in BANNED_USERS:
        await callback.answer("Пользователь заблокирован", show_alert=True)
        return

    await _set_order_status_and_notify(callback.bot, target_user_id, "✅ Выполнен", order_idx=order_idx)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=_build_paid_order_keyboard(
                target_user_id, order_index=order_idx, in_work=False, is_done=True
            )
        )
    except Exception:
        pass

    await callback.answer("✅ Статус заказа обновлен: Выполнен", show_alert=True)


@router.message(StaffDirectForm.waiting_reply, F.text)
async def process_staff_direct_reply(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return
    text_to_send = message.text.strip()
    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        forbidden = find_forbidden_requisites_for_manager(text_to_send)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    data = await state.get_data()
    target_user_id = data.get("reply_target_user_id")
    tid = data.get("reply_ticket_id")
    await state.clear()

    if not target_user_id:
        await message.reply("❌ Не найден ID клиента для ответа.")
        return
    if int(target_user_id) in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    ticket = TICKETS.get(tid) if tid else None
    kind = ticket.get("kind") if ticket else ""
    prefix = (
        "💬 <b>Ответ от менеджера по вашему заказу:</b>"
        if kind in ("special_order", "payment_help", "unpaid_checkout")
        else "💬 <b>Ответ от службы поддержки:</b>"
    )
    ok, err_text = await _deliver_send_message_to_client(
        message.bot,
        target_user_id=int(target_user_id),
        text_to_send=text_to_send,
        prefix=prefix,
        staff_id=message.from_user.id,
    )
    if ok:
        await message.reply(
            f"✅ Ответ отправлен клиенту <code>{target_user_id}</code>.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"❌ Не удалось отправить ответ: {err_text}")


@router.message(StaffDirectForm.waiting_bill, F.text)
async def process_staff_direct_bill(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return
    raw_input = message.text.strip()
    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        forbidden = find_forbidden_requisites_for_manager(raw_input)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    service_name, parsed = _parse_bill_input(raw_input, default_service="Особый заказ")
    if service_name is None:
        await message.reply(parsed, parse_mode="HTML")
        return

    data = await state.get_data()
    target_user_id = data.get("reply_target_user_id")
    await state.clear()

    if not target_user_id:
        await message.reply("❌ Не найден ID клиента для выставления счета.")
        return
    if int(target_user_id) in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    _, reply_text = await _issue_bill_to_client(
        bot=message.bot,
        caller_id=message.from_user.id,
        caller_state=state,
        target_user_id=int(target_user_id),
        service_name=service_name,
        amount_usd=parsed,
    )
    await message.reply(reply_text, parse_mode="HTML")


@router.message(StaffDirectForm.waiting_reply_and_bill, F.text)
async def process_staff_direct_reply_and_bill(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return
    raw_input = message.text.strip()
    if "|" not in raw_input:
        await message.reply(
            "❌ Используйте формат: <code>Текст сообщения | услуга сумма$</code>\n"
            "Пример: <code>Расчет готов, можно оплачивать. | особый заказ 220$</code>",
            parse_mode="HTML",
        )
        return

    msg_part, bill_part = [p.strip() for p in raw_input.split("|", 1)]
    if not msg_part:
        await message.reply("❌ Укажите текст сообщения перед символом <code>|</code>.", parse_mode="HTML")
        return

    if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
        forbidden = find_forbidden_requisites_for_manager(raw_input)
        if forbidden:
            await message.reply(
                f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
            )
            return

    service_name, parsed = _parse_bill_input(bill_part, default_service="Особый заказ")
    if service_name is None:
        await message.reply(parsed, parse_mode="HTML")
        return

    data = await state.get_data()
    target_user_id = data.get("reply_target_user_id")
    await state.clear()

    if not target_user_id:
        await message.reply("❌ Не найден ID клиента.")
        return
    if int(target_user_id) in BANNED_USERS:
        await message.reply("❌ Пользователь заблокирован.")
        return

    ok_msg, err_msg = await _deliver_send_message_to_client(
        message.bot,
        target_user_id=int(target_user_id),
        text_to_send=msg_part,
        prefix="💬 <b>Сообщение от менеджера:</b>",
        staff_id=message.from_user.id,
    )
    if not ok_msg:
        await message.reply(f"❌ Не удалось отправить сообщение: {err_msg}")
        return

    _, bill_reply = await _issue_bill_to_client(
        bot=message.bot,
        caller_id=message.from_user.id,
        caller_state=state,
        target_user_id=int(target_user_id),
        service_name=service_name,
        amount_usd=parsed,
    )
    await message.reply(
        f"✅ Сообщение и счет отправлены клиенту <code>{target_user_id}</code>.\n{bill_reply}",
        parse_mode="HTML",
    )


@router.message(F.reply_to_message)
async def admin_reply_handler(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        return

    current_staff_state = await state.get_state()
    if current_staff_state in (
        StaffDirectForm.waiting_reply.state,
        StaffDirectForm.waiting_bill.state,
        StaffDirectForm.waiting_reply_and_bill.state,
    ):
        await state.clear()

    reply_msg = message.reply_to_message
    text_source = (reply_msg.text or "") + "\n" + (reply_msg.caption or "")

    match = re.search(r"ID:\s*<code>(\d+)</code>|<code>(\d+)</code>", text_source)
    if not match:
        match = re.search(r"(?:ID:|\b)(\d{6,})", text_source)

    if match:
        target_user_id = int(
            match.group(1)
            if match.lastindex == 1
            else (match.group(1) or match.group(2))
        )
        if target_user_id in BANNED_USERS:
            await message.reply("❌ Пользователь заблокирован.")
            return
        if is_manager(message.from_user.id) and not is_admin(message.from_user.id):
            forbidden = find_forbidden_requisites_for_manager(message.text or "")
            if forbidden:
                await message.reply(
                    f"🚫 Отправка запрещена: обнаружен {forbidden}. Менеджеру нельзя передавать ссылки, адреса криптокошельков, номера карт и номера телефонов."
                )
                return

        text_lower = text_source.lower()
        is_special = "особый заказ" in text_lower or "заказ" in text_lower
        prefix = (
            "💬 <b>Ответ от менеджера по вашему заказу:</b>"
            if is_special
            else "💬 <b>Ответ от службы поддержки:</b>"
        )

        ok, err_text = await _deliver_send_message_to_client(
            message.bot,
            target_user_id=target_user_id,
            text_to_send=(message.text or "").strip(),
            prefix=prefix,
            staff_id=message.from_user.id,
        )
        if ok:
            await message.reply("✅ Ответ успешно отправлен клиенту.")
        else:
            await message.reply(f"❌ Не удалось отправить сообщение клиенту: {err_text}")


@router.message(Command("clear_cache"))
async def cmd_clear_cache(message: Message):
    if not is_admin(message.from_user.id):
        return
    BANNER_CACHE.clear()
    await message.reply("✅ Кеш баннеров успешно очищен.")


@router.message(Command("add"))
async def cmd_add_card(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование:\n"
            "• Карта: `/add +783839293 ОзонБанк Россия`\n"
            "• Крипта: `/add TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe USDT TRC-20`",
            parse_mode="Markdown",
        )
        return

    raw_arg = args[1].strip()
    parts = raw_arg.split()

    if len(parts) >= 2:
        address = parts[0]
        net_part = " ".join(parts[1:])
        net_key = normalize_crypto_key(net_part)
        if net_key:
            CRYPTO_DETAILS.setdefault(net_key, []).append(address)
            queue_db(("crypto_add", net_key, address))
            net_name = CRYPTO_NETWORKS[net_key]["name"]
            total_in_net = len(CRYPTO_DETAILS[net_key])
            await message.reply(
                f"✅ Криптореквизиты добавлены в список ({total_in_net} шт.)!\n"
                f"🪙 <b>{net_name}</b>\n<code>{address}</code>",
                parse_mode="HTML",
            )
            return

    if len(parts) < 3:
        await message.reply("❌ Неверный формат.")
        return
    country = parts[-1]
    bank = parts[-2]
    details = " ".join(parts[:-2])
    CARD_DETAILS.append({
        "details": details,
        "bank": bank,
        "country": country,
        "frozen_until": None,
    })
    queue_db(("card_add", details, bank, country))
    await message.reply(
        f"✅ Реквизиты добавлены!\n🌍 {country} | 🏦 {bank}\n<code>{details}</code>",
        parse_mode="HTML",
    )


@router.message(Command("delete"))
async def cmd_delete_card(message: Message):
    if not is_admin(message.from_user.id):
        return
    global CARD_DETAILS
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование:\n"
            "• Карта: `/delete <реквизиты> <Банк> <Страна>`\n"
            "• Крипта: `/delete <USDT TRC-20 / USDT TON / BEP-20 / BTC>` или"
            " `/delete <адрес> <USDT TRC-20>`",
            parse_mode="Markdown",
        )
        return

    raw_arg = args[1].strip()
    net_key = normalize_crypto_key(raw_arg)
    if net_key:
        if net_key in CRYPTO_DETAILS and CRYPTO_DETAILS[net_key]:
            CRYPTO_DETAILS.pop(net_key, None)
            CRYPTO_POINTERS.pop(net_key, None)
            queue_db(("crypto_delete", net_key))
            await message.reply(
                f"✅ Все криптореквизиты для <b>{CRYPTO_NETWORKS[net_key]['name']}</b>"
                " удалены.",
                parse_mode="HTML",
            )
        else:
            await message.reply("❌ Криптореквизиты для этой сети не найдены.")
        return

    parts = raw_arg.split()
    if len(parts) >= 2:
        addr_candidate = parts[0]
        net_candidate_key = normalize_crypto_key(" ".join(parts[1:]))
        if net_candidate_key:
            addrs = CRYPTO_DETAILS.get(net_candidate_key) or []
            if addr_candidate in addrs:
                CRYPTO_DETAILS[net_candidate_key] = [
                    a for a in addrs if a != addr_candidate
                ]
                if not CRYPTO_DETAILS[net_candidate_key]:
                    CRYPTO_DETAILS.pop(net_candidate_key, None)
                    CRYPTO_POINTERS.pop(net_candidate_key, None)
                queue_db(("crypto_delete_addr", net_candidate_key, addr_candidate))
                await message.reply(
                    f"✅ Кошелек <code>{addr_candidate}</code> удален из"
                    f" <b>{CRYPTO_NETWORKS[net_candidate_key]['name']}</b>.",
                    parse_mode="HTML",
                )
            else:
                await message.reply("❌ Указанный криптокошелек не найден в списке.")
            return

    if len(parts) < 3:
        await message.reply("❌ Неверный формат.")
        return
    country = parts[-1]
    bank = parts[-2]
    details = " ".join(parts[:-2])
    initial_len = len(CARD_DETAILS)
    CARD_DETAILS = [
        c
        for c in CARD_DETAILS
        if not (
            c["details"] == details
            and c["bank"] == bank
            and c["country"].lower() == country.lower()
        )
    ]
    if len(CARD_DETAILS) < initial_len:
        queue_db(("card_delete", details, bank, country))
        await message.reply("✅ Удалено.")
    else:
        await message.reply("❌ Не найдено.")


@router.message(Command("freeze"))
async def cmd_freeze_card(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "❌ Использование: `/freeze <реквизиты> <Банк> <Страна>`",
            parse_mode="Markdown",
        )
        return
    parts = args[1].split()
    if len(parts) < 3:
        await message.reply("❌ Неверный формат.")
        return
    country = parts[-1]
    bank = parts[-2]
    details = " ".join(parts[:-2])
    found = False
    now = datetime.datetime.now()
    for card in CARD_DETAILS:
        if (
            card["details"] == details
            and card["bank"] == bank
            and card["country"].lower() == country.lower()
        ):
            frozen_until = now + datetime.timedelta(hours=24)
            card["frozen_until"] = frozen_until
            queue_db(("card_freeze", details, bank, country, frozen_until))
            found = True
            break
    if found:
        await message.reply("✅ Зафрижено на 24 часа.")
    else:
        await message.reply("❌ Не найдено.")


@router.message(Command("list"))
async def cmd_list_details(message: Message):
    if not is_admin(message.from_user.id):
        return

    now = datetime.datetime.now()
    active_cards = []
    frozen_cards = []

    for card in CARD_DETAILS:
        frozen_until = card.get("frozen_until")
        if frozen_until and frozen_until > now:
            frozen_cards.append((card, frozen_until))
        else:
            if frozen_until and frozen_until <= now:
                card["frozen_until"] = None
                queue_db((
                    "card_freeze",
                    card["details"],
                    card["bank"],
                    card["country"],
                    None,
                ))
            active_cards.append(card)

    active_crypto = []
    for net_key, addresses in CRYPTO_DETAILS.items():
        net_name = CRYPTO_NETWORKS.get(net_key, {}).get("name", net_key.upper())
        for addr in addresses:
            active_crypto.append((net_name, addr))

    active_users_count = len([u for u in KNOWN_USERS if u not in BANNED_USERS])
    active_bills_count = len([u for u in USER_BILLS if u not in BANNED_USERS])

    lines = [
        "📊 <b>Сводка реквизитов и показателей</b>",
        f"• Активных карт/переводов: <b>{len(active_cards)}</b> | Зафрижено: <b>{len(frozen_cards)}</b>",
        f"• Активных криптокошельков: <b>{len(active_crypto)}</b>",
        f"• Пользователей в базе: <b>{active_users_count}</b> (банов: <b>{len(BANNED_USERS)}</b>) | Счетов: <b>{active_bills_count}</b>\n",
        "━━━━━━━━━━━━━━",
        "💳 <b>Актуальные карты / переводы:</b>",
    ]

    if active_cards:
        for idx, card in enumerate(active_cards, 1):
            lines.append(
                f"  {idx}. 🌍 <b>{card['country']}</b> · 🏦 <b>{card['bank']}</b>\n"
                f"      └ <code>{card['details']}</code>"
            )
    else:
        lines.append("  • Нет активных реквизитов")

    lines.append("\n🪙 <b>Актуальная криптовалюта:</b>")
    if active_crypto:
        for idx, (net_name, address) in enumerate(active_crypto, 1):
            lines.append(
                f"  {idx}. <b>{net_name}</b>\n"
                f"      └ <code>{address}</code>"
            )
    else:
        lines.append("  • Нет активных криптокошельков")

    lines.append("\n❄️ <b>Зафриженные на 24 часа:</b>")
    if frozen_cards:
        for idx, (card, until_dt) in enumerate(frozen_cards, 1):
            remaining = until_dt - now
            total_minutes = max(1, int(remaining.total_seconds() // 60))
            rem_hours, rem_mins = divmod(total_minutes, 60)
            lines.append(
                f"  {idx}. 🌍 <b>{card['country']}</b> · 🏦 <b>{card['bank']}</b>\n"
                f"      └ <code>{card['details']}</code> (еще {rem_hours}ч {rem_mins}м, до {until_dt.strftime('%d.%m %H:%M')})"
            )
    else:
        lines.append("  • Нет зафриженных реквизитов")

    await message.reply("\n".join(lines), parse_mode="HTML")


# === CRYPTOBOT ИНТЕГРАЦИЯ ===
async def create_cryptobot_invoice(amount_usd: float, description: str):
    if not CRYPTO_BOT_TOKEN:
        return "", None
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(_format_num(amount_usd)),
        "description": description,
        "payload": "order_payment",
    }
    try:
        async with ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=10
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        res = data["result"]
                        return res["pay_url"], res["invoice_id"]
    except Exception as e:
        logger.error(f"Ошибка CryptoBot: {e}")
    return "", None


async def check_cryptobot_invoice_status(invoice_id: int) -> bool:
    if not CRYPTO_BOT_TOKEN or not invoice_id:
        return True
    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    params = {"invoice_ids": str(invoice_id)}
    try:
        async with ClientSession() as session:
            async with session.get(
                url, params=params, headers=headers, timeout=10
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        items = data.get("result", {}).get("items", [])
                        if items:
                            return items[0].get("status") == "paid"
    except Exception as e:
        logger.error(f"Ошибка проверки инвойса: {e}")
    return False


# === ФОНОВЫЕ ЗАДАЧИ ===
async def scheduled_broadcast(bot: Bot):
    while True:
        await asyncio.sleep(3600)
        now = datetime.datetime.now()
        banner_path = get_banner_path("special_offer.jpg")
        for user_id, last_seen_time in list(KNOWN_USERS.items()):
            if user_id in BANNED_USERS:
                continue
            ref_time = USER_LAST_BROADCAST.get(user_id, last_seen_time)
            if ref_time is None or (now - ref_time).total_seconds() >= 48 * 3600:
                if user_id in BANNED_USERS:
                    continue
                try:
                    today = datetime.date.today()
                    target_date = today + datetime.timedelta(days=3)
                    months_ru = [
                        "",
                        "января",
                        "февраля",
                        "марта",
                        "апреля",
                        "мая",
                        "июня",
                        "июля",
                        "августа",
                        "сентября",
                        "октября",
                        "ноября",
                        "декабря",
                    ]
                    text = (
                        "✨ <b>Специальное предложение</b>\n\n"
                        "Действует персональная скидка <b>20%</b> до"
                        f" <b>{target_date.day} {months_ru[target_date.month]}</b>.\n\n"
                        "Ждем вас в боте 👇"
                    )
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✨ Оформить со скидкой",
                                    callback_data="start_order",
                                    style="danger",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="🏠 На главную",
                                    callback_data="back_home",
                                    style="primary",
                                )
                            ],
                        ]
                    )

                    if "special_offer.jpg" in BANNER_CACHE:
                        photo = BANNER_CACHE["special_offer.jpg"]
                        await bot.send_photo(
                            chat_id=user_id,
                            photo=photo,
                            caption=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                    elif banner_path and os.path.exists(banner_path):
                        photo = FSInputFile(banner_path)
                        sent = await bot.send_photo(
                            chat_id=user_id,
                            photo=photo,
                            caption=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                        if sent.photo:
                            BANNER_CACHE["special_offer.jpg"] = sent.photo[-1].file_id
                    else:
                        await bot.send_message(
                            chat_id=user_id,
                            text=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )

                    USER_LAST_BROADCAST[user_id] = now
                    queue_db(("last_broadcast", user_id, now))
                except Exception as e:
                    logger.error(f"Ошибка рассылки: {e}")


async def delayed_abandoned_cart_reminder(
    bot: Bot, user_id: int, remind_at: datetime.datetime
):
    try:
        delay = max(0.0, (remind_at - datetime.datetime.now()).total_seconds())
        if delay > 0:
            await asyncio.sleep(delay)

        ABANDONED_CHECKOUTS.pop(user_id, None)
        ABANDONED_DEADLINES.pop(user_id, None)
        queue_db(("abandoned_delete", user_id))

        if user_id in BANNED_USERS:
            return
        if not _has_active_unpaid_item(user_id):
            return

        reminder_text = (
            "👋 <b>Завершить заказ со скидкой -20%?</b>\n\nВы остановились на"
            " этапе оплаты. Возвращайтесь к оформлению 👇"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🛒 Вернуться к заказу",
                        callback_data="start_order",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Поддержка",
                        callback_data="contact_admin",
                        style="danger",
                    )
                ],
            ]
        )

        banner_path = get_banner_path("abandoned_cart.jpg")
        if "abandoned_cart.jpg" in BANNER_CACHE:
            photo = BANNER_CACHE["abandoned_cart.jpg"]
            await bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=reminder_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        elif banner_path and os.path.exists(banner_path):
            photo = FSInputFile(banner_path)
            sent = await bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=reminder_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            if sent.photo:
                BANNER_CACHE["abandoned_cart.jpg"] = sent.photo[-1].file_id
        else:
            await bot.send_message(
                chat_id=user_id,
                text=reminder_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    except asyncio.CancelledError:
        pass


async def restore_abandoned_reminders(bot: Bot):
    for user_id, remind_at in list(ABANDONED_DEADLINES.items()):
        if user_id in BANNED_USERS or not _has_active_unpaid_item(user_id):
            ABANDONED_DEADLINES.pop(user_id, None)
            queue_db(("abandoned_delete", user_id))
            continue
        _schedule_abandoned_reminder(bot, user_id, remind_at)


# === ОБРАБОТЧИКИ КОМАНД ===
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await _full_reset_user_session(message.from_user.id, state, message.from_user.username)
    await show_main_menu(message, state)


@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    await _full_reset_user_session(message.from_user.id, state, message.from_user.username)
    try:
        await message.reply("🔄 Бот перезапущен, состояние очищено.")
    except Exception:
        pass
    await show_main_menu(message, state)


@router.message(Command("order"))
async def cmd_order(message: Message, state: FSMContext):
    await _show_catalog_step(message, state, record_history=True)


@router.message(Command("my_orders"))
async def cmd_my_orders_msg(message: Message, state: FSMContext):
    user_id = message.from_user.id
    _touch_user_activity(user_id, message.from_user.username)
    orders = _get_client_paid_orders(user_id)
    if not orders:
        text = (
            "📦 <b>Мои заказы</b>\n\nУ вас пока нет оплаченных или оформленных"
            " заказов."
        )
    else:
        text = "📦 <b>История ваших заказов:</b>\n\n"
        for idx, ord_data in enumerate(orders, 1):
            price_str = (
                f"{_format_num(ord_data['total_price'])}$"
                if float(ord_data["total_price"]) > 0
                else "Индивидуально"
            )
            text += (
                f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма:"
                f" {price_str}\n• Статус: {ord_data['status']}\n\n"
            )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_home", style="primary"
            )
        ]]
    )
    await render_step(message, state, "my_orders.jpg", text, keyboard)


@router.message(Command("reputation"))
async def cmd_reputation_msg(message: Message, state: FSMContext):
    _touch_user_activity(message.from_user.id, message.from_user.username)
    completed, in_progress, new_requests = get_reputation_stats()
    text = (
        "📊 <b>Репутация сервиса</b>\n\n"
        f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
        f"• В активной работе: <b>{in_progress}</b>\n"
        f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
        "<i>Конфиденциальность и безопасность гарантированы.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_home", style="primary"
            )
        ]]
    )
    await render_step(message, state, "reputation.jpg", text, keyboard)


@router.callback_query(F.data == "back_home")
async def back_home(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _cancel_abandoned_reminder(user_id)
    await show_main_menu(callback, state)


@router.callback_query(F.data == "reputation")
async def show_reputation(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    _touch_user_activity(callback.from_user.id, callback.from_user.username)
    completed, in_progress, new_requests = get_reputation_stats()
    text = (
        "📊 <b>Репутация сервиса</b>\n\n"
        f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
        f"• В активной работе: <b>{in_progress}</b>\n"
        f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
        "<i>Конфиденциальность и безопасность гарантированы.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_home", style="primary"
            )
        ]]
    )
    await render_step(callback, state, "reputation.jpg", text, keyboard)


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _touch_user_activity(user_id, callback.from_user.username)
    orders = _get_client_paid_orders(user_id)
    if not orders:
        text = (
            "📦 <b>Мои заказы</b>\n\nУ вас пока нет оплаченных или оформленных"
            " заказов."
        )
    else:
        text = "📦 <b>История ваших заказов:</b>\n\n"
        for idx, ord_data in enumerate(orders, 1):
            price_str = (
                f"{_format_num(ord_data['total_price'])}$"
                if float(ord_data["total_price"]) > 0
                else "Индивидуально"
            )
            text += (
                f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма:"
                f" {price_str}\n• Статус: {ord_data['status']}\n\n"
            )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_home", style="primary"
            )
        ]]
    )
    await render_step(callback, state, "my_orders.jpg", text, keyboard)


@router.callback_query(F.data == "contact_admin")
async def contact_admin_info(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    _touch_user_activity(callback.from_user.id, callback.from_user.username)
    await state.set_state(SupportForm.reason)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Отмена", callback_data="back_home", style="danger"
            )
        ]]
    )
    text = (
        "💬 <b>Служба поддержки</b>\n\n"
        "Опишите вашу ситуацию или задайте вопрос (оплата, статус заказа и т.д.):"
    )
    await render_step(callback, state, "support.jpg", text, keyboard)


@router.message(SupportForm.reason, F.text)
async def process_support_reason(message: Message, state: FSMContext):
    user_id = message.from_user.id
    _touch_user_activity(user_id, message.from_user.username)
    reason_text = message.text.strip()
    await state.update_data(reason=reason_text)
    data = await state.get_data()
    _record_problem_user(
        user_id=user_id,
        kind="support",
        step_label="Обращение в поддержку",
        query_text=reason_text,
        data=data,
    )
    await state.set_state(SupportForm.photo)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏩ Пропустить",
                    callback_data="skip_support_photo",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Отмена", callback_data="back_home", style="danger"
                )
            ],
        ]
    )
    text = "📸 Отправьте фото при необходимости или нажмите пропустить:"
    await render_step(message, state, "support.jpg", text, keyboard)


@router.message(SupportForm.photo, F.photo)
async def process_support_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    user = message.from_user
    _touch_user_activity(user.id, user.username)
    _record_problem_user(
        user_id=user.id,
        kind="support",
        step_label="Обращение в поддержку",
        query_text=data.get("reason") or "",
        data=data,
    )
    user_info = f"@{user.username}" if user.username else "без @username"
    body_text = (
        f"• От кого: {user_info} (ID: <code>{user.id}</code>)\n"
        f"• Вопрос: {data.get('reason')}"
    )
    await _send_staff_ticket_menu(
        bot=message.bot,
        user_id=user.id,
        kind="support",
        header_title="📩 <b>Новое обращение в поддержку</b>",
        body_text=body_text,
        extra_data=data,
        photo_id=photo_id,
    )
    text = "✅ Обращение отправлено! Специалист скоро свяжется с вами."
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(message, state, "main.jpg", text, keyboard)
    await state.clear()


@router.callback_query(SupportForm.photo, F.data == "skip_support_photo")
async def skip_support_photo(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    data = await state.get_data()
    user = callback.from_user
    _touch_user_activity(user.id, user.username)
    _record_problem_user(
        user_id=user.id,
        kind="support",
        step_label="Обращение в поддержку",
        query_text=data.get("reason") or "",
        data=data,
    )
    user_info = f"@{user.username}" if user.username else "без @username"
    body_text = (
        f"• От кого: {user_info} (ID: <code>{user.id}</code>)\n"
        f"• Вопрос: {data.get('reason')}"
    )
    await _send_staff_ticket_menu(
        bot=callback.bot,
        user_id=user.id,
        kind="support",
        header_title="📩 <b>Новое обращение в поддержку</b>",
        body_text=body_text,
        extra_data=data,
    )
    text = "✅ Обращение отправлено! Специалист скоро свяжется с вами."
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(callback, state, "main.jpg", text, keyboard)
    await state.clear()


# === КАТАЛОГ И ОФОРМЛЕНИЕ ЗАКАЗА ===
@router.callback_query(F.data == "start_order")
async def process_start_order(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _cancel_abandoned_reminder(user_id)
    await state.update_data(nav_history=["service"])
    await _show_catalog_step(callback, state, record_history=False)


@router.callback_query(OrderForm.service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    parts = callback.data.split("_")
    price = int(parts[-1])
    service_name = " ".join(parts[1:-1]).replace("_", " ")

    _clear_back_after_service_flags(callback.from_user.id)
    _track_user_step(
        callback.from_user.id,
        "catalog",
        callback.from_user.username,
        service_name=service_name,
    )
    await state.update_data(
        service_name=service_name,
        service_price=price,
        nav_history=["service"],
    )

    if service_name == "Вернуть долг":
        _record_problem_user(
            user_id=callback.from_user.id,
            kind="debt_return",
            step_label=STEP_LABELS["debt_info"],
            query_text=service_name,
            data={"service_name": service_name, "service_price": price},
        )
        await _show_debt_info_step(callback, state, record_history=True)
    elif service_name == "Особый заказ":
        _record_problem_user(
            user_id=callback.from_user.id,
            kind="special_order",
            step_label=STEP_LABELS["special_task"],
            query_text=service_name,
            data={"service_name": service_name, "service_price": price},
        )
        await _show_special_task_step(callback, state, record_history=True)
    else:
        await show_country_selection(callback, state, service_name, price, record_history=True)


async def show_country_selection(
    event: Message | CallbackQuery,
    state: FSMContext,
    service_name: str,
    price: int,
    record_history: bool = True,
):
    _track_user_step(
        event.from_user.id,
        "country",
        event.from_user.username,
        service_name=service_name,
    )
    await state.set_state(OrderForm.country)
    if record_history:
        await _push_nav_step(state, "country")
    countries = [
        ("🇷🇺 Россия", "country_Russia"),
        ("🇧🇾 Беларусь", "country_Belarus"),
        ("🇺🇦 Украина", "country_Ukraine"),
        ("🇰🇿 Казахстан", "country_Kazakhstan"),
    ]
    kb_rows = []
    for i in range(0, len(countries), 2):
        row = [
            InlineKeyboardButton(
                text=countries[i][0], callback_data=countries[i][1], style="primary"
            )
        ]
        if i + 1 < len(countries):
            row.append(
                InlineKeyboardButton(
                    text=countries[i + 1][0],
                    callback_data=countries[i + 1][1],
                    style="danger",
                )
            )
        kb_rows.append(row)
    kb_rows.append([
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data="order_back_step", style="primary"
        )
    ])

    price_str = f"{price}$" if price > 0 else "Расчет менеджера"
    text = (
        f"🎯 Выбрано: <b>{service_name}</b> ({price_str})\n\n🌍 <b>Страна"
        " вашей цели</b>\n\nВыберите страну из списка:"
    )
    await render_step(
        event,
        state,
        "country.jpg",
        text,
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.message(OrderForm.debt_info, F.text)
async def process_debt_info(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "debt_info", message.from_user.username)
    await state.update_data(debt_info=message.text.strip())
    data = await state.get_data()
    _record_problem_user(
        user_id=message.from_user.id,
        kind="debt_return",
        step_label=STEP_LABELS["debt_wallet"],
        query_text=data.get("debt_info") or "",
        data=data,
    )
    await _show_debt_wallet_step(message, state, record_history=True)


@router.message(OrderForm.debt_wallet, F.text)
async def process_debt_wallet(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "debt_wallet", message.from_user.username)
    await state.update_data(debt_wallet=message.text.strip())
    data = await state.get_data()
    _record_problem_user(
        user_id=message.from_user.id,
        kind="debt_return",
        step_label=STEP_LABELS["country"],
        query_text=data.get("debt_info") or "",
        data=data,
    )
    await show_country_selection(
        message, state, data.get("service_name"), data.get("service_price"), record_history=True
    )


@router.message(OrderForm.special_task, F.text)
async def process_special_task(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "special_task", message.from_user.username)
    await state.update_data(special_task=message.text.strip())
    data = await state.get_data()
    _record_problem_user(
        user_id=message.from_user.id,
        kind="special_order",
        step_label=STEP_LABELS["country"],
        query_text=data.get("special_task") or "",
        data=data,
    )
    await show_country_selection(
        message, state, data.get("service_name"), data.get("service_price"), record_history=True
    )


@router.callback_query(OrderForm.country, F.data.startswith("country_"))
async def process_country(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    c_code = callback.data.split("_")[1]
    names = {
        "Russia": "Россия",
        "Belarus": "Беларусь",
        "Ukraine": "Украина",
        "Kazakhstan": "Казахстан",
    }
    selected = names.get(c_code, c_code)
    USER_LAST_COUNTRY[callback.from_user.id] = selected
    queue_db(("last_country", callback.from_user.id, selected))
    curr_info = COUNTRY_CURRENCIES.get(
        selected, {"code": "USD", "symbol": "USD"}
    )
    await state.update_data(
        country=selected,
        curr_code=curr_info["code"],
        curr_symbol=curr_info["symbol"],
    )
    data = await state.get_data()
    if (data.get("service_name") or "").lower() == "особый заказ":
        _record_problem_user(
            user_id=callback.from_user.id,
            kind="special_order",
            step_label=STEP_LABELS["target_location"],
            query_text=data.get("special_task") or "",
            data=data,
        )
    elif data.get("service_name") == "Вернуть долг":
        _record_problem_user(
            user_id=callback.from_user.id,
            kind="debt_return",
            step_label=STEP_LABELS["target_location"],
            query_text=data.get("debt_info") or "",
            data=data,
        )
    await _show_target_location_step(callback, state, record_history=True)


@router.message(OrderForm.target_location, F.text)
async def process_target_location(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "target_location", message.from_user.username)
    await state.update_data(target_location=message.text.strip())
    data = await state.get_data()
    if (data.get("service_name") or "").lower() == "особый заказ":
        _record_problem_user(
            user_id=message.from_user.id,
            kind="special_order",
            step_label=STEP_LABELS["target_data"],
            query_text=data.get("special_task") or "",
            data=data,
        )
    elif data.get("service_name") == "Вернуть долг":
        _record_problem_user(
            user_id=message.from_user.id,
            kind="debt_return",
            step_label=STEP_LABELS["target_data"],
            query_text=data.get("debt_info") or "",
            data=data,
        )
    await _show_target_data_step(message, state, record_history=True)


@router.message(OrderForm.target_data, F.text)
async def process_target_data(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "target_data", message.from_user.username)
    existing_data = await state.get_data()
    existing_photos = existing_data.get("photos") or []
    await state.update_data(target_data=message.text.strip(), photos=existing_photos)
    data = await state.get_data()
    if (data.get("service_name") or "").lower() == "особый заказ":
        _record_problem_user(
            user_id=message.from_user.id,
            kind="special_order",
            step_label=STEP_LABELS["photos"],
            query_text=data.get("special_task") or "",
            data=data,
        )
    elif data.get("service_name") == "Вернуть долг":
        _record_problem_user(
            user_id=message.from_user.id,
            kind="debt_return",
            step_label=STEP_LABELS["photos"],
            query_text=data.get("debt_info") or "",
            data=data,
        )
    await _show_photos_step(message, state, record_history=True)


@router.message(OrderForm.photos, F.photo)
async def process_photo(message: Message, state: FSMContext):
    _track_user_step(message.from_user.id, "photos", message.from_user.username)
    await asyncio.sleep(0.3)

    data = await state.get_data()
    photos = list(data.get("photos", []))
    if len(photos) >= 3:
        await message.answer("⚠️ Добавлено максимальное количество фото (3 шт.).")
        return

    photo_id = message.photo[-1].file_id
    if photo_id not in photos:
        photos.append(photo_id)
        await state.update_data(photos=photos)

    await _show_photos_step(message, state, record_history=False)


@router.callback_query(OrderForm.photos, F.data == "finish_photos")
async def finish_photos(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    await _show_urgency_step(callback, state, record_history=True)


@router.callback_query(OrderForm.urgency, F.data.startswith("urg_"))
async def process_urgency(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    parts = callback.data.split("_")
    urg_price = int(parts[2])
    urg_map = {
        "fast": "⚡ За 2-3 часа",
        "tomorrow": "🕐 Завтра",
        "standard": "📅 2-5 дней",
    }
    urgency = urg_map.get(parts[1], "📅 2-5 дней")
    await state.update_data(urgency=urgency, urgency_price=urg_price)
    await _show_summary_step(callback, state, record_history=True)


@router.callback_query(F.data == "submit_special_order")
async def process_special_order_submit(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user = callback.from_user
    user_id = user.id
    data = await state.get_data()
    service_name = data.get("service_name") or "Особый заказ"

    _reset_payment_step_tracking(user_id, leave_step=True)
    _cancel_abandoned_reminder(user_id)
    _clear_back_after_service_flags(user_id)
    USER_BILLS.pop(user_id, None)
    queue_db(("bill_delete", user_id))
    _track_user_step(user_id, "paid", user.username, service_name=service_name)
    _record_problem_user(
        user_id=user_id,
        kind="special_order",
        step_label=STEP_LABELS["paid"],
        query_text=data.get("special_task") or service_name,
        data=data,
    )

    order_idx = len(USER_ORDERS.get(user_id) or [])

    user_tag = f"@{user.username}" if user.username else "без @username"
    extra_info = f"\n• Задача: {data.get('special_task', 'Не указано')}"
    if data.get("country"):
        extra_info += f"\n• Страна: {data.get('country')}"
    if data.get("target_location"):
        extra_info += f"\n• Локация: {data.get('target_location')}"
    if data.get("target_data"):
        extra_info += f"\n• Данные цели: {data.get('target_data')}"
    if data.get("urgency"):
        extra_info += f"\n• Срочность: {data.get('urgency')}"
    target_photos = data.get("photos") or []
    order_kb = _build_paid_order_keyboard(user.id, order_index=order_idx, in_work=False)

    for staff_id in get_staff_recipients():
        try:
            if target_photos:
                await callback.bot.send_photo(
                    chat_id=staff_id,
                    photo=target_photos[0],
                    caption=(
                        f"🎯 <b>Заявка на расчет стоимости (Особый заказ) от {user_tag} (ID:"
                        f" <code>{user.id}</code>)</b>{extra_info}"
                    ),
                    reply_markup=order_kb,
                    parse_mode="HTML",
                )
                for p_id in target_photos[1:]:
                    await callback.bot.send_photo(
                        chat_id=staff_id,
                        photo=p_id,
                        caption=(
                            f"📸 Доп. фото цели по особому заказу от {user_tag} (ID:"
                            f" <code>{user.id}</code>)"
                        ),
                        parse_mode="HTML",
                    )
            else:
                await callback.bot.send_message(
                    chat_id=staff_id,
                    text=(
                        f"🎯 <b>Заявка на расчет стоимости (Особый заказ) от {user_tag} (ID:"
                        f" <code>{user.id}</code>)</b>{extra_info}"
                    ),
                    reply_markup=order_kb,
                    parse_mode="HTML",
                )
        except Exception:
            pass

    text = (
        "✅ <b>Заявка отправлена менеджеру!</b>\n\nОн свяжется с вами в течение 25 минут"
        " для расчета стоимости."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(callback, state, "main.jpg", text, keyboard)
    await state.clear()


@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    await state.set_state(OrderForm.payment)
    await _push_nav_step(state, "payment")
    _track_user_step(user_id, "payment", callback.from_user.username)
    data, country, curr_code, curr_symbol, total_price, service_name = (
        await get_payment_context(user_id, state)
    )

    if service_name.strip().lower() == "особый заказ" and user_id not in USER_BILLS:
        await process_special_order_submit(callback, state)
        return

    _enter_payment_method_step(callback.bot, user_id)

    remind_at = datetime.datetime.now() + datetime.timedelta(minutes=30)
    queue_db(("abandoned_set", user_id, remind_at))
    _schedule_abandoned_reminder(callback.bot, user_id, remind_at)

    rate = await get_exchange_rate(curr_code)
    local_amount = round(float(total_price) * rate) if float(total_price) > 0 else 0
    orig_price = data.get("original_total_price") or total_price
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    has_disc = disc_pct > 0 and float(orig_price) > float(total_price or 0)

    is_crypto_only = float(total_price) > 500

    if is_crypto_only:
        if has_disc:
            price_note = (
                f"Сумма: <s>{_format_num(orig_price)} USDT</s> → "
                f"<b>{_format_num(total_price)} USDT</b> <b>(-{disc_pct}%)</b>"
            )
        else:
            price_note = f"Сумма: <b>{_format_num(total_price)} USDT</b>"
        payment_text = (
            "⏳ <b>Оплата заказа</b>\n"
            "Время на завершение: <b><u>20:00</u></b>\n\n"
            "⚠️ <b>Внимание:</b> Для сумм свыше 500$ оплата"
            " картами недоступна. Доступен CryptoBot и криптовалюта.\n\n"
            f"{price_note}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤖 CryptoBot (без комиссии)",
                        callback_data="pay_cryptobot",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🪙 Криптовалюта",
                        callback_data="crypto_expert",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🎟 Промокод",
                        callback_data="promo_code_entry",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Нужна помощь",
                        callback_data="payment_help_manager",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="order_back_step", style="primary"
                    )
                ],
            ]
        )
    else:
        bill_prefix = (
            f"🧾 <b>Счет на {service_name}</b>\n" if user_id in USER_BILLS else ""
        )
        if has_disc:
            sum_line = (
                f"Сумма: <s>{_format_num(orig_price)}$</s> → "
                f"<b>{_format_num(total_price)}$</b> <b>(-{disc_pct}%)</b> "
                f"<i>(≈ {local_amount:,} {curr_symbol})</i>"
            )
        else:
            sum_line = (
                f"Сумма: <b>{_format_num(total_price)}$</b> <i>(≈ {local_amount:,}"
                f" {curr_symbol})</i>"
            )
        payment_text = (
            "⏳ <b>Оплата заказа</b>\n"
            "Время на завершение: <b><u>20:00</u></b>\n\n"
            f"{bill_prefix}"
            f"💳 <b>Способ оплаты ({country})</b>\n"
            f"{sum_line}"
        )
        keyboard = build_payment_keyboard(country, back_callback="order_back_step")

    await render_step(callback, state, "payment.jpg", payment_text, keyboard)


@router.callback_query(F.data == "pay_card_option")
async def process_pay_card_option(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "payment_method", callback.from_user.username)
    await state.update_data(payment_method="card")
    data, country, curr_code, curr_symbol, total_price, _ = (
        await get_payment_context(user_id, state)
    )
    rate = await get_exchange_rate(curr_code)
    local_amount = round(float(total_price) * rate)
    orig_price = data.get("original_total_price") or total_price
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    has_disc = disc_pct > 0 and float(orig_price) > float(total_price or 0)

    card = get_next_card(country)
    if not card:
        text = (
            "❌ <b>Реквизиты временно недоступны</b>\n\n"
            f"В данный момент реквизиты для страны <b>{country}</b> недоступны."
            " Воспользуйтесь CryptoBot или криптовалютой 👇"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤖 CryptoBot",
                        callback_data="pay_cryptobot",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🪙 Криптовалюта",
                        callback_data="crypto_expert",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                    )
                ],
            ]
        )
        await render_step(callback, state, "payment.jpg", text, keyboard)
        return

    header_title = "Оплата картой"

    if has_disc:
        amount_line = (
            f"• Итого к оплате: <b>{local_amount:,} {curr_symbol}</b> "
            f"(<s>{_format_num(orig_price)}$</s> → <b>{_format_num(total_price)}$</b> "
            f"<b>-{disc_pct}%</b>)"
        )
    else:
        amount_line = (
            f"• Итого к оплате: <b>{local_amount:,} {curr_symbol}</b>"
            f" <i>({_format_num(total_price)}$)</i>"
        )

    text = (
        f"💳 <b>{header_title} ({country})</b>\n\n"
        f"{amount_line}\n\n"
        f"⚠️ <b>Просто совершите банковский перевод на эти реквизиты:</b>\n\n"
        f"<b>Банк:</b> {card['bank']}\n"
        f"<b>Реквизиты:</b> <code>{card['details']}</code>\n\n"
        "💡 <i>После перевода нажмите кнопку ниже для отправки чека.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Прикрепить чек",
                    callback_data="ask_for_receipt",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                )
            ],
        ]
    )
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "payment_help_manager")
async def process_payment_help_manager(callback: CallbackQuery, state: FSMContext):
    user = callback.from_user
    _touch_user_activity(user.id, user.username)
    data = await state.get_data()
    _record_problem_user(
        user_id=user.id,
        kind="payment_help",
        step_label=STEP_LABELS["payment"],
        query_text="Запрос помощи с оплатой",
        data=data,
    )
    user_tag = f"@{user.username}" if user.username else "без @username"
    body = (
        f"• От кого: {user_tag} (ID: <code>{user.id}</code>)\n"
        "• Ситуация: клиент запросил помощь с оплатой заказа."
    )
    await _send_staff_ticket_menu(
        bot=callback.bot,
        user_id=user.id,
        kind="payment_help",
        header_title="🚨 <b>Проблема / запрос помощи с оплатой</b>",
        body_text=body,
        extra_data=data,
    )
    await callback.answer(
        "✅ Менеджер уведомлен, скоро свяжется с вами.", show_alert=True
    )


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "payment_method", callback.from_user.username)
    data, _, _, _, total_price, _ = await get_payment_context(
        user_id, state
    )
    if float(total_price) <= 0:
        total_price = 100

    orig_price = data.get("original_total_price") or total_price
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    has_disc = disc_pct > 0 and float(orig_price) > float(total_price)

    pay_url, inv_id = await create_cryptobot_invoice(float(total_price), "Оплата услуг")
    if not pay_url:
        await callback.answer("❌ Сервис недоступен.", show_alert=True)
        return

    await state.update_data(invoice_id=inv_id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Оплатить счет", url=pay_url, style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Проверить оплату",
                    callback_data="cryptobot_check_success",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                )
            ],
        ]
    )
    if has_disc:
        sum_txt = (
            f"<s>{_format_num(orig_price)} USDT</s> → "
            f"<b>{_format_num(total_price)} USDT</b> <b>(-{disc_pct}%)</b>"
        )
    else:
        sum_txt = f"<b>{_format_num(total_price)} USDT</b>"

    text = (
        f"🤖 <b>CryptoBot</b>\n\nСумма: {sum_txt}\n\nПосле оплаты"
        " нажмите кнопку проверки:"
    )
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "cryptobot_check_success")
async def process_cryptobot_check(callback: CallbackQuery, state: FSMContext):
    data, country, _, _, total_price, service_desc = await get_payment_context(
        callback.from_user.id, state
    )
    inv_id = data.get("invoice_id")

    is_paid = await check_cryptobot_invoice_status(inv_id)
    if not is_paid:
        await callback.answer(
            "⏳ Платеж еще не поступил. Попробуйте через минуту.", show_alert=True
        )
        return

    try:
        await callback.answer()
    except Exception:
        pass
    user = callback.from_user
    _reset_payment_step_tracking(user.id, leave_step=True)
    _cancel_abandoned_reminder(user.id)
    _clear_back_after_service_flags(user.id)
    PROBLEM_USERS.pop(user.id, None)
    _track_user_step(user.id, "paid", user.username, service_name=service_desc)

    if user.id not in USER_ORDERS:
        USER_ORDERS[user.id] = []

    order_idx = len(USER_ORDERS[user.id])
    USER_ORDERS[user.id].append({
        "service_name": service_desc,
        "total_price": total_price,
        "status": "✅ Оплачено через CryptoBot",
    })
    queue_db((
        "order_add",
        user.id,
        service_desc,
        total_price,
        "✅ Оплачено через CryptoBot",
    ))
    USER_BILLS.pop(user.id, None)
    queue_db(("bill_delete", user.id))

    order_details = _build_order_details_text(
        data, country, service_desc, total_price
    )
    user_tag = f"@{user.username}" if user.username else "без @username"
    staff_text = (
        f"🤖 Успешная оплата CryptoBot от {user_tag} (ID:"
        f" <code>{user.id}</code>){order_details}"
    )
    target_photos = data.get("photos") or []
    order_kb = _build_paid_order_keyboard(user.id, order_index=order_idx, in_work=False)

    for staff_id in get_staff_recipients():
        try:
            await callback.bot.send_message(
                chat_id=staff_id,
                text=staff_text,
                reply_markup=order_kb,
                parse_mode="HTML",
            )
            for p_id in target_photos:
                await callback.bot.send_photo(
                    chat_id=staff_id,
                    photo=p_id,
                    caption=(
                        f"📸 Фото цели по заказу CryptoBot от {user_tag} (ID:"
                        f" <code>{user.id}</code>)"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    text = "✅ <b>Оплата подтверждена!</b>\n\nВаша заявка принята в работу."
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(callback, state, "main.jpg", text, keyboard)
    await state.clear()


@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "payment_method", callback.from_user.username)
    await state.update_data(payment_method="crypto")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 USDT (TRC-20)",
                    callback_data="pay_crypto_trc20",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💵 USDT (BEP-20)",
                    callback_data="pay_crypto_bep20",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 USDT (TON)",
                    callback_data="pay_crypto_ton",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Bitcoin (BTC)",
                    callback_data="pay_crypto_btc",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                )
            ],
        ]
    )
    text = "🪙 Выберите криптовалютную сеть для перевода:"
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data.startswith("pay_crypto_"))
async def process_pay_crypto(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "payment_method", callback.from_user.username)
    await state.update_data(payment_method="crypto")
    net_key = callback.data.split("_")[2]
    net_info = CRYPTO_NETWORKS.get(net_key)
    if not net_info:
        await callback.answer("Сеть не найдена", show_alert=True)
        return

    address = get_next_crypto(net_key)
    if not address:
        text = (
            "❌ <b>Реквизиты временно недоступны</b>\n\n"
            f"В данный момент реквизиты для сети <b>{net_info['name']}</b>"
            " недоступны, мы уже решаем эту проблему. Оплатите другим способом"
            " (например, через CryptoBot или карту) 👇"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤖 CryptoBot",
                        callback_data="pay_cryptobot",
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💳 Оплата картой",
                        callback_data="pay_card_option",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data="crypto_expert", style="primary"
                    )
                ],
            ]
        )
        await render_step(callback, state, "payment.jpg", text, keyboard)
        return

    data, _, curr_code, curr_symbol, total_price, _ = await get_payment_context(
        user_id, state
    )
    rate = await get_exchange_rate(curr_code)
    local_amount = round(float(total_price) * rate) if float(total_price) > 0 else 0
    orig_price = data.get("original_total_price") or total_price
    disc_pct = int(data.get("promo_discount_pct", 0) or 0)
    has_disc = disc_pct > 0 and float(orig_price) > float(total_price or 0)

    if float(total_price) > 0 and has_disc:
        price_str = (
            f"<s>{_format_num(orig_price)}$</s> → <b>{_format_num(total_price)}$</b> "
            f"<b>(-{disc_pct}%)</b> <i>(≈ {local_amount:,} {curr_symbol})</i>"
        )
    elif float(total_price) > 0:
        price_str = (
            f"<b>{_format_num(total_price)}$</b> <i>(≈ {local_amount:,} {curr_symbol})</i>"
        )
    else:
        price_str = "<b>Индивидуальный расчет</b>"

    text = (
        f"💳 <b>Оплата {net_info['name']}</b>\n\n"
        f"• Сумма: {price_str}\n"
        f"• Сеть: <b>{net_info['network']}</b>\n"
        f"• Реквизиты:\n<code>{address}</code>\n\n"
        "💡 <i>После перевода прикрепите фото скриншот оплаты перевода.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Прикрепить чек",
                    callback_data="ask_for_receipt",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="crypto_expert", style="primary"
                )
            ],
        ]
    )
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    user_id = callback.from_user.id
    _reset_payment_step_tracking(user_id, leave_step=True)
    _track_user_step(user_id, "receipt", callback.from_user.username)
    data = await state.get_data()
    payment_method = data.get("payment_method", "card")
    await state.set_state(OrderForm.receipt)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data="go_to_payment", style="primary"
            )
        ]]
    )
    if payment_method == "crypto":
        text = (
            "🧾 Отправьте фото скриншот оплаты перевода следующим сообщением:"
        )
    else:
        text = (
            "🧾 Отправьте фото чека или документ квитанции об оплате следующим"
            " сообщением:"
        )
    await render_step(callback, state, "payment.jpg", text, keyboard)


@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
    user = message.from_user
    _reset_payment_step_tracking(user.id, leave_step=True)
    _cancel_abandoned_reminder(user.id)
    _clear_back_after_service_flags(user.id)
    PROBLEM_USERS.pop(user.id, None)
    data, country, _, _, total_price, service_desc = await get_payment_context(
        user.id, state
    )
    _track_user_step(user.id, "paid", user.username, service_name=service_desc)

    if user.id not in USER_ORDERS:
        USER_ORDERS[user.id] = []

    order_idx = len(USER_ORDERS[user.id])
    USER_ORDERS[user.id].append({
        "service_name": service_desc,
        "total_price": total_price,
        "status": "⏳ Ожидает проверки чека",
    })
    queue_db((
        "order_add",
        user.id,
        service_desc,
        total_price,
        "⏳ Ожидает проверки чека",
    ))
    USER_BILLS.pop(user.id, None)
    queue_db(("bill_delete", user.id))

    order_details = _build_order_details_text(
        data, country, service_desc, total_price
    )
    user_tag = f"@{user.username}" if user.username else "без @username"
    receipt_caption = (
        f"🛒 Новый чек от {user_tag} (ID:"
        f" <code>{user.id}</code>){order_details}"
    )
    target_photos = data.get("photos") or []
    order_kb = _build_paid_order_keyboard(user.id, order_index=order_idx, in_work=False)

    for staff_id in get_staff_recipients():
        try:
            await message.bot.send_photo(
                chat_id=staff_id,
                photo=message.photo[-1].file_id,
                caption=receipt_caption,
                reply_markup=order_kb,
                parse_mode="HTML",
            )
            for p_id in target_photos:
                await message.bot.send_photo(
                    chat_id=staff_id,
                    photo=p_id,
                    caption=(
                        f"📸 Фото цели по чеку от {user_tag} (ID:"
                        f" <code>{user.id}</code>)"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    text = (
        "✅ Чек получен! Менеджер проверит платеж в течение 25 минут."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(message, state, "main.jpg", text, keyboard)
    await state.clear()


@router.message(OrderForm.receipt, F.document)
async def process_receipt_document(message: Message, state: FSMContext):
    data = await state.get_data()
    payment_method = data.get("payment_method", "card")
    if payment_method == "crypto":
        await message.reply(
            "⚠️ Для оплаты криптовалютой отправьте фото скриншот оплаты перевода."
        )
        return

    user = message.from_user
    _reset_payment_step_tracking(user.id, leave_step=True)
    _cancel_abandoned_reminder(user.id)
    _clear_back_after_service_flags(user.id)
    PROBLEM_USERS.pop(user.id, None)
    data, country, _, _, total_price, service_desc = await get_payment_context(
        user.id, state
    )
    _track_user_step(user.id, "paid", user.username, service_name=service_desc)

    if user.id not in USER_ORDERS:
        USER_ORDERS[user.id] = []

    order_idx = len(USER_ORDERS[user.id])
    USER_ORDERS[user.id].append({
        "service_name": service_desc,
        "total_price": total_price,
        "status": "⏳ Ожидает проверки чека",
    })
    queue_db((
        "order_add",
        user.id,
        service_desc,
        total_price,
        "⏳ Ожидает проверки чека",
    ))
    USER_BILLS.pop(user.id, None)
    queue_db(("bill_delete", user.id))

    order_details = _build_order_details_text(
        data, country, service_desc, total_price
    )
    user_tag = f"@{user.username}" if user.username else "без @username"
    receipt_caption = (
        f"🛒 Новая квитанция (документ) от {user_tag} (ID:"
        f" <code>{user.id}</code>){order_details}"
    )
    target_photos = data.get("photos") or []
    order_kb = _build_paid_order_keyboard(user.id, order_index=order_idx, in_work=False)

    for staff_id in get_staff_recipients():
        try:
            await message.bot.send_document(
                chat_id=staff_id,
                document=message.document.file_id,
                caption=receipt_caption,
                reply_markup=order_kb,
                parse_mode="HTML",
            )
            for p_id in target_photos:
                await message.bot.send_photo(
                    chat_id=staff_id,
                    photo=p_id,
                    caption=(
                        f"📸 Фото цели по чеку от {user_tag} (ID:"
                        f" <code>{user.id}</code>)"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    text = (
        "✅ Чек получен! Менеджер проверит платеж в течение 25 минут."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏠 На главную", callback_data="back_home", style="danger"
            )
        ]]
    )
    await render_step(message, state, "main.jpg", text, keyboard)
    await state.clear()


# === ЗАЩИТНЫЕ (FALLBACK) ХЕНДЛЕРЫ ОТ ЗАВИСАНИЙ И НЕВЕРНОГО ВВОДА ===
@router.message(OrderForm.debt_info)
async def fallback_order_debt_info(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте текстовое сообщение с информацией о долге и"
        " сумме."
    )


@router.message(OrderForm.debt_wallet)
async def fallback_order_debt_wallet(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте текстовое сообщение с вашим криптокошельком"
        " USDT или TON."
    )


@router.message(OrderForm.special_task)
async def fallback_order_special_task(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, опишите вашу задачу текстовым сообщением."
    )


@router.message(OrderForm.target_location)
async def fallback_order_location(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте текстовое сообщение с локацией цели (город,"
        " адрес, дом или квартиру)."
    )


@router.message(OrderForm.target_data)
async def fallback_order_data(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте текстовое сообщение с данными цели (ФИО или"
        " «неизвестно»)."
    )


@router.message(OrderForm.photos)
async def fallback_order_photos(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте фото цели или нажмите кнопку «Готово» /"
        " «Пропустить»."
    )


@router.message(OrderForm.promo_code)
async def fallback_order_promo_code(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    cmd_head = raw.split()[0].lower().split("@")[0] if raw else ""
    if cmd_head in ("/start", "/restart"):
        await _full_reset_user_session(message.from_user.id, state, message.from_user.username)
        if cmd_head == "/restart":
            try:
                await message.reply("🔄 Бот перезапущен, состояние очищено.")
            except Exception:
                pass
        await show_main_menu(message, state)
        return
    await _send_promo_return_notice(
        message.bot,
        message.from_user.id,
        "❌ Неправильный промокод, АРЕНУ не обманешь)\n\n"
        "Введите промокод <b>одним словом</b> или нажмите кнопку возврата:",
    )


@router.message(OrderForm.receipt)
async def fallback_order_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("payment_method") == "crypto":
        await message.reply(
            "⚠️ Пожалуйста, отправьте фото скриншот оплаты перевода."
        )
    else:
        await message.reply(
            "⚠️ Пожалуйста, отправьте фото чека или документ квитанции об оплате."
        )


@router.message(SupportForm.reason)
async def fallback_support_reason(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, опишите вашу ситуацию текстовым сообщением."
    )


@router.message(SupportForm.photo)
async def fallback_support_photo(message: Message):
    await message.reply(
        "⚠️ Пожалуйста, отправьте фото или нажмите кнопку «Пропустить»."
    )


# === WEB SERVER FOR RENDER / RAILWAY ===
async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    global BOT_INSTANCE
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    BOT_INSTANCE = bot
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="restart", description="🔄 Перезапуск / Сброс бота"),
            BotCommand(command="order", description="Заказать услугу"),
            BotCommand(command="my_orders", description="Мои заказы"),
            BotCommand(command="reputation", description="Репутация"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось установить команды: {e}")

    await restore_abandoned_reminders(bot)
    asyncio.create_task(start_web_server())
    asyncio.create_task(scheduled_broadcast(bot))
    await bot.delete_webhook(drop_pending_updates=True)

    while True:
        try:
            logger.info("Запуск поллинга бота...")
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"Ошибка поллинга: {e}. Переподключение через 5 секунд...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
```[cite: 1]
