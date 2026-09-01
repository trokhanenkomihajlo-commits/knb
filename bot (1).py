import asyncio
import html
import logging
import os
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токен бота
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Сколько секунд даём игроку на ход
MOVE_TIMEOUT_SECONDS = 60
MOVE_TIMER_UPDATE_INTERVAL = 15
PREGAME_TIMEOUT_SECONDS = 30

# Только этот Telegram user_id может использовать админ-команды
OWNER_ID = 1033222354
OFFICIAL_CHAT_ID = -1002449493506

SCHEDULED_CHECK_INTERVAL = 60
SCHEDULED_REMINDER_INTERVAL = 5 * 60
SCHEDULED_FORFEIT_CEILING = 15 * 60
PLAYOFF_DUELS_TO_WIN = 3

MAX_DODGES_PER_PLAYER = 3
DB_PATH = os.environ.get("DB_PATH", "bot.db")

known_chats: set[int] = set()

BOT: Bot | None = None
router = Router()


# ---------- Вспомогательные функции времени (MSK / UTC+3) ----------
def get_msk_tz() -> timezone:
    return timezone(timedelta(hours=3))

def get_now_msk() -> datetime:
    return datetime.now(timezone.utc).astimezone(get_msk_tz())


# ---------- Хранилище (SQLite) ----------
def db_connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def db_init() -> None:
    conn = db_connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS player_stats (
            user_id INTEGER PRIMARY KEY,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            label TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS known_chats (
            chat_id INTEGER PRIMARY KEY
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            overtime_enabled INTEGER NOT NULL DEFAULT 1,
            well_enabled INTEGER NOT NULL DEFAULT 0
        )"""
    )
    
    try:
        conn.execute("ALTER TABLE chat_settings ADD COLUMN well_enabled INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass 

    # --- ДОБАВЛЕНИЕ СТОЛБЦОВ ДЛЯ FACEIT СИСТЕМЫ ---
    try:
        conn.execute("ALTER TABLE player_stats ADD COLUMN elo INTEGER NOT NULL DEFAULT 1000")
    except sqlite3.OperationalError:
        pass
    
    try:
        conn.execute("ALTER TABLE player_stats ADD COLUMN matches_played INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def db_load_into_memory() -> None:
    conn = db_connect()
    for user_id, wins, losses, label, elo, matches_played in conn.execute("SELECT user_id, wins, losses, label, elo, matches_played FROM player_stats"):
        player_stats[user_id] = PlayerStats(wins=wins, losses=losses, label=label, elo=elo, matches_played=matches_played)
        
    for (chat_id,) in conn.execute("SELECT chat_id FROM known_chats"):
        known_chats.add(chat_id)
    
    for chat_id, overtime, well in conn.execute("SELECT chat_id, overtime_enabled, well_enabled FROM chat_settings"):
        chat_overtime_settings[chat_id] = bool(overtime)
        chat_well_settings[chat_id] = bool(well)

    conn.close()
    logger.info(f"Загружено из базы: {len(player_stats)} игроков, {len(known_chats)} чатов.")


def db_save_player_stats(user_id: int, stats: "PlayerStats") -> None:
    conn = db_connect()
    conn.execute(
        "INSERT INTO player_stats (user_id, wins, losses, label, elo, matches_played) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET wins=excluded.wins, losses=excluded.losses, label=excluded.label, elo=excluded.elo, matches_played=excluded.matches_played",
        (user_id, stats.wins, stats.losses, stats.label, stats.elo, stats.matches_played),
    )
    conn.commit()
    conn.close()


def db_save_known_chat(chat_id: int) -> None:
    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()


def db_save_chat_settings(chat_id: int, overtime_enabled: bool, well_enabled: bool) -> None:
    conn = db_connect()
    conn.execute(
        "INSERT INTO chat_settings (chat_id, overtime_enabled, well_enabled) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET overtime_enabled=excluded.overtime_enabled, well_enabled=excluded.well_enabled",
        (chat_id, 1 if overtime_enabled else 0, 1 if well_enabled else 0)
    )
    conn.commit()
    conn.close()


@router.message.outer_middleware()
async def remember_chat_middleware(handler, message: Message, data):
    if message.chat.id not in known_chats:
        known_chats.add(message.chat.id)
        await asyncio.to_thread(db_save_known_chat, message.chat.id)
    return await handler(message, data)


# ---------- Игровые модели ----------
class Move(str, Enum):
    rock = "rock"
    scissors = "scissors"
    paper = "paper"
    well = "well"

MOVE_EMOJI = {
    Move.rock: "🪨",
    Move.scissors: "✂️",
    Move.paper: "🧻",
    Move.well: "🕳",
}
MOVE_NAME = {
    Move.rock: "Камень",
    Move.scissors: "Ножницы",
    Move.paper: "Бумага",
    Move.well: "Колодец",
}
WIN_RULES = {
    Move.rock: [Move.scissors],
    Move.scissors: [Move.paper],
    Move.paper: [Move.rock, Move.well],
    Move.well: [Move.rock, Move.scissors],
}
EMOJI_DIGITS = {
    "0": "0️⃣", "1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣",
    "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣",
}

def emoji_number(n: int) -> str:
    return "".join(EMOJI_DIGITS[d] for d in str(n))

class GameStatus(str, Enum):
    choosing_rounds = "choosing_rounds"
    choosing_format = "choosing_format"
    waiting_join = "waiting_join"
    playing = "playing"
    between_duels = "between_duels"
    paused = "paused"
    finished = "finished"

@dataclass
class Player:
    user_id: int
    name: str
    username: str | None = None

    def label(self) -> str:
        return f"@{html.escape(self.username)}" if self.username else html.escape(self.name)

@dataclass
class RoundResult:
    move1: Move
    move2: Move
    winner_id: int | None

@dataclass
class DuelSummary:
    duel_number: int
    winner_id: int
    score_p1: int
    score_p2: int
    by_timeout: bool = False
    rounds: list[RoundResult] = field(default_factory=list)

@dataclass
class Game:
    chat_id: int
    creator: Player
    target_score: int = 3
    opponent: Player | None = None
    status: GameStatus = GameStatus.waiting_join
    is_faceit: bool = False
    round_number: int = 1
    scores: dict[int, int] = field(default_factory=dict)
    choices: dict[int, Move] = field(default_factory=dict)
    history: list[RoundResult] = field(default_factory=list)
    turn_order: list[int] = field(default_factory=list)
    message_id: int | None = None
    timeout_task: "asyncio.Task | None" = field(default=None, compare=False, repr=False)
    duel_number: int = 1
    duel_wins: dict[int, int] = field(default_factory=dict)
    duel_history: list[DuelSummary] = field(default_factory=list)
    duels_to_win: int = 1
    ready_for_next: set[int] = field(default_factory=set)
    last_duel_winner_id: int | None = None
    pause_timeout_uid: int | None = None
    resume_ready: set[int] = field(default_factory=set)
    scheduled_match_id: int | None = None
    dodges: dict[int, int] = field(default_factory=dict)

    def players(self) -> list[Player]:
        return [p for p in (self.creator, self.opponent) if p]

    def other(self, user_id: int) -> Player | None:
        if self.creator.user_id == user_id:
            return self.opponent
        if self.opponent and self.opponent.user_id == user_id:
            return self.creator
        return None

active_games: dict[int, Game] = {}
finished_games: dict[int, Game] = {}

@dataclass
class PlayerStats:
    wins: int = 0
    losses: int = 0
    label: str = ""
    elo: int = 1000
    matches_played: int = 0

player_stats: dict[int, PlayerStats] = {}

chat_overtime_settings: dict[int, bool] = {}
chat_well_settings: dict[int, bool] = {}

def is_overtime_enabled(chat_id: int) -> bool:
    return chat_overtime_settings.get(chat_id, True)

def is_well_enabled(chat_id: int) -> bool:
    return chat_well_settings.get(chat_id, False)


@dataclass
class ScheduledMatch:
    id: int
    chat_id: int
    round_name: str
    player1_username: str
    player2_username: str
    scheduled_time: datetime
    duels_to_win: int = PLAYOFF_DUELS_TO_WIN
    status: str = "pending"
    message_id: int | None = None
    called_at: datetime | None = None
    last_reminder_at: datetime | None = None
    ready_usernames: set[str] = field(default_factory=set)
    player1_obj: Player | None = None
    player2_obj: Player | None = None

scheduled_matches: dict[int, ScheduledMatch] = {}
_next_scheduled_id = 1


# ---------- Callback data ----------
class RoundsCB(CallbackData, prefix="rounds"):
    target: int

class JoinCB(CallbackData, prefix="join"):
    pass

class MoveCB(CallbackData, prefix="move"):
    move: Move

class CancelCB(CallbackData, prefix="cancel"):
    pass

class LeaveCB(CallbackData, prefix="leave"):
    pass

class FormatCB(CallbackData, prefix="format"):
    duels_to_win: int

class ReadyCB(CallbackData, prefix="ready"):
    pass

class HistoryCB(CallbackData, prefix="history"):
    pass

class ResumeCB(CallbackData, prefix="resume"):
    pass

class ScheduledReadyCB(CallbackData, prefix="sched_ready"):
    match_id: int

class ToggleOvertimeCB(CallbackData, prefix="toggle_ot"):
    pass

class ToggleWellCB(CallbackData, prefix="toggle_well"):
    pass


# ---------- Клавиатуры ----------
def rounds_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for n in (1, 2, 3, 4, 5):
        builder.button(text=f"До {n} побед", callback_data=RoundsCB(target=n))
    builder.button(text="❌ Отменить", callback_data=CancelCB())
    builder.adjust(3, 2, 1)
    return builder.as_markup()

def join_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=JoinCB())
    builder.adjust(1)
    return builder.as_markup()

def leave_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚪 Покинуть игру", callback_data=LeaveCB())
    builder.adjust(1)
    return builder.as_markup()

def moves_keyboard(is_tournament: bool, well_enabled: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for move in Move:
        if move == Move.well and not well_enabled:
            continue
        builder.button(text=f"{MOVE_EMOJI[move]} {MOVE_NAME[move]}", callback_data=MoveCB(move=move))
    
    if is_tournament:
        builder.button(text="📜 История матча", callback_data=HistoryCB())
        
    if well_enabled:
        if is_tournament:
            builder.adjust(2, 2, 1)
        else:
            builder.adjust(2, 2)
    else:
        if is_tournament:
            builder.adjust(3, 1)
        else:
            builder.adjust(3)
            
    return builder.as_markup()

def finished_keyboard(is_tournament: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if is_tournament:
        builder.button(text="📜 История матча", callback_data=HistoryCB())
    builder.button(text="KNB MAJOR🔥", url="https://t.me/knbtour")
    builder.adjust(1)
    return builder.as_markup()

def format_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Bo3 (до 2 побед)", callback_data=FormatCB(duels_to_win=2))
    builder.button(text="Bo5 (до 3 побед)", callback_data=FormatCB(duels_to_win=3))
    builder.adjust(1)
    return builder.as_markup()

def ready_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готов(а) начать", callback_data=ReadyCB())
    builder.button(text="📜 История матча", callback_data=HistoryCB())
    builder.adjust(1)
    return builder.as_markup()

def resume_keyboard(is_tournament: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Возобновить игру", callback_data=ResumeCB())
    builder.button(text="🚪 Покинуть игру", callback_data=LeaveCB())
    if is_tournament:
        builder.button(text="📜 История матча", callback_data=HistoryCB())
    builder.adjust(1)
    return builder.as_markup()

def scheduled_ready_keyboard(match_id: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готов(а)", callback_data=ScheduledReadyCB(match_id=match_id))
    return builder.as_markup()

def settings_keyboard(chat_id: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    ot_status = "✅ Включены" if is_overtime_enabled(chat_id) else "❌ Выключены"
    builder.button(text=f"Допы при 4:4: {ot_status}", callback_data=ToggleOvertimeCB())
    
    well_status = "✅ Включен" if is_well_enabled(chat_id) else "❌ Выключен"
    builder.button(text=f"🕳 Колодец: {well_status}", callback_data=ToggleWellCB())
    
    builder.adjust(1)
    return builder.as_markup()


# ---------- Логика FACEIT (Статистика и ТОП) ----------
def get_faceit_level(elo: int) -> int:
    if elo < 800: return 1
    elif elo < 950: return 2
    elif elo < 1100: return 3
    elif elo < 1250: return 4
    elif elo < 1400: return 5
    elif elo < 1550: return 6
    elif elo < 1700: return 7
    elif elo < 1850: return 8
    elif elo < 2000: return 9
    else: return 10


async def record_match_result(winner: Player, loser: Player, is_faceit: bool) -> None:
    w = player_stats.setdefault(winner.user_id, PlayerStats())
    l = player_stats.setdefault(loser.user_id, PlayerStats())

    w.wins += 1
    w.label = winner.label()
    l.losses += 1
    l.label = loser.label()

    if is_faceit:
        w_k = 50 if w.matches_played < 5 else 25
        l_k = 50 if l.matches_played < 5 else 25

        expected_w = 1 / (1 + 10 ** ((l.elo - w.elo) / 400))
        expected_l = 1 / (1 + 10 ** ((w.elo - l.elo) / 400))

        w_change = round(w_k * (1 - expected_w))
        l_change = round(l_k * (0 - expected_l)) 

        w.elo += max(1, w_change)
        l.elo = max(0, l.elo + l_change) 

        w.matches_played += 1
        l.matches_played += 1

    await asyncio.to_thread(db_save_player_stats, winner.user_id, w)
    await asyncio.to_thread(db_save_player_stats, loser.user_id, l)


def build_stats_text(user_id: int, fallback_label: str) -> str:
    stats = player_stats.get(user_id)
    if not stats or (stats.wins == 0 and stats.losses == 0):
        return f"📊 {fallback_label}, у вас пока нет завершённых матчей."

    total = stats.wins + stats.losses
    faceit_played = stats.matches_played
    winrate = round(stats.wins / total * 100) if total > 0 else 0
    
    if faceit_played < 5:
        lvl_text = f"🔄 Калибровка ({faceit_played}/5)"
        elo_text = "Скрыто"
    else:
        lvl = get_faceit_level(stats.elo)
        lvl_text = f"{lvl} lvl"
        elo_text = str(stats.elo)

    return (
        f"📊 Статистика {stats.label}\n\n"
        f"🏆 Уровень: {lvl_text}\n"
        f"📈 ELO: {elo_text}\n"
        f"Матчей сыграно всего: {total}\n"
        f"Побед: {stats.wins}\n"
        f"Поражений: {stats.losses}\n"
        f"Винрейт: {winrate}%"
    )


def challenge_stats_line(user_id: int, is_faceit: bool) -> str:
    stats = player_stats.get(user_id)
    if not stats:
        return "Матчей пока не было"
    
    if is_faceit:
        if stats.matches_played < 5:
            return f"🔄 Калибровка ({stats.matches_played}/5 матчей)"
        lvl = get_faceit_level(stats.elo)
        return f"Уровень {lvl} 📊 {stats.elo} ELO"
    else:
        total = stats.wins + stats.losses
        winrate = round(stats.wins / total * 100) if total > 0 else 0
        return f"Рекорд: {stats.wins}W-{stats.losses}L ({winrate}% винрейт, {total} матчей)"


def build_leaderboard_text() -> str:
    # В топ попадают только игроки, прошедшие калибровку (5+ матчей FACEIT)
    active_players = [
        (uid, s) for uid, s in player_stats.items() if s.matches_played >= 5
    ]
    
    if not active_players:
        return "🏆 <b>Таблица лидеров пуста!</b>\nСыграйте хотя бы 5 матчей /faceit для калибровки."

    active_players.sort(key=lambda item: item[1].elo, reverse=True)

    lines = ["🏆 <b>ТОП-10 ИГРОКОВ (FACEIT)</b> 🏆", ""]
    for index, (uid, s) in enumerate(active_players[:10], start=1):
        lvl = get_faceit_level(s.elo)
        
        if index == 1: medal = "🥇"
        elif index == 2: medal = "🥈"
        elif index == 3: medal = "🥉"
        else: medal = f"<b>{index}.</b>"

        winrate = round(s.wins / (s.wins + s.losses) * 100) if (s.wins + s.losses) > 0 else 0
        lines.append(
            f"{medal} [{lvl} lvl] {s.label} — <b>{s.elo} ELO</b> (WR: {winrate}%)"
        )
    return "\n".join(lines)


# ---------- Хендлеры и Команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для игры в Камень-Ножницы-Бумага.\n\n"
        "Напиши /game для обычной игры, или /faceit для рейтингового матча."
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    text = message.text.split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        await message.answer(
            "Использование: /broadcast текст сообщения\n\n"
            f"Известно чатов для рассылки: {len(known_chats)}"
        )
        return

    broadcast_text = text[1]
    sent, failed = 0, 0

    for chat_id in list(known_chats):
        try:
            await message.bot.send_message(chat_id, broadcast_text)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Не удалось отправить рассылку в чат {chat_id}: {e}")
        await asyncio.sleep(0.05)

    await message.answer(f"Рассылка завершена. Успешно: {sent}, не удалось: {failed}")


# ---------- Управление чатами (только OWNER_ID) ----------
@router.message(Command("chats"))
async def cmd_chats(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    if not known_chats:
        await message.answer("Бот пока не состоит ни в одном известном чате.")
        return

    lines = ["📋 <b>Чаты, где состоит бот:</b>", ""]
    for chat_id in sorted(known_chats):
        try:
            chat = await message.bot.get_chat(chat_id)
            title = chat.title or chat.full_name or chat.username or "(без названия)"
        except Exception:
            title = "(название недоступно)"

        lines.append(f"<code>{chat_id}</code> — {html.escape(str(title))}")

    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    fallback_label = (
        f"@{html.escape(message.from_user.username)}"
        if message.from_user.username
        else html.escape(message.from_user.full_name)
    )
    await message.answer(build_stats_text(message.from_user.id, fallback_label))


@router.message(Command("top"))
async def cmd_top(message: Message):
    await message.answer(build_leaderboard_text())


# ---------- Команда Настроек (Доступна ТОЛЬКО OWNER_ID) ----------
@router.message(Command("settings"))
async def cmd_settings(message: Message):
    if message.from_user.id != OWNER_ID:
        logger.warning(f"Несанкционированный доступ к /settings от ID: {message.from_user.id}")
        await message.answer("⚠️ У вас нет прав для использования этой команды.")
        
        try:
            await message.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"⚠️ <b>Внимание! Попытка доступа</b>\n"
                    f"Пользователь {message.from_user.full_name} (@{message.from_user.username} / ID: <code>{message.from_user.id}</code>)\n"
                    f"попытался использовать команду /settings в чате <code>{message.chat.id}</code>."
                )
            )
        except Exception:
            pass
            
        return

    await message.answer(
        "⚙️ <b>Панель управления настройками бота в чате:</b>\n\n"
        "Вы можете изменить правила проведения матчей кнопками ниже.",
        reply_markup=settings_keyboard(message.chat.id)
    )

@router.callback_query(ToggleOvertimeCB.filter())
async def on_toggle_overtime(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⚠️ Менять настройки может только администратор бота.", show_alert=True)
        try:
            await callback.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"⚠️ <b>Внимание! Попытка изменить настройки</b>\n"
                    f"Пользователь {callback.from_user.full_name} (@{callback.from_user.username} / ID: <code>{callback.from_user.id}</code>)\n"
                    f"попытался нажать кнопку 'Допы' в чате <code>{callback.message.chat.id}</code>."
                )
            )
        except Exception:
            pass
        return

    chat_id = callback.message.chat.id
    new_status = not is_overtime_enabled(chat_id)
    well_status = is_well_enabled(chat_id)
    
    chat_overtime_settings[chat_id] = new_status
    await asyncio.to_thread(db_save_chat_settings, chat_id, new_status, well_status)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=settings_keyboard(chat_id))
    except Exception:
        pass
    
    try:
        await callback.answer(f"Допы при 4:4: {'ВКЛЮЧЕНЫ' if new_status else 'ВЫКЛЮЧЕНЫ'}!")
    except Exception:
        pass


@router.callback_query(ToggleWellCB.filter())
async def on_toggle_well(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⚠️ Менять настройки может только администратор бота.", show_alert=True)
        try:
            await callback.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"⚠️ <b>Внимание! Попытка изменить настройки</b>\n"
                    f"Пользователь {callback.from_user.full_name} (@{callback.from_user.username} / ID: <code>{callback.from_user.id}</code>)\n"
                    f"попытался нажать кнопку 'Колодец' в чате <code>{callback.message.chat.id}</code>."
                )
            )
        except Exception:
            pass
        return

    chat_id = callback.message.chat.id
    new_status = not is_well_enabled(chat_id)
    ot_status = is_overtime_enabled(chat_id)
    
    chat_well_settings[chat_id] = new_status
    await asyncio.to_thread(db_save_chat_settings, chat_id, ot_status, new_status)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=settings_keyboard(chat_id))
    except Exception:
        pass
    
    try:
        await callback.answer(f"Колодец: {'ВКЛЮЧЕН' if new_status else 'ВЫКЛЮЧЕН'}!")
    except Exception:
        pass


# ---------- Турнирный планировщик ----------
@router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    global _next_scheduled_id
    if message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) != 6:
        await message.answer(
            "⚠️ <b>[Турнир] Использование:</b>\n"
            "<code>/schedule Название @user1 @user2 ГГГГ-ММ-ДД ЧЧ:ММ</code>\n\n"
            "Время указывается строго по московскому времени (МСК)."
        )
        return
    _, round_name, p1_raw, p2_raw, date_str, time_str = parts
    if not p1_raw.startswith("@") or not p2_raw.startswith("@"):
        await message.answer("⚠️ Игроки должны быть указаны через @username.")
        return
    try:
        naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        scheduled_time = naive_time.replace(tzinfo=get_msk_tz())
    except ValueError:
        await message.answer("⚠️ Неверный формат даты/времени. Нужно: ГГГГ-ММ-ДД ЧЧ:ММ (МСК).")
        return

    match = ScheduledMatch(
        id=_next_scheduled_id,
        chat_id=OFFICIAL_CHAT_ID,
        round_name=round_name,
        player1_username=p1_raw[1:],
        player2_username=p2_raw[1:],
        scheduled_time=scheduled_time,
    )
    scheduled_matches[match.id] = match
    _next_scheduled_id += 1

    await message.answer(
        f"🏆 Матч запланирован (# {match.id})\n\n"
        f"<b>{match.round_name}:</b> @{match.player1_username} vs @{match.player2_username}\n"
        f"Время: {scheduled_time.strftime('%Y-%m-%d %H:%M')} (МСК)\n\n"
        f"Бот сделает автосозыв в чате {OFFICIAL_CHAT_ID}."
    )


@router.message(Command("schedule_list"))
async def cmd_schedule_list(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    active = [m for m in scheduled_matches.values() if m.status not in ("finished", "cancelled", "no_show")]
    if not active:
        await message.answer("⚠️ Запланированных матчей нет.")
        return
    lines = ["🏆 Запланированные матчи (время МСК):", ""]
    for m in sorted(active, key=lambda x: x.scheduled_time):
        msk_time = m.scheduled_time.astimezone(get_msk_tz())
        lines.append(
            f"# {m.id} [{m.status}] {m.round_name}: @{m.player1_username} vs @{m.player2_username} — "
            f"{msk_time.strftime('%Y-%m-%d %H:%M')}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("cancel_match"))
async def cmd_cancel_match(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /cancel_match НОМЕР")
        return
    match_id = int(parts[1])
    match = scheduled_matches.get(match_id)
    if not match or match.status in ("finished", "cancelled", "no_show"):
        await message.answer("Активный запланированный матч не найден.")
        return
    was_called = match.status == "called"
    match.status = "cancelled"
    if was_called and match.message_id and BOT is not None:
        try:
            await BOT.unpin_chat_message(chat_id=match.chat_id, message_id=match.message_id)
        except Exception:
            pass
        try:
            await BOT.edit_message_text(chat_id=match.chat_id, message_id=match.message_id, text=f"❌ Матч «{match.round_name}» отменён.")
        except Exception:
            pass
    await message.answer(f"Матч # {match_id} отменён.")


# ---------- Фоновый процесс контроля времени ----------
async def scheduled_matches_watcher():
    while True:
        await asyncio.sleep(SCHEDULED_CHECK_INTERVAL)
        now = datetime.now(timezone.utc)
        for match in list(scheduled_matches.values()):
            if match.status == "pending" and match.scheduled_time <= now:
                await call_scheduled_match(match)
            elif match.status == "called":
                elapsed = (now - match.called_at).total_seconds()
                if elapsed >= SCHEDULED_FORFEIT_CEILING:
                    await forfeit_scheduled_match(match)
                elif (now - match.last_reminder_at).total_seconds() >= SCHEDULED_REMINDER_INTERVAL:
                    await remind_scheduled_match(match)


def build_scheduled_call_text(match: ScheduledMatch) -> str:
    bo_n = match.duels_to_win * 2 - 1
    lines = [
        "🏆 <b>Официальный матч плей-офф!</b>",
        "",
        f"<b>⚔️ {match.round_name}</b>",
        f"@{match.player1_username} vs @{match.player2_username}",
        f"Формат серии: Bo{bo_n}",
        "",
        "Оба игрока обязаны подтвердить готовность кнопкой ниже:",
        "",
    ]
    for username in (match.player1_username, match.player2_username):
        mark = "✅" if username.lower() in match.ready_usernames else "⏳"
        lines.append(f"{mark} @{username}")
    return "\n".join(lines)


async def call_scheduled_match(match: ScheduledMatch):
    if BOT is None:
        return
    match.status = "called"
    now = datetime.now(timezone.utc)
    match.called_at = now
    match.last_reminder_at = now
    try:
        sent = await BOT.send_message(
            chat_id=match.chat_id,
            text=build_scheduled_call_text(match),
            reply_markup=scheduled_ready_keyboard(match.id)
        )
        match.message_id = sent.message_id
        try:
            await BOT.pin_chat_message(chat_id=match.chat_id, message_id=sent.message_id)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Не удалось сделать созыв матча # {match.id}: {e}")


async def remind_scheduled_match(match: ScheduledMatch):
    if BOT is None:
        return
    match.last_reminder_at = datetime.now(timezone.utc)
    missing = [u for u in (match.player1_username, match.player2_username) if u.lower() not in match.ready_usernames]
    if not missing:
        return
    tags = " ".join(f"@{u}" for u in missing)
    try:
        await BOT.send_message(chat_id=match.chat_id, text=f"⏰ {tags}, жду вашей готовности в закрепе на матч «{match.round_name}»!")
    except Exception:
        pass


async def forfeit_scheduled_match(match: ScheduledMatch):
    if BOT is None:
        return
    ready = match.ready_usernames
    p1_ready = match.player1_username.lower() in ready
    p2_ready = match.player2_username.lower() in ready
    match.status = "no_show"
    if match.message_id:
        try:
            await BOT.unpin_chat_message(chat_id=match.chat_id, message_id=match.message_id)
        except Exception:
            pass
        if p1_ready and not p2_ready:
            text = f"⌛ @{match.player2_username} дисквалифицирован за неявку.\n🏆 Техническая победа: @{match.player1_username}"
        elif p2_ready and not p1_ready:
            text = f"⌛ @{match.player1_username} дисквалифицирован за неявку.\n🏆 Техническая победа: @{match.player2_username}"
        else:
            text = "❌ Ни один из игроков не подтвердил участие. Матч отменён."
        try:
            await BOT.edit_message_text(chat_id=match.chat_id, message_id=match.message_id, text=text)
        except Exception:
            pass


# ---------- Игровая механика, Коллбэки и Запуск ----------
@router.callback_query(ScheduledReadyCB.filter())
async def on_scheduled_ready(callback: CallbackQuery, callback_data: ScheduledReadyCB):
    match = scheduled_matches.get(callback_data.match_id)
    if not match or match.status != "called":
        await callback.answer("Этот созыв больше не активен.", show_alert=True)
        return
    username = (callback.from_user.username or "").lower()
    if username not in (match.player1_username.lower(), match.player2_username.lower()):
        await callback.answer("Вы не участвуете в этом матче.", show_alert=True)
        return
    if username in match.ready_usernames:
        await callback.answer("Вы уже подтвердили готовность.", show_alert=True)
        return

    match.ready_usernames.add(username)
    player_obj = Player(user_id=callback.from_user.id, name=callback.from_user.full_name, username=callback.from_user.username)
    if username == match.player1_username.lower():
        match.player1_obj = player_obj
    else:
        match.player2_obj = player_obj

    if match.player1_obj and match.player2_obj:
        if active_games.get(match.chat_id):
            await callback.answer("В чате идёт другая игра, готовность сохранена.", show_alert=True)
            return
        match.status = "in_progress"
        game = Game(
            chat_id=match.chat_id, creator=match.player1_obj, opponent=match.player2_obj,
            target_score=5, duels_to_win=match.duels_to_win,
            duel_wins={match.player1_obj.user_id: 0, match.player2_obj.user_id: 0},
            message_id=match.message_id, scheduled_match_id=match.id,
            is_faceit=True
        )
        await start_duel(game)
        active_games[match.chat_id] = game
        try:
            await callback.message.edit_text(build_round_text(game), reply_markup=moves_keyboard(is_tournament=True, well_enabled=is_well_enabled(game.chat_id)))
        except Exception:
            pass
        schedule_move_timer(game)
        await callback.answer("Матч начинается!")
        return
    try:
        await callback.message.edit_text(build_scheduled_call_text(match), reply_markup=scheduled_ready_keyboard(match.id))
    except Exception:
        pass
    await callback.answer("Готовность принята.")


def check_existing_game(chat_id: int, user_id: int) -> bool:
    existing = active_games.get(chat_id)
    if existing and existing.status != GameStatus.finished:
        return True
    return False


@router.message(Command("game"))
async def cmd_game(message: Message):
    chat_id = message.chat.id
    if check_existing_game(chat_id, message.from_user.id):
        existing = active_games.get(chat_id)
        if message.from_user.id in {p.user_id for p in existing.players()}:
            await message.answer("Вы сейчас участвуете в активной игре. Хотите выйти?", reply_markup=leave_keyboard())
        else:
            await message.answer("В этом чате уже есть активная игра. Дождитесь её окончания.")
        return

    creator = Player(user_id=message.from_user.id, name=message.from_user.full_name, username=message.from_user.username)
    game = Game(chat_id=chat_id, creator=creator, status=GameStatus.waiting_join, is_faceit=False)
    active_games[chat_id] = game

    sent = await message.answer(
        f"🎮 {creator.label()} хочет сыграть **обычную игру**!\n"
        f"{challenge_stats_line(creator.user_id, is_faceit=False)}\n\n"
        "Нажмите кнопку ниже, чтобы принять вызов:",
        reply_markup=join_keyboard(),
    )
    game.message_id = sent.message_id
    schedule_pregame_timer(game, GameStatus.waiting_join)


@router.message(Command("faceit"))
async def cmd_faceit(message: Message):
    chat_id = message.chat.id
    if check_existing_game(chat_id, message.from_user.id):
        existing = active_games.get(chat_id)
        if message.from_user.id in {p.user_id for p in existing.players()}:
            await message.answer("Вы сейчас участвуете в активной игре. Хотите выйти?", reply_markup=leave_keyboard())
        else:
            await message.answer("В этом чате уже есть активная игра. Дождитесь её окончания.")
        return

    creator = Player(user_id=message.from_user.id, name=message.from_user.full_name, username=message.from_user.username)
    game = Game(chat_id=chat_id, creator=creator, status=GameStatus.waiting_join, is_faceit=True)
    active_games[chat_id] = game

    sent = await message.answer(
        f"🏆 {creator.label()} ищет соперника на **FACEIT матч** (Рейтинг)!\n"
        f"{challenge_stats_line(creator.user_id, is_faceit=True)}\n\n"
        "Нажмите кнопку ниже, чтобы принять вызов:",
        reply_markup=join_keyboard(),
    )
    game.message_id = sent.message_id
    schedule_pregame_timer(game, GameStatus.waiting_join)


@router.callback_query(JoinCB.filter())
async def on_join(callback: CallbackQuery):
    game = active_games.get(callback.message.chat.id)
    if not game or game.status != GameStatus.waiting_join:
        await callback.answer("Эта игра больше не активна.", show_alert=True)
        return

    if callback.from_user.id == game.creator.user_id:
        await callback.answer("Нельзя принять собственный вызов 🙂", show_alert=True)
        return

    game.opponent = Player(
        user_id=callback.from_user.id,
        name=callback.from_user.full_name,
        username=callback.from_user.username,
    )
    
    if game.is_faceit:
        game.status = GameStatus.choosing_format
        try:
            await callback.message.edit_text(
                f"🏆 {game.creator.label()} vs {game.opponent.label()}\n\n"
                f"{game.creator.label()}, выберите формат FACEIT матча:",
                reply_markup=format_keyboard(),
            )
        except Exception:
            pass
        schedule_pregame_timer(game, GameStatus.choosing_format)
    else:
        game.status = GameStatus.choosing_rounds
        try:
            await callback.message.edit_text(
                f"🎮 {game.creator.label()} vs {game.opponent.label()}\n\n"
                f"{game.creator.label()}, выберите количество побед для обычной игры:",
                reply_markup=rounds_keyboard(),
            )
        except Exception:
            pass
        schedule_pregame_timer(game, GameStatus.choosing_rounds)

    await callback.answer("Вызов принят!")


@router.callback_query(RoundsCB.filter())
async def on_rounds_chosen(callback: CallbackQuery, callback_data: RoundsCB):
    game = active_games.get(callback.message.chat.id)
    if not game or game.status != GameStatus.choosing_rounds:
        await callback.answer("Эта игра больше не активна.", show_alert=True)
        return

    if callback.from_user.id != game.creator.user_id:
        await callback.answer("Только создатель игры может выбрать количество раундов.", show_alert=True)
        return

    game.target_score = callback_data.target
    game.duels_to_win = 1
    game.duel_wins = {game.creator.user_id: 0, game.opponent.user_id: 0}
    game.duel_number = 1
    game.message_id = callback.message.message_id

    await start_duel(game)
    try:
        await callback.message.edit_text(build_round_text(game), reply_markup=moves_keyboard(is_tournament=False, well_enabled=is_well_enabled(game.chat_id)))
    except Exception:
        pass
    schedule_move_timer(game)
    await callback.answer()


@router.callback_query(FormatCB.filter())
async def on_format_chosen(callback: CallbackQuery, callback_data: FormatCB):
    game = active_games.get(callback.message.chat.id)
    if not game or game.status != GameStatus.choosing_format:
        await callback.answer("Эта игра больше не активна.", show_alert=True)
        return

    if callback.from_user.id != game.creator.user_id:
        await callback.answer("Только создатель игры может выбрать формат.", show_alert=True)
        return

    game.target_score = 5
    game.duels_to_win = callback_data.duels_to_win
    game.duel_wins = {game.creator.user_id: 0, game.opponent.user_id: 0}
    game.duel_number = 1
    game.message_id = callback.message.message_id

    await start_duel(game)
    try:
        await callback.message.edit_text(build_round_text(game), reply_markup=moves_keyboard(is_tournament=True, well_enabled=is_well_enabled(game.chat_id)))
    except Exception:
        pass
    schedule_move_timer(game)
    await callback.answer()


async def start_duel(game: Game) -> None:
    game.status = GameStatus.playing
    game.scores = {game.creator.user_id: 0, game.opponent.user_id: 0}
    game.round_number = 1
    game.choices = {}
    game.history = []
    game.ready_for_next = set()
    game.dodges = {game.creator.user_id: 0, game.opponent.user_id: 0}

    turn_order = [game.creator.user_id, game.opponent.user_id]
    random.shuffle(turn_order)
    game.turn_order = turn_order


@router.callback_query(ResumeCB.filter())
async def on_resume(callback: CallbackQuery):
    game = active_games.get(callback.message.chat.id)
    if not game or game.status != GameStatus.paused:
        await callback.answer("Матч сейчас не на паузе.", show_alert=True)
        return

    user_id = callback.from_user.id
    participant_ids = {p.user_id for p in game.players()}
    if user_id not in participant_ids:
        await callback.answer("Вы не участвуете в этом матче.", show_alert=True)
        return

    if user_id in game.resume_ready:
        await callback.answer("Вы уже готовы, ждём соперника.", show_alert=True)
        return

    game.resume_ready.add(user_id)

    if participant_ids.issubset(game.resume_ready):
        game.status = GameStatus.playing
        game.resume_ready = set()
        is_tournament = game.duels_to_win > 1
        try:
            await callback.message.edit_text(build_round_text(game), reply_markup=moves_keyboard(is_tournament=is_tournament, well_enabled=is_well_enabled(game.chat_id)))
        except Exception:
            pass
        schedule_move_timer(game)
        await callback.answer("Оба готовы! Матч продолжается.")
        return

    is_tournament = game.duels_to_win > 1
    try:
        await callback.message.edit_text(build_pause_text(game), reply_markup=resume_keyboard(is_tournament=is_tournament))
    except Exception:
        pass
    await callback.answer("Готовность принята! Ждём соперника.")


@router.callback_query(ReadyCB.filter())
async def on_ready(callback: CallbackQuery):
    game = active_games.get(callback.message.chat.id)
    if not game or game.status != GameStatus.between_duels:
        await callback.answer("Эта игра больше не активна.", show_alert=True)
        return

    user_id = callback.from_user.id
    participant_ids = {p.user_id for p in game.players()}
    if user_id not in participant_ids:
        await callback.answer("Вы не участвуете в этом матче.", show_alert=True)
        return

    if user_id in game.ready_for_next:
        await callback.answer("Вы уже готовы, ждём соперника.", show_alert=True)
        return

    game.ready_for_next.add(user_id)

    if participant_ids.issubset(game.ready_for_next):
        game.duel_number += 1
        await start_duel(game)
        is_tournament = game.duels_to_win > 1
        try:
            await callback.message.edit_text(build_round_text(game), reply_markup=moves_keyboard(is_tournament=is_tournament, well_enabled=is_well_enabled(game.chat_id)))
        except Exception:
            pass
        schedule_move_timer(game)
        await callback.answer("Оба готовы! Начинаем игру.")
        return

    winner = game.creator if game.last_duel_winner_id == game.creator.user_id else game.opponent
    try:
        await callback.message.edit_text(
            build_duel_finished_text(game, winner),
            reply_markup=ready_keyboard(),
        )
    except Exception:
        pass
    await callback.answer("Готовность принята! Ждём соперника.")


@router.callback_query(CancelCB.filter())
async def on_cancel(callback: CallbackQuery):
    game = active_games.get(callback.message.chat.id)
    if not game:
        await callback.answer("Эта игра уже неактуальна.", show_alert=True)
        return

    allowed_ids = {p.user_id for p in game.players()}
    if callback.from_user.id not in allowed_ids:
        await callback.answer("Отменить игру может только её участник.", show_alert=True)
        return

    active_games.pop(callback.message.chat.id, None)
    cancel_move_timer(game)
    try:
        await callback.message.edit_text("Игра отменена.")
    except Exception:
        pass
    await callback.answer()


@router.callback_query(LeaveCB.filter())
async def on_leave(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = active_games.get(chat_id)
    if not game:
        await callback.answer("Активной игры уже нет.", show_alert=True)
        return

    user_id = callback.from_user.id
    leaver = next((p for p in game.players() if p.user_id == user_id), None)
    if not leaver:
        await callback.answer("Вы не участвуете в этой игре.", show_alert=True)
        return

    active_games.pop(chat_id, None)
    cancel_move_timer(game)
    try:
        await callback.message.edit_text(f"🚪 {leaver.label()} покинул(а) игру. Матч отменён.")
    except Exception:
        pass
    await callback.answer()


@router.callback_query(HistoryCB.filter())
async def on_history(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = active_games.get(chat_id) or finished_games.get(chat_id)
    if not game:
        await callback.answer("История недоступна — матч не найден.", show_alert=True)
        return

    if callback.from_user.id not in {p.user_id for p in game.players()}:
        await callback.answer("Вы не участвуете в этом матче.", show_alert=True)
        return

    try:
        await callback.message.answer(build_history_text(game))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(MoveCB.filter())
async def on_move(callback: CallbackQuery, callback_data: MoveCB):
    game = active_games.get(callback.message.chat.id)
    if not game or game.status != GameStatus.playing:
        await callback.answer("Эта игра больше не активна.", show_alert=True)
        return

    user_id = callback.from_user.id
    allowed_ids = {p.user_id for p in game.players()}
    if user_id not in allowed_ids:
        await callback.answer("Вы не участвуете в этой игре.", show_alert=True)
        return

    if user_id in game.choices:
        await callback.answer("Вы уже сделали выбор в этом раунде! Ожидайте соперника.", show_alert=True)
        return

    next_uid = next((uid for uid in game.turn_order if uid not in game.choices), None)
    if user_id != next_uid:
        waiting_player = game.creator if game.creator.user_id == next_uid else game.opponent
        await callback.answer(
            f"Сейчас ходит {waiting_player.label()}, дождитесь своей очереди.",
            show_alert=True,
        )
        return

    game.choices[user_id] = callback_data.move
    await callback.answer("Выбор принят! Никто не увидит его, пока не выберет соперник.")

    if len(game.choices) < 2:
        schedule_move_timer(game)
        is_tournament = game.duels_to_win > 1
        try:
            await callback.message.edit_text(build_round_text(game), reply_markup=moves_keyboard(is_tournament=is_tournament, well_enabled=is_well_enabled(game.chat_id)))
        except Exception:
            pass
        return

    await resolve_round(callback, game)


def history_lines(game: Game) -> list[str]:
    lines = []
    for i, r in enumerate(game.history, start=1):
        if r.winner_id is None:
            outcome = "Ничья"
        else:
            winner = game.creator if game.creator.user_id == r.winner_id else game.opponent
            outcome = f"🏆 {winner.label()}"
        lines.append(f"{i}) {MOVE_EMOJI[r.move1]} vs {MOVE_EMOJI[r.move2]} ({outcome})")
    return lines


def build_round_text(game: Game, remaining_seconds: int | None = None) -> str:
    p1, p2 = game.creator, game.opponent
    is_tournament = game.duels_to_win > 1

    s1 = game.scores.get(p1.user_id, 0)
    s2 = game.scores.get(p2.user_id, 0)

    ot_active = is_overtime_enabled(game.chat_id)

    if game.target_score == 5 and s1 >= 4 and s2 >= 4 and ot_active:
        target_desc = "до разницы в 2 очка"
    else:
        target_desc = f"до {game.target_score} побед"

    score_line = f"{emoji_number(s1)} : {emoji_number(s2)} ({target_desc})"

    if is_tournament:
        bo_n = game.duels_to_win * 2 - 1
        match_score_line = (
            f"Матч Bo{bo_n} — счёт игр: {emoji_number(game.duel_wins.get(p1.user_id, 0))} : {emoji_number(game.duel_wins.get(p2.user_id, 0))} "
            f"(до {game.duels_to_win})"
        )
        parts = [f"<b>Идёт игра {game.duel_number}</b>", match_score_line, ""]
    else:
        parts = ["<b>Идёт дуэль</b>", ""]

    parts += [f"{p1.label()} vs {p2.label()}", "", score_line]

    dodge_lines = []
    for p in (p1, p2):
        d_count = game.dodges.get(p.user_id, 0)
        if d_count > 0:
            dodge_lines.append(f"⚠️ Доджи {p.label()}: {d_count}/{MAX_DODGES_PER_PLAYER}")
    if dodge_lines:
        parts.append("")
        parts.extend(dodge_lines)

    hist = history_lines(game)
    if hist:
        parts.append("")
        parts.extend(hist)

    next_uid = next((uid for uid in game.turn_order if uid not in game.choices), None)
    next_player = game.creator if game.creator.user_id == next_uid else game.opponent

    if remaining_seconds is None:
        remaining_seconds = MOVE_TIMEOUT_SECONDS

    parts.append("")
    parts.append(f"<b>Ходит: {next_player.label()}</b>")
    parts.append(f"⏳ Осталось: {remaining_seconds} сек")
    return "\n".join(parts)


def build_pause_text(game: Game) -> str:
    p1, p2 = game.creator, game.opponent
    loser = p1 if game.pause_timeout_uid == p1.user_id else p2
    d_count = game.dodges.get(loser.user_id, 0)

    lines = [
        "<b>⏸ Матч на паузе</b>",
        "",
        f"{p1.label()} vs {p2.label()}",
        "",
        f"⏰ {loser.label()} не успел(а) сделать ход вовремя.",
        f"⚠️ Использован додж: {d_count}/{MAX_DODGES_PER_PLAYER}",
        "",
        "Чтобы продолжить с того же места, оба нажмите «▶️ Возобновить игру»:",
        "",
    ]
    for p in game.players():
        mark = "✅" if p.user_id in game.resume_ready else "⏳"
        lines.append(f"{mark} {p.label()}")
    return "\n".join(lines)


def _duel_round_lines(rounds: list[RoundResult], p1: Player, p2: Player) -> list[str]:
    lines = []
    for i, r in enumerate(rounds, start=1):
        if r.winner_id is None:
            outcome = "Ничья"
        else:
            rw = p1 if r.winner_id == p1.user_id else p2
            outcome = f"🏆 {rw.label()}"
        lines.append(f"  {i}) {MOVE_EMOJI[r.move1]} vs {MOVE_EMOJI[r.move2]} ({outcome})")
    return lines


def build_history_text(game: Game) -> str:
    p1, p2 = game.creator, game.opponent
    lines = ["<b>📜 История матча</b>", "", f"{p1.label()} vs {p2.label()}"]

    if not game.duel_history and not game.history:
        lines.append("")
        lines.append("Пока сыграно раундов нет.")
        return "\n".join(lines)

    for d in game.duel_history:
        winner_label = p1.label() if d.winner_id == p1.user_id else p2.label()
        lines.append("")
        if d.by_timeout:
            lines.append(f"<b>Игра {d.duel_number}</b> — 🏆 {winner_label} (соперник превысил лимит доджей / ТП)")
        else:
            lines.append(f"<b>Игра {d.duel_number}</b> — счёт {d.score_p1}:{d.score_p2}, победитель: 🏆 {winner_label}")
        lines.extend(_duel_round_lines(d.rounds, p1, p2))

    if game.history and game.status != GameStatus.finished:
        lines.append("")
        lines.append(f"<b>Игра {game.duel_number} (идёт)</b>")
        lines.extend(_duel_round_lines(game.history, p1, p2))

    return "\n".join(lines)


def build_session_finished_text(game: Game, winner: Player) -> str:
    p1, p2 = game.creator, game.opponent
    score_line = (
        f"{emoji_number(game.scores[p1.user_id])} : {emoji_number(game.scores[p2.user_id])} "
        f"(до {game.target_score} побед)"
    )
    parts = [
        "<b>Дуэль завершена</b>",
        "",
        f"{p1.label()} vs {p2.label()}",
        "",
        score_line,
        "",
        *history_lines(game),
        "",
        f"<b>🏆 {winner.label()} победил!</b>",
    ]
    return "\n".join(parts)


def build_dodge_forfeit_text(game: Game, winner: Player, loser: Player) -> str:
    p1, p2 = game.creator, game.opponent
    lines = [
        "<b>Матч завершён (ТП)</b>",
        "",
        f"{p1.label()} vs {p2.label()}",
        "",
        f"⏰ {loser.label()} превысил лимит доджей ({MAX_DODGES_PER_PLAYER + 1}/{MAX_DODGES_PER_PLAYER})!",
        f"🏆 Победитель матча: <b>{winner.label()}</b>",
    ]
    return "\n".join(lines)


def build_duel_finished_text(game: Game, duel_winner: Player) -> str:
    p1, p2 = game.creator, game.opponent
    score_line = (
        f"{emoji_number(game.scores[p1.user_id])} : {emoji_number(game.scores[p2.user_id])} "
        f"(до {game.target_score} побед)"
    )
    match_score_line = (
        f"Счёт игр в матче: {emoji_number(game.duel_wins.get(p1.user_id, 0))} : {emoji_number(game.duel_wins.get(p2.user_id, 0))} "
        f"(до {game.duels_to_win})"
    )
    ready_lines = []
    for p in game.players():
        mark = "✅" if p.user_id in game.ready_for_next else "⏳"
        ready_lines.append(f"{mark} {p.label()}")

    parts = [
        f"<b>Игра {game.duel_number} завершена</b>",
        "",
        f"{p1.label()} vs {p2.label()}",
        "",
        score_line,
        "",
        *history_lines(game),
        "",
        f"<b>🏆 {duel_winner.label()} победил(а) в игре!</b>",
        "",
        match_score_line,
        "",
        "Готовность к следующей игре:",
        *ready_lines,
    ]
    return "\n".join(parts)


def build_match_finished_text(game: Game, match_winner: Player) -> str:
    p1, p2 = game.creator, game.opponent
    lines = [
        "<b>Матч завершён</b>",
        "",
        f"{p1.label()} vs {p2.label()}",
        "",
        f"Счёт игр: {emoji_number(game.duel_wins.get(p1.user_id, 0))} : {emoji_number(game.duel_wins.get(p2.user_id, 0))} "
        f"(до {game.duels_to_win})",
        "",
    ]
    for d in game.duel_history:
        winner_label = p1.label() if d.winner_id == p1.user_id else p2.label()
        if d.by_timeout:
            lines.append(f"Игра {d.duel_number}: 🏆 {winner_label} (соперник превысил лимит доджей / ТП)")
        else:
            lines.append(f"Игра {d.duel_number}: {d.score_p1} : {d.score_p2} — 🏆 {winner_label}")
    lines.append("")
    lines.append(f"<b>🏆 {match_winner.label()} выигрывает матч!</b>")
    return "\n".join(lines)


async def finalize_scheduled_match(game: Game, winner: Player) -> None:
    if game.scheduled_match_id is None:
        return
    match = scheduled_matches.get(game.scheduled_match_id)
    if not match:
        return
    match.status = "finished"
    if match.message_id and BOT is not None:
        try:
            await BOT.unpin_chat_message(chat_id=match.chat_id, message_id=match.message_id)
        except Exception as e:
            pass


async def resolve_round(callback: CallbackQuery, game: Game):
    p1, p2 = game.creator, game.opponent
    move1, move2 = game.choices[p1.user_id], game.choices[p2.user_id]

    if move1 == move2:
        winner = None
    elif move2 in WIN_RULES[move1]:
        winner = p1
    else:
        winner = p2

    if winner:
        game.scores[winner.user_id] += 1

    game.history.append(RoundResult(move1=move1, move2=move2, winner_id=winner.user_id if winner else None))
    game.choices = {}

    s1 = game.scores[p1.user_id]
    s2 = game.scores[p2.user_id]
    is_set_won = False

    ot_active = is_overtime_enabled(game.chat_id)

    if game.target_score == 5 and ot_active:
        if s1 >= 4 and s2 >= 4:
            if abs(s1 - s2) >= 2:
                is_set_won = True
                winner = p1 if s1 > s2 else p2
        else:
            if s1 >= 5 or s2 >= 5:
                is_set_won = True
                winner = p1 if s1 >= 5 else p2
    else:
        if winner and game.scores[winner.user_id] >= game.target_score:
            is_set_won = True

    if is_set_won and winner:
        game.duel_wins[winner.user_id] += 1
        game.duel_history.append(
            DuelSummary(
                duel_number=game.duel_number,
                winner_id=winner.user_id,
                score_p1=game.scores[p1.user_id],
                score_p2=game.scores[p2.user_id],
                rounds=list(game.history),
            )
        )
        cancel_move_timer(game)
        game.last_duel_winner_id = winner.user_id

        if game.duel_wins[winner.user_id] >= game.duels_to_win:
            game.status = GameStatus.finished
            active_games.pop(game.chat_id, None)
            finished_games[game.chat_id] = game
            
            loser = p2 if winner is p1 else p1
            
            final_text = (
                build_session_finished_text(game, winner)
                if game.duels_to_win <= 1
                else build_match_finished_text(game, winner)
            )
            is_tournament = game.duels_to_win > 1
            try:
                await callback.message.edit_text(final_text, reply_markup=finished_keyboard(is_tournament=is_tournament))
            except Exception:
                pass
                
            await record_match_result(winner, loser, game.is_faceit)
            await finalize_scheduled_match(game, winner)
            return

        game.status = GameStatus.between_duels
        try:
            await callback.message.edit_text(
                build_duel_finished_text(game, winner),
                reply_markup=ready_keyboard(),
            )
        except Exception:
            pass
        return

    game.round_number += 1
    is_tournament = game.duels_to_win > 1
    try:
        await callback.message.edit_text(build_round_text(game), reply_markup=moves_keyboard(is_tournament=is_tournament, well_enabled=is_well_enabled(game.chat_id)))
    except Exception:
        pass
    schedule_move_timer(game)


def cancel_move_timer(game: Game) -> None:
    if game.timeout_task and not game.timeout_task.done():
        game.timeout_task.cancel()
    game.timeout_task = None


def schedule_pregame_timer(game: Game, expected_status: GameStatus) -> None:
    cancel_move_timer(game)
    game.timeout_task = asyncio.create_task(handle_pregame_timeout(game, expected_status))


async def handle_pregame_timeout(game: Game, expected_status: GameStatus) -> None:
    await asyncio.sleep(PREGAME_TIMEOUT_SECONDS)

    if active_games.get(game.chat_id) is not game:
        return
    if game.status != expected_status:
        return

    active_games.pop(game.chat_id, None)

    if BOT is None or game.message_id is None:
        return

    if expected_status == GameStatus.waiting_join:
        text = (
            f"⌛ {game.creator.label()} создал(а) игру, но никто не откликнулся за "
            f"{PREGAME_TIMEOUT_SECONDS} секунд. Игра отменена."
        )
    else:
        text = (
            f"⌛ {game.creator.label()} не сделал(а) выбор за "
            f"{PREGAME_TIMEOUT_SECONDS} секунд. Игра отменена."
        )

    try:
        await BOT.edit_message_text(chat_id=game.chat_id, message_id=game.message_id, text=text)
    except Exception:
        pass


def schedule_move_timer(game: Game) -> None:
    cancel_move_timer(game)
    next_uid = next((uid for uid in game.turn_order if uid not in game.choices), None)
    if next_uid is None:
        return
    game.timeout_task = asyncio.create_task(
        handle_move_timeout(game, expected_uid=next_uid, expected_round=game.round_number)
    )


async def handle_move_timeout(game: Game, expected_uid: int, expected_round: int) -> None:
    try:
        remaining = MOVE_TIMEOUT_SECONDS

        while remaining > 0:
            step = min(MOVE_TIMER_UPDATE_INTERVAL, remaining)
            await asyncio.sleep(step)
            remaining -= step

            if active_games.get(game.chat_id) is not game:
                return
            if game.status != GameStatus.playing:
                return
            if game.round_number != expected_round:
                return
            if expected_uid in game.choices:
                return

            if remaining > 0 and BOT is not None and game.message_id is not None:
                is_tournament = game.duels_to_win > 1
                try:
                    await BOT.edit_message_text(
                        chat_id=game.chat_id,
                        message_id=game.message_id,
                        text=build_round_text(game, remaining_seconds=remaining),
                        reply_markup=moves_keyboard(is_tournament=is_tournament, well_enabled=is_well_enabled(game.chat_id)),
                    )
                except Exception:
                    pass

        game.dodges[expected_uid] = game.dodges.get(expected_uid, 0) + 1
        current_dodges = game.dodges[expected_uid]

        if current_dodges > MAX_DODGES_PER_PLAYER:
            cancel_move_timer(game)
            
            loser = game.creator if expected_uid == game.creator.user_id else game.opponent
            winner = game.other(loser.user_id) if loser else None
            
            if not loser or not winner:
                return

            active_games.pop(game.chat_id, None)
            game.status = GameStatus.finished
            finished_games[game.chat_id] = game

            if game.duels_to_win > 1:
                game.duel_wins[winner.user_id] += 1
                game.duel_history.append(
                    DuelSummary(
                        duel_number=game.duel_number,
                        winner_id=winner.user_id,
                        score_p1=game.scores[game.creator.user_id],
                        score_p2=game.scores[game.opponent.user_id],
                        by_timeout=True,
                        rounds=list(game.history),
                    )
                )
                game.last_duel_winner_id = winner.user_id

                if game.duel_wins[winner.user_id] >= game.duels_to_win:
                    if BOT is not None and game.message_id is not None:
                        try:
                            await BOT.edit_message_text(
                                chat_id=game.chat_id,
                                message_id=game.message_id,
                                text=build_match_finished_text(game, winner),
                                reply_markup=finished_keyboard(is_tournament=True)
                            )
                        except Exception:
                            pass
                    
                    await record_match_result(winner, loser, game.is_faceit)
                    await finalize_scheduled_match(game, winner)
                    return

                game.status = GameStatus.between_duels
                active_games[game.chat_id] = game
                
                if BOT is not None and game.message_id is not None:
                    try:
                        await BOT.edit_message_text(
                            chat_id=game.chat_id,
                            message_id=game.message_id,
                            text=f"⏰ {loser.label()} превысил лимит доджей!\n"
                                 f"<b>🏆 Сет #{game.duel_number} досрочно завершается победой {winner.label()} (ТП)</b>\n\n"
                                 f"Серия продолжается. Готовность к следующему сету:",
                            reply_markup=ready_keyboard()
                        )
                    except Exception:
                        pass
                return

            else:
                if BOT is not None and game.message_id is not None:
                    try:
                        await BOT.edit_message_text(
                            chat_id=game.chat_id,
                            message_id=game.message_id,
                            text=build_dodge_forfeit_text(game, winner, loser),
                            reply_markup=finished_keyboard(is_tournament=False)
                        )
                    except Exception:
                        pass
                
                await record_match_result(winner, loser, game.is_faceit)
                return

        game.status = GameStatus.paused
        game.pause_timeout_uid = expected_uid
        game.resume_ready = set()

        if BOT is None or game.message_id is None:
            return

        is_tournament = game.duels_to_win > 1
        try:
            await BOT.edit_message_text(
                chat_id=game.chat_id,
                message_id=game.message_id,
                text=build_pause_text(game),
                reply_markup=resume_keyboard(is_tournament=is_tournament),
            )
        except Exception:
            pass
            
    except Exception as e:
        pass


async def main():
    global BOT

    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN.")

    await asyncio.to_thread(db_init)
    await asyncio.to_thread(db_load_into_memory)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT = bot
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="game", description="Обычная игра (без рейтинга)"),
        BotCommand(command="faceit", description="Рейтинговая игра (Влияет на ELO)"),
        BotCommand(command="stats", description="Моя статистика (FACEIT)"),
        BotCommand(command="top", description="Таблица лидеров чата (FACEIT)"),
        BotCommand(command="settings", description="Настройки матчей (админ)"),
        BotCommand(command="chats", description="Список чатов бота (админ)"),
    ])

    asyncio.create_task(scheduled_matches_watcher())

    logger.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())