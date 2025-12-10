# bot.py
import logging
import os
import sqlite3
from datetime import datetime
from urllib.parse import quote_plus
from typing import Optional, Set

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# ============ НАСТРОЙКИ ============

# Токен и ID читаем из окружения, но есть дефолтные значения
BOT_TOKEN = os.getenv("BOT_TOKEN", "8529830956:AAEg_ToFvLI5o69q5gEY5GzYzCJPESQYYFQ")  # токен от @BotFather

# Чат для руководства (группа/канал) – сюда бот будет слать все заявки и уведомления
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1003362582742"))

# Список ID админов, которые могут управлять ботом/БД
ADMIN_USER_IDS = [
    int(x) for x in os.getenv("ADMIN_USER_IDS", "1403904334").split(",") if x.strip()
]

DB_PATH = "tickets.db"

# Файл с адресами: каждая строка "Номер | Адрес"
# Пример:
# 1 | Казань, ул. Космонавтов, 4
# 2 | Казань, ул. Патриса Лумумбы, 32
STORES_FILE_PATH = "stores.txt"

# Файл с техниками: по одному ID в строке, можно с комментом через "|"
# Пример:
# 111111111 | Илья (камеры)
# 222222222 | Вася (весы)
TECHS_FILE_PATH = "techs.txt"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# Карта: номер магазина -> адрес
STORE_ADDRESS_MAP: dict[str, str] = {}

# Множество media_group_id, чтобы не дублировать ответы на альбомы
RECENT_MEDIA_GROUPS: Set[str] = set()

# Список ID техников (загружается из файла + управляется командами)
TECH_USER_IDS: Set[int] = set()


# ============ ЗАГРУЗКА АДРЕСОВ МАГАЗИНОВ ============

def load_store_addresses(path: str = STORES_FILE_PATH):
    global STORE_ADDRESS_MAP
    STORE_ADDRESS_MAP = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" not in line:
                    continue
                number, address = line.split("|", 1)
                number = number.strip()
                address = address.strip()
                if not number or not address:
                    continue
                STORE_ADDRESS_MAP[number] = address
        logging.info(f"Загружено магазинов из файла: {len(STORE_ADDRESS_MAP)}")
    except FileNotFoundError:
        logging.warning(
            f"Файл с магазинами '{path}' не найден. "
            "Проверка номеров магазинов и адреса в заявках работать не будут."
        )


# ============ ЗАГРУЗКА / СОХРАНЕНИЕ СПИСКА ТЕХНИКОВ ============

def load_tech_ids_from_file(path: str = TECHS_FILE_PATH):
    """Читает TECH_USER_IDS из файла."""
    global TECH_USER_IDS
    TECH_USER_IDS = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # поддерживаем формат "id | комментарий"
                if "|" in line:
                    left, _ = line.split("|", 1)
                else:
                    left = line
                left = left.strip()
                # игнорируем строки не с числом
                if not left.isdigit():
                    continue
                TECH_USER_IDS.add(int(left))
        logging.info(f"Загружено техников из файла: {len(TECH_USER_IDS)}")
    except FileNotFoundError:
        logging.warning(
            f"Файл с техниками '{path}' не найден. "
            "Создастся автоматически при первом добавлении техника."
        )


def save_tech_ids_to_file(path: str = TECHS_FILE_PATH):
    """Сохраняет текущее множество TECH_USER_IDS в файл (по одному ID в строке)."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            for uid in sorted(TECH_USER_IDS):
                f.write(f"{uid}\n")
        logging.info(f"Список техников сохранён в '{path}'.")
    except Exception as e:
        logging.warning(f"Не удалось сохранить список техников в файл: {e}")


# ============ БАЗА ДАННЫХ SQLITE ============


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Таблица заявок
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id      INTEGER PRIMARY KEY,
            created        TEXT,
            store          TEXT,
            sender_id      INTEGER,
            sender_name    TEXT,
            equipment      TEXT,
            description    TEXT,
            priority       TEXT,
            status         TEXT,
            executor_id    INTEGER,
            executor_name  TEXT,
            admin_msg_id   INTEGER
        );
        """
    )

    # Таблица техников
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS technicians (
            user_id      INTEGER PRIMARY KEY,
            display_name TEXT
        );
        """
    )

    # Таблица отправителей (продавцов)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS senders (
            user_id     INTEGER PRIMARY KEY,
            display_name TEXT,
            store       TEXT,
            created_at  TEXT
        );
        """
    )

    conn.commit()
    conn.close()


def get_next_ticket_id() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MAX(ticket_id) FROM tickets;")
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return row[0] + 1
    return 1001


def create_ticket_row(
    ticket_id: int,
    store: str,
    sender_id: int,
    sender_name: str,
    equipment: str,
    description: str,
    priority: str,
    status: str,
    admin_msg_id: int = 0,
):
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tickets (
            ticket_id, created, store, sender_id, sender_name,
            equipment, description, priority, status,
            executor_id, executor_name, admin_msg_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            ticket_id,
            created,
            store,
            sender_id,
            sender_name,
            equipment,
            description,
            priority,
            status,
            None,
            "",
            admin_msg_id,
        ),
    )
    conn.commit()
    conn.close()


def get_ticket_data(ticket_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ticket_id, created, store, sender_id, sender_name,
               equipment, description, priority, status,
               executor_id, executor_name, admin_msg_id
        FROM tickets
        WHERE ticket_id = ?;
        """,
        (ticket_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "ticket_id": row[0],
        "created": row[1],
        "store": row[2],
        "sender_id": row[3],
        "sender_name": row[4],
        "equipment": row[5],
        "description": row[6],
        "priority": row[7],
        "status": row[8],
        "executor_id": row[9],
        "executor_name": row[10],
        "admin_msg_id": row[11],
    }


def update_ticket(ticket_id: int, **fields):
    if not fields:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    columns = []
    values = []
    for key, value in fields.items():
        columns.append(f"{key} = ?")
        values.append(value)
    values.append(ticket_id)

    sql = f"UPDATE tickets SET {', '.join(columns)} WHERE ticket_id = ?;"
    cur.execute(sql, values)
    conn.commit()
    conn.close()


# ---- Техники ----

def set_technician_name(user_id: int, display_name: str):
    """Сохраняем/обновляем отображаемое имя техника."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO technicians (user_id, display_name)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name;
        """,
        (user_id, display_name),
    )
    conn.commit()
    conn.close()


def get_technician_name(user: types.User) -> str:
    """Возвращаем имя техника из БД, если есть, иначе имя из Telegram."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT display_name FROM technicians WHERE user_id = ?;",
        (user.id,),
    )
    row = cur.fetchone()
    conn.close()

    if row and row[0]:
        return row[0]

    return (user.full_name or "").strip() or user.username or "Исполнитель"


def get_all_technicians():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, display_name FROM technicians ORDER BY user_id ASC;"
    )
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({"user_id": r[0], "display_name": r[1] or ""})
    return result


# ---- Пользователи (отправители) ----

def get_sender_profile(user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT display_name, store, created_at FROM senders WHERE user_id = ?;",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "user_id": user_id,
        "display_name": row[0],
        "store": row[1],
        "created_at": row[2],
    }


def set_sender_profile(user_id: int, display_name: str, store: str):
    """Создаём/обновляем профиль отправителя (имя + магазин)."""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO senders (user_id, display_name, store, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = excluded.display_name,
            store        = excluded.store;
        """,
        (user_id, display_name, store, created_at),
    )
    conn.commit()
    conn.close()


def set_sender_name(user_id: int, display_name: str):
    """Обновляем только имя отправителя, магазин не трогаем."""
    profile = get_sender_profile(user_id)
    store = ""
    if profile:
        store = profile.get("store") or ""
    set_sender_profile(user_id, display_name, store)


def get_all_senders(limit: Optional[int] = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    sql = """
        SELECT user_id, display_name, store, created_at
        FROM senders
        ORDER BY COALESCE(created_at, '') DESC
    """
    params = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append(
            {
                "user_id": r[0],
                "display_name": r[1] or "",
                "store": r[2] or "",
                "created_at": r[3] or "",
            }
        )
    return result


def delete_sender(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM senders WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ============ FSM ДЛЯ СОЗДАНИЯ ЗАЯВКИ И ПРОФИЛЯ ============

class TicketForm(StatesGroup):
    equipment = State()
    description = State()
    priority = State()
    photo = State()


class UserProfile(StatesGroup):
    waiting_for_name = State()
    waiting_for_store = State()


# ============ КНОПКИ ============

CANCEL_TEXT = "❌ Отмена"
BACK_TEXT = "⬅ Назад"
NO_PHOTO_TEXT = "Продолжить без фото"

EQUIPMENT_CHOICES = [
    "Весы",
    "Видеонаблюдение",
    "Интернет",
    "Кассовое оборудование",
    "Другое",
]


def equipment_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("Весы", "Видеонаблюдение")
    kb.add("Интернет", "Кассовое оборудование")
    kb.add("Другое")
    kb.add(CANCEL_TEXT)
    return kb


def description_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(BACK_TEXT, CANCEL_TEXT)
    return kb


def priority_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("обычная", "высокая")
    kb.add(BACK_TEXT, CANCEL_TEXT)
    return kb


def photo_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(NO_PHOTO_TEXT)
    kb.add(BACK_TEXT, CANCEL_TEXT)
    return kb


def tech_inline_keyboard(ticket_id: int, sender_id: int):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "Принять", callback_data=f"take_{ticket_id}"
        ),
        types.InlineKeyboardButton(
            "Завершить", callback_data=f"done_{ticket_id}"
        ),
    )
    kb.add(
        types.InlineKeyboardButton(
            "Связаться с отправителем",
            url=f"tg://user?id={sender_id}",
        )
    )
    return kb


def admin_inline_keyboard(sender_id: int):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "Отправитель", url=f"tg://user?id={sender_id}"
        )
    )
    return kb


def user_ticket_inline_keyboard(ticket_id: int):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "Отменить заявку", callback_data=f"user_cancel_{ticket_id}"
        )
    )
    return kb


def format_ticket_text(
    ticket_id: int,
    store: str,
    sender_id: int,
    equipment: str,
    description: str,
    priority: str,
    status: str,
    sender_name: Optional[str] = None,
    executor_name: str = "",
    executor_id: Optional[int] = None,
):
    # Статус
    if status == "Создана":
        status_text = "Создана"
    elif status == "Выполняется" and executor_name:
        if executor_id:
            status_text = (
                f'Выполняется <a href="tg://user?id={executor_id}">{executor_name}</a>'
            )
        else:
            status_text = f"Выполняется {executor_name}"
    elif status == "Выполнена" and executor_name:
        if executor_id:
            status_text = (
                f'Выполнена <a href="tg://user?id={executor_id}">{executor_name}</a>'
            )
        else:
            status_text = f"Выполнена {executor_name}"
    elif status == "Аннулирована пользователем":
        status_text = "Аннулирована пользователем"
    else:
        status_text = status

    sender_label = sender_name or "Отправитель"

    # Адрес магазина + ссылки на карты
    address_line = ""
    if store and STORE_ADDRESS_MAP:
        address = STORE_ADDRESS_MAP.get(str(store).strip())
        if address:
            q = quote_plus(address)
            yandex_url = f"https://yandex.ru/maps/?text={q}"
            google_url = f"https://maps.google.com/?q={q}"
            dgis_url = f"https://2gis.ru/search/{q}"
            address_line = (
                f"<b>Адрес:</b> {address}\n"
                f"Открыть в: "
                f'<a href="{yandex_url}">Яндекс</a> | '
                f'<a href="{dgis_url}">2ГИС</a> | '
                f'<a href="{google_url}">Google</a>\n'
            )

    text = (
        f"#{ticket_id}\n"
        f"<b>Магазин:</b> {store} / "
        f'<a href="tg://user?id={sender_id}">{sender_label}</a>\n'
        f"{address_line}"
        f"<b>Оборудование:</b> {equipment}\n"
        f"<b>Описание:</b> {description}\n"
        f"<b>Срочность:</b> {priority}\n"
        f"<b>Статус:</b> {status_text}\n"
    )
    return text


# ============ СЛУЖЕБНЫЕ ПРОВЕРКИ ============

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def is_tech(user_id: int) -> bool:
    return user_id in TECH_USER_IDS


async def cancel_creation(message: types.Message, state: FSMContext):
    await state.finish()
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📝 Новая заявка"))
    await message.answer("Создание заявки отменено.", reply_markup=kb)


# ============ ХЭНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ / РЕГИСТРАЦИЯ ============

@dp.message_handler(commands=["start"], state="*")
async def cmd_start(message: types.Message, state: FSMContext):
    """Старт: регистрация имени и номера магазина (кроме техников/админов)."""
    await state.finish()
    user_id = message.from_user.id

    # Техники и админы не указывают магазин
    if is_admin(user_id) or is_tech(user_id):
        text = "Вы отмечены как техник." if is_tech(user_id) else "Вы отмечены как администратор."
        extra = ""
        if is_admin(user_id):
            extra = "\nКоманда /admin — открыть админ-панель."
        await message.answer(
            f"{text}\n"
            "Регистрация магазина вам не требуется, вы будете получать заявки от пользователей."
            f"{extra}"
        )
        return

    profile = get_sender_profile(user_id)

    # Есть и имя, и магазин – обычный старт
    if profile and profile.get("display_name") and profile.get("store"):
        name = profile["display_name"]
        store = profile["store"]
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add(types.KeyboardButton("📝 Новая заявка"))
        await message.answer(
            f"Здравствуйте, {name}!\n\n"
            f"Ваш магазин: №{store}.\n\n"
            "Это бот технической поддержки.\n"
            "Через него вы можете оставить заявку по весам, "
            "видеонаблюдению, интернету и кассовому оборудованию.\n\n"
            "Для создания новой заявки нажмите кнопку «📝 Новая заявка».",
            reply_markup=kb,
        )
        return

    # Нужна регистрация
    await UserProfile.waiting_for_name.set()
    remove_kb = types.ReplyKeyboardRemove()
    await message.answer(
        "Добро пожаловать в бота технической поддержки.\n\n"
        "Сначала давайте познакомимся.\n"
        "Пожалуйста, напишите ваше имя (как к вам обращаться).",
        reply_markup=remove_kb,
    )


@dp.message_handler(state=UserProfile.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пожалуйста, напишите ваше имя текстом.")
        return

    await state.update_data(name=name)
    await UserProfile.next()

    await message.answer(
        "Теперь укажите номер вашего магазина цифрами.\n\n"
        "Пример: <b>1</b> или <b>12</b>.\n"
        "Если вы не знаете номер — уточните у руководства.",
    )


@dp.message_handler(state=UserProfile.waiting_for_store)
async def process_store_registration(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()

    if not text.isdigit():
        await message.answer(
            "Номер магазина должен содержать только цифры.\n"
            "Попробуйте ещё раз, например: 1, 5 или 12."
        )
        return

    store = text
    # Если есть файл с магазинами — проверяем существование
    if STORE_ADDRESS_MAP and store not in STORE_ADDRESS_MAP:
        await message.answer(
            "Такой номер магазина не найден в списке.\n"
            "Проверьте номер и введите ещё раз.\n\n"
            "Если уверены, что номер верный — обратитесь к руководству или в техподдержку."
        )
        return

    data = await state.get_data()
    name = data.get("name") or "Без имени"

    user_id = message.from_user.id
    set_sender_profile(user_id, name, store)
    await state.finish()

    # Клавиатура с "Новая заявка"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📝 Новая заявка"))

    await message.answer(
        f"Готово, {name}!\n"
        f"Ваш магазин: №{store}.\n\n"
        "Теперь вы можете создавать заявки в техподдержку.\n"
        "Нажмите «📝 Новая заявка», чтобы описать проблему.",
        reply_markup=kb,
    )

    # Уведомляем админов о новой регистрации
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(
                admin_id,
                "🆕 Новая регистрация пользователя:\n"
                f"Имя: {name}\n"
                f"Магазин: №{store}\n"
                f"Telegram ID: <code>{user_id}</code>",
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить админа {admin_id} о новой регистрации: {e}")


# ============ СОЗДАНИЕ ЗАЯВКИ ============

@dp.message_handler(lambda m: m.text == "📝 Новая заявка")
async def new_ticket(message: types.Message, state: FSMContext):
    """Старт создания новой заявки."""
    user_id = message.from_user.id

    # Техникам/админам заявки не нужны
    if is_admin(user_id) or is_tech(user_id):
        await message.answer("Создание заявок доступно только для магазинов (продавцов).")
        return

    profile = get_sender_profile(user_id)
    if not profile or not profile.get("display_name") or not profile.get("store"):
        # Пользователь еще не зарегистрирован или не указан магазин
        await message.answer(
            "Сначала нужно пройти регистрацию.\n\n"
            "Нажмите /start и укажите своё имя и номер магазина."
        )
        return

    await TicketForm.equipment.set()

    kb = equipment_keyboard()
    await message.answer(
        "Что сломалось? Выберите вариант кнопкой ниже или напишите свой вариант текстом.\n\n"
        f"Можно отменить создание заявки в любой момент кнопкой «{CANCEL_TEXT}».",
        reply_markup=kb,
    )


@dp.message_handler(content_types=["text", "photo"], state=TicketForm.equipment)
async def process_equipment(message: types.Message, state: FSMContext):
    # Пользователь может по привычке сразу отправить фото
    if message.content_type == "photo":
        if message.caption:
            text = message.caption.strip()
        else:
            await message.answer(
                "Сначала нужно указать, к какому оборудованию относится проблема.\n\n"
                "Нажмите подходящую кнопку (Весы, Видеонаблюдение, Интернет, "
                "Кассовое оборудование, Другое) или напишите одним словом.\n"
                "Фото мы попросим отдельным шагом чуть позже."
            )
            return
    else:
        text = (message.text or "").strip()

    if text == CANCEL_TEXT:
        await cancel_creation(message, state)
        return

    if text in EQUIPMENT_CHOICES:
        equipment_value = text
    else:
        equipment_value = f"Другое: {text}"

    await state.update_data(equipment=equipment_value)
    await TicketForm.next()

    kb = description_keyboard()
    await message.answer(
        "Опишите проблему максимально понятным языком.\n\n"
        "Желательно указать:\n"
        "• что именно не работает;\n"
        "• на какой точке (какая касса/какие весы/какая камера);\n"
        "• с какого времени примерно проблема;\n"
        "• есть ли ошибка на экране (можно сфотографировать — фото будет следующим шагом).",
        reply_markup=kb,
    )


@dp.message_handler(content_types=["text", "photo"], state=TicketForm.description)
async def process_description(message: types.Message, state: FSMContext):
    if message.content_type == "photo":
        if message.caption:
            text = message.caption.strip()
        else:
            await message.answer(
                "На этом шаге важно описать проблему словами.\n\n"
                "Напишите, пожалуйста, коротко, что происходит: что не работает, "
                "на какой точке и с какого времени.\n"
                "Фото можно будет отправить следующим шагом отдельно."
            )
            return
    else:
        text = (message.text or "").strip()

    if text == CANCEL_TEXT:
        await cancel_creation(message, state)
        return

    if text == BACK_TEXT:
        # Назад к выбору оборудования
        await TicketForm.equipment.set()
        kb = equipment_keyboard()
        await message.answer(
            "Вы вернулись к выбору оборудования.\n"
            "Что сломалось? Выберите вариант или напишите свой.",
            reply_markup=kb,
        )
        return

    await state.update_data(description=text)
    await TicketForm.next()
    kb = priority_keyboard()
    await message.answer(
        "Укажите срочность: обычная или высокая.",
        reply_markup=kb,
    )


@dp.message_handler(state=TicketForm.priority)
async def process_priority(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()

    if raw == CANCEL_TEXT:
        await cancel_creation(message, state)
        return

    if raw == BACK_TEXT:
        # Назад к описанию
        await TicketForm.description.set()
        kb = description_keyboard()
        await message.answer(
            "Вы вернулись к описанию проблемы.\n"
            "Опишите проблему ещё раз или скорректируйте текст.",
            reply_markup=kb,
        )
        return

    text = raw.lower()
    if text not in ["обычная", "высокая"]:
        text = "обычная"
    await state.update_data(priority=text)
    await TicketForm.next()
    kb = photo_keyboard()
    await message.answer(
        "Пришлите фото/скрин проблемы (если есть).\n"
        "Например, экран с ошибкой, фото весов или камеры.\n"
        f"Если фото не нужно — нажмите «{NO_PHOTO_TEXT}».\n\n"
        f"В любой момент можно нажать «{BACK_TEXT}» или «{CANCEL_TEXT}».",
        reply_markup=kb,
    )


@dp.message_handler(content_types=["photo", "text"], state=TicketForm.photo)
async def process_photo(message: types.Message, state: FSMContext):
    # Обработка кнопок отмены/назад
    if message.text:
        text_btn = message.text.strip()
        if text_btn == CANCEL_TEXT:
            await cancel_creation(message, state)
            return
        if text_btn == BACK_TEXT:
            # Назад к срочности
            await TicketForm.priority.set()
            kb = priority_keyboard()
            await message.answer(
                "Вы вернулись к выбору срочности.\n"
                "Укажите срочность: обычная или высокая.",
                reply_markup=kb,
            )
            return

    # Если пользователь отправил альбом (несколько фото одним пакетом)
    if message.media_group_id:
        # Чтобы не спамить, реагируем только один раз на этот media_group_id
        if message.media_group_id not in RECENT_MEDIA_GROUPS:
            RECENT_MEDIA_GROUPS.add(message.media_group_id)
            await message.answer(
                "Сейчас бот принимает только одно фото в одном сообщении.\n"
                "Пожалуйста, отправьте одно ключевое фото или сделайте коллаж.",
            )
        return

    data = await state.get_data()

    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.text and (
        message.text.lower().strip() == "нет" or message.text.strip() == NO_PHOTO_TEXT
    ):
        photo_id = None
    else:
        await message.answer(
            f"Пришлите фото или нажмите «{NO_PHOTO_TEXT}», если фото не требуется.\n"
            f"Либо используйте «{BACK_TEXT}» для возврата или «{CANCEL_TEXT}» для отмены.",
        )
        return

    await state.finish()

    sender = message.from_user
    sender_id = sender.id
    profile = get_sender_profile(sender_id)

    if profile:
        store = profile.get("store") or "не указан"
        sender_name = profile.get("display_name") or "Без имени"
    else:
        store = "не указан"
        sender_name = (sender.full_name or "").strip() or sender.username or "Без имени"

    equipment = data["equipment"]
    description = data["description"]
    priority = data["priority"]

    ticket_id = get_next_ticket_id()
    status = "Создана"

    text = format_ticket_text(
        ticket_id=ticket_id,
        store=store,
        sender_id=sender_id,
        equipment=equipment,
        description=description,
        priority=priority,
        status=status,
        sender_name=sender_name,
    )

    # В чат руководства
    admin_kb = admin_inline_keyboard(sender_id)
    if photo_id:
        admin_msg = await bot.send_photo(
            ADMIN_CHAT_ID,
            photo=photo_id,
            caption=text,
            reply_markup=admin_kb,
        )
    else:
        admin_msg = await bot.send_message(
            ADMIN_CHAT_ID,
            text,
            reply_markup=admin_kb,
        )

    admin_msg_id = admin_msg.message_id

    # Техникам в ЛС
    tech_kb = tech_inline_keyboard(ticket_id, sender_id)
    for tech_id in TECH_USER_IDS:
        try:
            if photo_id:
                await bot.send_photo(
                    tech_id,
                    photo=photo_id,
                    caption=text,
                    reply_markup=tech_kb,
                )
            else:
                await bot.send_message(
                    tech_id,
                    text,
                    reply_markup=tech_kb,
                )
        except Exception as e:
            logging.warning(f"Не удалось отправить технику {tech_id}: {e}")

    # Запись в БД
    create_ticket_row(
        ticket_id=ticket_id,
        store=store,
        sender_id=sender_id,
        sender_name=sender_name,
        equipment=equipment,
        description=description,
        priority=priority,
        status=status,
        admin_msg_id=admin_msg_id,
    )

    # Клавиатура с "Новая заявка"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📝 Новая заявка"))

    await message.answer(
        f"Заявка #{ticket_id} создана.\n"
        "Если проблема решилась или заявка отправлена по ошибке, вы можете её отменить.",
        reply_markup=kb,
    )
    # Отдельным сообщением — кнопка отмены заявки
    await message.answer(
        "Чтобы отменить заявку, нажмите кнопку ниже.",
        reply_markup=user_ticket_inline_keyboard(ticket_id),
    )


# ============ CALLBACK ДЛЯ ОТМЕНЫ ЗАЯВКИ ПОЛЬЗОВАТЕЛЕМ ============

@dp.callback_query_handler(lambda c: c.data.startswith("user_cancel_"))
async def callback_user_cancel(call: types.CallbackQuery):
    user_id = call.from_user.id
    ticket_id = int(call.data.split("_")[2])

    ticket = get_ticket_data(ticket_id)
    if not ticket:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    if ticket["sender_id"] != user_id:
        await call.answer("Отменить заявку может только отправитель.", show_alert=True)
        return

    if ticket["status"] == "Выполнена":
        await call.answer("Заявка уже выполнена и не может быть отменена.", show_alert=True)
        return

    if ticket["status"] == "Аннулирована пользователем":
        await call.answer("Заявка уже аннулирована.", show_alert=True)
        return

    update_ticket(ticket_id, status="Аннулирована пользователем")

    new_text = format_ticket_text(
        ticket_id=ticket_id,
        store=ticket["store"],
        sender_id=ticket["sender_id"],
        equipment=ticket["equipment"],
        description=ticket["description"],
        priority=ticket["priority"],
        status="Аннулирована пользователем",
        sender_name=ticket["sender_name"],
        executor_name=ticket["executor_name"] or "",
        executor_id=ticket["executor_id"],
    )

    admin_chat_id = ADMIN_CHAT_ID
    admin_msg_id = ticket["admin_msg_id"]

    if admin_msg_id:
        try:
            await bot.edit_message_caption(
                chat_id=admin_chat_id,
                message_id=admin_msg_id,
                caption=new_text,
                reply_markup=admin_inline_keyboard(ticket["sender_id"]),
            )
        except Exception:
            try:
                await bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text=new_text,
                    reply_markup=admin_inline_keyboard(ticket["sender_id"]),
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logging.warning(
                    f"Не удалось обновить сообщение в чате руководства при отмене заявки: {e}"
                )

    await call.answer("Заявка аннулирована.")
    await call.message.edit_reply_markup()  # убираем кнопку отмены


# ============ CALLBACK ДЛЯ ТЕХНИКОВ ============

@dp.callback_query_handler(lambda c: c.data.startswith("take_"))
async def callback_take(call: types.CallbackQuery):
    user_id = call.from_user.id
    if not is_tech(user_id):
        await call.answer("Только для техников.", show_alert=True)
        return

    ticket_id = int(call.data.split("_")[1])
    ticket = get_ticket_data(ticket_id)
    if not ticket:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    # Если уже выполнена – не трогаем
    if ticket["status"] == "Выполнена":
        await call.answer("Заявка уже выполнена.", show_alert=True)
        return

    if ticket["status"] == "Аннулирована пользователем":
        await call.answer("Заявка аннулирована отправителем.", show_alert=True)
        return

    # Если уже кто-то выполняет
    if ticket["status"] == "Выполняется":
        if ticket["executor_id"] == user_id:
            await call.answer("Вы уже назначены исполнителем этой заявки.")
        else:
            name = ticket["executor_name"] or "другой техник"
            await call.answer(
                f"Заявка уже выполняется: {name}.", show_alert=True
            )
        return

    executor_name = get_technician_name(call.from_user)

    # Назначаем исполнителя и меняем статус
    update_ticket(
        ticket_id,
        status="Выполняется",
        executor_id=user_id,
        executor_name=executor_name,
    )

    new_text = format_ticket_text(
        ticket_id=ticket_id,
        store=ticket["store"],
        sender_id=ticket["sender_id"],
        equipment=ticket["equipment"],
        description=ticket["description"],
        priority=ticket["priority"],
        status="Выполняется",
        sender_name=ticket["sender_name"],
        executor_name=executor_name,
        executor_id=user_id,
    )

    admin_chat_id = ADMIN_CHAT_ID
    admin_msg_id = ticket["admin_msg_id"]

    # Обновляем сообщение в чате руководства
    if admin_msg_id:
        try:
            await bot.edit_message_caption(
                chat_id=admin_chat_id,
                message_id=admin_msg_id,
                caption=new_text,
                reply_markup=admin_inline_keyboard(ticket["sender_id"]),
            )
        except Exception:
            try:
                await bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text=new_text,
                    reply_markup=admin_inline_keyboard(ticket["sender_id"]),
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logging.warning(
                    f"Не удалось обновить сообщение в чате руководства: {e}"
                )

    # Уведомляем отправителя, что заявка принята
    try:
        executor_link = f'<a href="tg://user?id={user_id}">{executor_name}</a>'
        await bot.send_message(
            ticket["sender_id"],
            f"Ваша заявка #{ticket_id} принята в работу.\n"
            f"Исполнитель: {executor_link}.\n\n"
            "Если появились новые детали — можно написать ответом на это сообщение.",
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить отправителя о принятии заявки: {e}")

    await call.answer("Заявка взята в работу.")
    await call.message.reply("Вы назначены исполнителем этой заявки.")


@dp.callback_query_handler(lambda c: c.data.startswith("done_"))
async def callback_done(call: types.CallbackQuery):
    user_id = call.from_user.id
    if not is_tech(user_id):
        await call.answer("Только для техников.", show_alert=True)
        return

    ticket_id = int(call.data.split("_")[1])
    ticket = get_ticket_data(ticket_id)
    if not ticket:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    # Если ещё никто не взял заявку
    if ticket["status"] == "Создана" and not ticket["executor_id"]:
        await call.answer(
            "Сначала возьмите заявку в работу (кнопка «Принять»).",
            show_alert=True,
        )
        return

    # Если уже выполнена
    if ticket["status"] == "Выполнена":
        await call.answer("Заявка уже отмечена как выполненная.", show_alert=True)
        return

    if ticket["status"] == "Аннулирована пользователем":
        await call.answer("Заявка аннулирована отправителем.", show_alert=True)
        return

    # Разрешаем закрывать только назначенному исполнителю
    if ticket["executor_id"] and ticket["executor_id"] != user_id:
        name = ticket["executor_name"] or "другой техник"
        await call.answer(
            f"Эту заявку сейчас выполняет {name}. Только он может её завершить.",
            show_alert=True,
        )
        return

    executor_name = get_technician_name(call.from_user)

    update_ticket(
        ticket_id,
        status="Выполнена",
        executor_id=user_id,
        executor_name=executor_name,
    )

    new_text = format_ticket_text(
        ticket_id=ticket_id,
        store=ticket["store"],
        sender_id=ticket["sender_id"],
        equipment=ticket["equipment"],
        description=ticket["description"],
        priority=ticket["priority"],
        status="Выполнена",
        sender_name=ticket["sender_name"],
        executor_name=executor_name,
        executor_id=user_id,
    )

    admin_chat_id = ADMIN_CHAT_ID
    admin_msg_id = ticket["admin_msg_id"]

    if admin_msg_id:
        try:
            await bot.edit_message_caption(
                chat_id=admin_chat_id,
                message_id=admin_msg_id,
                caption=new_text,
                reply_markup=admin_inline_keyboard(ticket["sender_id"]),
            )
        except Exception:
            try:
                await bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text=new_text,
                    reply_markup=admin_inline_keyboard(ticket["sender_id"]),
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logging.warning(
                    f"Не удалось обновить сообщение в чате руководства: {e}"
                )

    # Уведомим отправителя
    try:
        executor_link = f'<a href="tg://user?id={user_id}">{executor_name}</a>'
        await bot.send_message(
            ticket["sender_id"],
            f"Ваша заявка #{ticket_id} отмечена как выполненная.\n"
            f"Исполнитель: {executor_link}.\n"
            "Если проблема осталась — создайте новую заявку или ответьте технику.",
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить отправителя: {e}")

    await call.answer("Заявка отмечена как выполненная.")
    await call.message.reply("Заявка закрыта.")


# ============ АДМИН-КОМАНДЫ / ПАНЕЛЬ ============

@dp.message_handler(commands=["admin"])
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM senders;")
    users_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM technicians;")
    tech_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM tickets;")
    tickets_total = cur.fetchone()[0]

    cur.execute(
        "SELECT status, COUNT(*) FROM tickets GROUP BY status;"
    )
    rows = cur.fetchall()
    conn.close()

    status_counts = {
        "Создана": 0,
        "Выполняется": 0,
        "Выполнена": 0,
        "Аннулирована пользователем": 0,
    }
    for status, cnt in rows:
        if status in status_counts:
            status_counts[status] = cnt

    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"Пользователей (продавцов): <b>{users_count}</b>\n"
        f"Техников в БД: <b>{tech_count}</b>\n"
        f"Техников в списке техников (TECH_USER_IDS): <b>{len(TECH_USER_IDS)}</b>\n"
        f"Заявок всего: <b>{tickets_total}</b>\n"
        f" — Создано: <b>{status_counts['Создана']}</b>\n"
        f" — В работе: <b>{status_counts['Выполняется']}</b>\n"
        f" — Выполнено: <b>{status_counts['Выполнена']}</b>\n"
        f" — Аннулировано отправителем: <b>{status_counts['Аннулирована пользователем']}</b>\n\n"
        "Команды администратора:\n"
        "• /list_users – последние регистрации пользователей\n"
        "• /list_techs – список техников\n"
        "• /addtech – добавить техника (по ответу или через ID)\n"
        "• /deltech – удалить техника из списка техников\n"
        "• /reloadtechs – перечитать список техников из файла\n"
        "• /setusername – изменить имя пользователя (по ответу)\n"
        "• /settechname – задать/изменить имя техника (по ответу)\n"
        "• /deluser – удалить пользователя из базы (по ответу)\n"
        "• /broadcast текст – разослать объявление всем пользователям\n"
        "• /wipe_db CONFIRM – <b>очистить ВСЮ базу</b> (заявки, пользователи, техники)\n"
    )
    await message.answer(text)


@dp.message_handler(commands=["list_users"])
async def cmd_list_users(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    users = get_all_senders(limit=15)
    if not users:
        await message.answer("Пользователей пока нет.")
        return

    lines = ["📋 <b>Последние регистрации пользователей:</b>"]
    for u in users:
        line = (
            f"ID: <code>{u['user_id']}</code>\n"
            f"Имя: {u['display_name'] or '—'}\n"
            f"Магазин: {u['store'] or 'не указан'}\n"
            f"Регистрация: {u['created_at'] or '—'}\n"
            "———"
        )
        lines.append(line)

    await message.answer("\n".join(lines))


@dp.message_handler(commands=["list_techs"])
async def cmd_list_techs(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    techs_db = get_all_technicians()
    if not techs_db and not TECH_USER_IDS:
        await message.answer("Техники пока не настроены.")
        return

    lines = ["🧑‍🔧 <b>Список техников:</b>"]

    # Техники, у которых есть имя в БД
    for t in techs_db:
        uid = t["user_id"]
        name = t["display_name"] or "без имени"
        mark = "✅" if uid in TECH_USER_IDS else "⚠️"
        lines.append(
            f"{mark} ID: <code>{uid}</code>\n"
            f"Имя: {name}\n"
            f"В списке техников: {'да' if uid in TECH_USER_IDS else 'нет'}\n"
            "———"
        )

    # Техники, которые в TECH_USER_IDS, но нет записи в БД
    ids_in_db = {t["user_id"] for t in techs_db}
    extra_ids = TECH_USER_IDS - ids_in_db
    if extra_ids:
        lines.append("Дополнительно в списке техников есть ID без имени:")
        for uid in sorted(extra_ids):
            lines.append(f"• <code>{uid}</code> (имя не задано, используйте /settechname по ответу)")

    await message.answer("\n".join(lines))


@dp.message_handler(commands=["setusername"])
async def cmd_setusername(message: types.Message):
    """Изменить имя пользователя (по ответу)."""
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    if not message.reply_to_message:
        await message.answer(
            "Сделайте команду ответом на сообщение пользователя.\n\n"
            "Пример:\n"
            "1) Пользователь пишет что-нибудь боту.\n"
            "2) Вы отвечаете на его сообщение командой:\n"
            "<code>/setusername Имя Фамилия</code>",
        )
        return

    args = message.get_args().strip()
    if not args:
        await message.answer(
            "Укажите имя, как отображать пользователя.\n\n"
            "Пример:\n"
            "<code>/setusername Мария</code>",
        )
        return

    target_user = message.reply_to_message.from_user
    target_id = target_user.id

    set_sender_name(target_id, args)
    await message.answer(
        f"Имя пользователя <code>{target_id}</code> изменено на: <b>{args}</b>."
    )


@dp.message_handler(commands=["settechname"])
async def cmd_settechname(message: types.Message):
    """Админ задаёт отображаемое имя технику (по ответу)."""
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    if not message.reply_to_message:
        await message.answer(
            "Сделайте команду ответом на сообщение техника.\n\n"
            "Пример:\n"
            "1) Техник пишет что-нибудь боту в ЛС.\n"
            "2) Вы отвечаете на его сообщение командой:\n"
            "<code>/settechname Илья (камеры)</code>",
        )
        return

    args = message.get_args().strip()
    if not args:
        await message.answer(
            "Укажите имя, как отображать техника.\n\n"
            "Пример:\n"
            "<code>/settechname Илья (камеры)</code>",
        )
        return

    target_user = message.reply_to_message.from_user
    target_id = target_user.id

    if target_id not in TECH_USER_IDS:
        await message.answer(
            "Этот пользователь не отмечен как техник (его ID нет в списке техников).\n"
            "Сначала добавьте его через /addtech."
        )
        return

    set_technician_name(target_id, args)
    await message.answer(
        f"Готово! Теперь техник <code>{target_id}</code> будет отображаться как: <b>{args}</b>."
    )


@dp.message_handler(commands=["addtech"])
async def cmd_addtech(message: types.Message):
    """
    Добавить техника в список TECH_USER_IDS и при желании задать ему имя.
    Варианты использования:
    1) Ответом на сообщение техника: /addtech Илья (камеры)
    2) Без ответа: /addtech 123456789 Илья (камеры)
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    target_id: Optional[int] = None
    display_name: Optional[str] = None

    args = message.get_args().strip()

    if message.reply_to_message:
        # Берём ID из ответа
        target_user = message.reply_to_message.from_user
        target_id = target_user.id
        display_name = args or (
            (target_user.full_name or "").strip() or target_user.username or ""
        )
    else:
        if not args:
            await message.answer(
                "Формат:\n"
                "• ответом на сообщение техника: <code>/addtech Илья (камеры)</code>\n"
                "• либо: <code>/addtech 123456789 Илья (камеры)</code>"
            )
            return
        parts = args.split(maxsplit=1)
        id_part = parts[0]
        if not id_part.isdigit():
            await message.answer(
                "Первым параметром должен быть numeric ID пользователя.\n"
                "Пример: <code>/addtech 123456789 Илья (камеры)</code>"
            )
            return
        target_id = int(id_part)
        display_name = parts[1] if len(parts) > 1 else None

    if target_id is None:
        await message.answer("Не удалось определить ID пользователя.")
        return

    TECH_USER_IDS.add(target_id)
    save_tech_ids_to_file()

    if display_name:
        set_technician_name(target_id, display_name)

    await message.answer(
        "Техник добавлен.\n"
        f"ID: <code>{target_id}</code>\n"
        f"Имя: <b>{display_name or 'не задано'}</b>\n"
        f"Техников в списке: <b>{len(TECH_USER_IDS)}</b>"
    )


@dp.message_handler(commands=["deltech"])
async def cmd_deltech(message: types.Message):
    """
    Удалить техника из списка TECH_USER_IDS.
    Варианты:
    1) По ответу: /deltech
    2) По ID: /deltech 123456789
    """
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    target_id: Optional[int] = None
    args = message.get_args().strip()

    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
    else:
        if not args or not args.isdigit():
            await message.answer(
                "Укажите ID техника или сделайте команду ответом на его сообщение.\n"
                "Пример: <code>/deltech 123456789</code>"
            )
            return
        target_id = int(args)

    if target_id not in TECH_USER_IDS:
        await message.answer(
            f"ID <code>{target_id}</code> не значится в списке техников."
        )
        return

    TECH_USER_IDS.remove(target_id)
    save_tech_ids_to_file()

    await message.answer(
        f"ID <code>{target_id}</code> удалён из списка техников.\n"
        "Запись в таблице имён техников (если была) сохранена."
    )


@dp.message_handler(commands=["reloadtechs"])
async def cmd_reloadtechs(message: types.Message):
    """Перечитать список техников из файла techs.txt."""
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    load_tech_ids_from_file()
    await message.answer(
        f"Список техников перечитан из файла.\n"
        f"Техников в списке: <b>{len(TECH_USER_IDS)}</b>"
    )


@dp.message_handler(commands=["deluser"])
async def cmd_deluser(message: types.Message):
    """Удалить пользователя из таблицы senders (использовать по ответу)."""
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    if not message.reply_to_message:
        await message.answer(
            "Сделайте команду ответом на сообщение пользователя, которого хотите удалить."
        )
        return

    target_user = message.reply_to_message.from_user
    target_id = target_user.id

    delete_sender(target_id)
    await message.answer(
        f"Пользователь с ID <code>{target_id}</code> удалён из базы отправителей.\n"
        "Его заявки в таблице заявок сохранены."
    )


@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(message: types.Message):
    """Рассылка объявления всем пользователям."""
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    text = message.get_args().strip()
    if not text:
        await message.answer(
            "Укажите текст объявления.\n\n"
            "Пример:\n"
            "<code>/broadcast Завтра с 9:00 до 10:00 возможны перебои в работе интернета.</code>"
        )
        return

    users = get_all_senders()
    if not users:
        await message.answer("В базе нет ни одного пользователя для рассылки.")
        return

    sent = 0
    failed = 0

    for u in users:
        try:
            await bot.send_message(
                u["user_id"],
                "📢 <b>Объявление техподдержки:</b>\n\n" + text,
            )
            sent += 1
        except Exception as e:
            logging.warning(f"Не удалось отправить объявление пользователю {u['user_id']}: {e}")
            failed += 1

    await message.answer(
        f"Рассылка завершена.\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"Не удалось отправить: <b>{failed}</b>"
    )


@dp.message_handler(commands=["wipe_db"])
async def cmd_wipe_db(message: types.Message):
    """Полная очистка БД (заявки, пользователи, техники). Требует подтверждения."""
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return

    args = message.get_args().strip()
    if args != "CONFIRM":
        await message.answer(
            "⚠️ <b>ВНИМАНИЕ!</b>\n\n"
            "Команда /wipe_db полностью очищает таблицы заявок, пользователей и техников.\n"
            "Это действие необратимо.\n\n"
            "Если вы уверены, выполните:\n"
            "<code>/wipe_db CONFIRM</code>"
        )
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM tickets;")
    cur.execute("DELETE FROM senders;")
    cur.execute("DELETE FROM technicians;")
    conn.commit()
    conn.close()

    await message.answer("База данных очищена. Все заявки, пользователи и техники удалены.")


# ============ ЗАПУСК ============

if __name__ == "__main__":
    init_db()
    load_store_addresses()
    load_tech_ids_from_file()
    executor.start_polling(dp, skip_updates=True)
