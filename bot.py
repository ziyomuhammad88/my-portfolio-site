"""
Телеграм-бот для канала о трейдинге.

Логика:
1. Ты (админ) нажимаешь «▶️ Старт» — бот по шагам (опрос-анкета) спрашивает
   скриншот сделки и детали по шаблону: инструмент, итог дня, количество
   сделок, новости, контекст, а затем по каждой сделке отдельно — причину
   входа, стоп, тейк, результат и ошибку/вывод.
2. Когда опрос закончен, бот собирает ответы в заметку и отправляет её в
   Gemini, тот оформляет её в аккуратный пост по тому же шаблону.
3. Бот присылает тебе черновик поста (с фото) и кнопки "Опубликовать" / "Отмена".
4. Если нужно что-то поправить — просто напиши следующим сообщением (или
   командой /edit <что поправить>), бот перегенерирует черновик и снова
   покажет его на подтверждение.
5. Только после нажатия "Опубликовать" (или команды /post) пост уходит в
   канал. Команда /stop отменяет и удаляет текущий опрос или черновик
   (аналог кнопки "Отмена").

Бот реагирует только на сообщения от ADMIN_USER_ID — это твоя защита от того,
что кто-то посторонний напишет боту и что-то опубликует в канал.
"""

import asyncio
import base64
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from openai import OpenAI

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# Часовой пояс для расписания отчётов (суббота/1-е число, 08:00) и для даты,
# под которой сохраняется опубликованная сессия. "or" (не .get(..., default))
# — чтобы пустое значение в .env тоже трактовалось как "используй дефолт".
REPORT_TIMEZONE = os.environ.get("REPORT_TIMEZONE") or "Asia/Tashkent"

# Файл базы данных истории опубликованных сессий (SQLite). По умолчанию —
# рядом с bot.py. Содержит реальную торговую историю — никогда не коммитить
# (см. .gitignore). Пустая строка в качестве пути открыла бы sqlite3
# временную БД, которая исчезает при закрытии соединения, — поэтому "or",
# а не .get(..., default).
DB_PATH = os.environ.get("DB_PATH") or str(Path(__file__).resolve().parent / "history.db")


def parse_csv_list(raw: str) -> list[str]:
    """Разобрать список значений из переменной окружения (через запятую)."""
    return [item.strip() for item in raw.split(",") if item.strip()]


# Если для шага анкеты задан список вариантов — бот покажет кнопки вместо
# свободного текста (но можно всё равно ответить текстом, если нужного
# варианта нет в списке — см. handle_wizard_answer).
INSTRUMENTS = parse_csv_list(os.environ.get("INSTRUMENTS", ""))
DAY_RESULTS = parse_csv_list(os.environ.get("DAY_RESULTS", ""))

NEWS_OPTIONS = ["Нет", "Да"]
# Если на шаге "Новости" выбрали "Да" — не сохраняем это как ответ, а
# просим кратко уточнить, что за новость (см. handle_header_choice_selection).
NEWS_FOLLOWUP_PROMPT = "Какие новости? Кратко."

# Ключ шага анкеты -> список вариантов для кнопок (пусто/нет ключа = только текст).
HEADER_CHOICE_OPTIONS = {
    "instrument": INSTRUMENTS,
    "day_result": DAY_RESULTS,
    "news": NEWS_OPTIONS,
}

MAX_TELEGRAM_MESSAGE_LENGTH = 4096
# У подписи к фото в Telegram лимит короче, чем у обычного текстового сообщения.
MAX_TELEGRAM_CAPTION_LENGTH = 1024

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# Не выводить в консоль URL запросов Telegram: в нём содержится токен бота.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# --- История опубликованных сессий (для недельных/месячных отчётов) -------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    published_at TEXT NOT NULL,
    session_date TEXT NOT NULL,
    instrument TEXT NOT NULL,
    day_result_raw TEXT NOT NULL,
    day_result_value REAL,
    trade_count INTEGER NOT NULL,
    news TEXT NOT NULL,
    psych_mistakes TEXT NOT NULL,
    context TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    trade_index INTEGER NOT NULL,
    direction TEXT NOT NULL,
    why TEXT NOT NULL,
    stop TEXT NOT NULL,
    take TEXT NOT NULL,
    result_raw TEXT NOT NULL,
    result_value REAL,
    mistake TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(session_date);
CREATE INDEX IF NOT EXISTS idx_trades_session_id ON trades(session_id);
"""


def get_connection() -> sqlite3.Connection:
    """Новое соединение с БД истории (не общий глобальный объект — так тесты
    могут подменить DB_PATH на временный файл перед вызовом)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Создать таблицы истории, если их ещё нет. Вызывается один раз при старте."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def save_session(answers: dict, trades: list[dict], session_date: str) -> int:
    """Сохранить опубликованную сессию и её сделки. Возвращает id сессии."""
    day_result_value = parse_signed_amount(answers["day_result"])
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO sessions "
            "(published_at, session_date, instrument, day_result_raw, day_result_value, "
            " trade_count, news, psych_mistakes, context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(ZoneInfo(REPORT_TIMEZONE)).isoformat(),
                session_date,
                answers["instrument"],
                answers["day_result"],
                day_result_value,
                answers["trade_count"],
                answers["news"],
                answers["psych_mistakes"],
                answers["context"],
            ),
        )
        session_id = cursor.lastrowid
        for i, trade in enumerate(trades, start=1):
            result_value = parse_signed_amount(trade["result"])
            conn.execute(
                "INSERT INTO trades "
                "(session_id, trade_index, direction, why, stop, take, result_raw, "
                " result_value, mistake) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    i,
                    trade["direction"],
                    trade["why"],
                    trade["stop"],
                    trade["take"],
                    trade["result"],
                    result_value,
                    trade["mistake"],
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return session_id


def get_sessions_in_range(start_date: str, end_date: str) -> list[dict]:
    """Сессии за период [start_date, end_date] включительно (ISO 'YYYY-MM-DD'),
    каждая — со вложенным списком "trades" (по порядку trade_index)."""
    conn = get_connection()
    try:
        session_rows = conn.execute(
            "SELECT * FROM sessions WHERE session_date BETWEEN ? AND ? ORDER BY session_date",
            (start_date, end_date),
        ).fetchall()

        sessions = []
        for row in session_rows:
            session = dict(row)
            trade_rows = conn.execute(
                "SELECT * FROM trades WHERE session_id = ? ORDER BY trade_index",
                (session["id"],),
            ).fetchall()
            session["trades"] = [dict(trade_row) for trade_row in trade_rows]
            sessions.append(session)
    finally:
        conn.close()
    return sessions


def persist_published_session(draft: dict) -> None:
    """Сохранить опубликованную сессию в историю (для будущих отчётов).

    Ошибка только логируется, не показывается админу: пост уже ушёл в канал,
    это важнее, чем не потерять запись локальной истории — её, при желании,
    можно будет добавить руками позже.
    """
    try:
        session_date = datetime.now(ZoneInfo(REPORT_TIMEZONE)).date().isoformat()
        save_session(draft["answers"], draft["trades"], session_date)
    except Exception:
        logger.exception("Ошибка при сохранении сессии в историю")


# --- Конец блока истории сессий -------------------------------------------

# Gemini отдаёт OpenAI-совместимый API — просто указываем свой base_url и
# обычный пакет openai работает как обычно.
gemini_client = OpenAI(
    api_key=GEMINI_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

SYSTEM_PROMPT = """\
Ты — трейдер, который ведёт свой Telegram-канал и оформляет посты о \
сессиях живым, увлекательным языком практикующего трейдера — не сухим \
отчётом и не канцеляритом, но по чёткому шаблону.

Тебе присылают сырой комментарий о прошедшей сессии — там может быть \
описана одна или несколько сделок. Оформи его СТРОГО по шаблону ниже, не \
меняя порядок и названия полей:

Инструмент: [тикер/пара]
Итог дня: [общий результат дня, например -100$]
Количество сделок: [число]
Новости: [кратко, если упоминались; если новостей нет — напиши "нет"]
Психологические ошибки: [кратко — FOMO, месть рынку, нарушение риска, \
пересиживание и т.п., если были; если нет — напиши "нет"]

Контекст:
[1 абзац общего контекста дня/рынка — тренд, диапазон, важные уровни, \
логика дня. Бери только то, что есть в заметке; если контекста в ней нет \
— не пиши этот абзац вообще]

Дальше — отдельный блок на КАЖДУЮ сделку из заметки, блоки разделяй \
пустой строкой:

[Лонг/Шорт].
Почему вошёл: [сигнал / сетап].
Стоп: [где].
Тейк: [где].
Результат: [плюс/минус, пункты или $].
Что получилось / ошибка: [кратко].

Правила:
- Направление (Лонг/Шорт) и значения всех полей бери строго из заметки — \
не угадывай и не меняй их.
- Свободные текстовые поля (Контекст, Почему вошёл, Что получилось / \
ошибка, Психологические ошибки, Новости) не копируй дословно — перескажи \
живее, своими словами, как рассказ практикующего трейдера, сохранив ВСЕ \
факты, цифры и термины автора без изменений.
- Не придумывай факты, цифры, уровни и детали сверх исходного текста и \
изображения.
- Если для какого-то поля в исходнике нет данных — напиши "—", не \
оставляй поле пустым и не выдумывай значение.
- Названия полей и структуру шаблона (включая "Инструмент:", "Итог дня:" \
и остальные) пиши ровно как в шаблоне выше, по-русски.
- Только обычный текст, без Markdown и HTML-разметки.
- Без хэштегов, приветствий, подписей и дисклеймеров — только сам пост.
- В ответе — только текст поста, без пояснений от себя.
- Если к заметке приложено изображение — используй его только для \
уточнения фактов о сделке (уровни, цифры на графике). Не выдумывай то, \
чего нет ни в заметке, ни на изображении.
- Длина готового поста — до 3 500 символов.

Пример нужного формата (факты примера не переноси в ответ, \
ориентируйся только на структуру и стиль):

Инструмент: EUR/USD
Итог дня: -80$
Количество сделок: 2
Новости: нет
Психологические ошибки: пересидел вторую сделку в надежде на отскок

Контекст:
День был во флэте, крупных уровней сверху не трогали, торговал внутри \
диапазона последних двух дней.

Лонг.
Почему вошёл: отбой от нижней границы диапазона, подтверждение объёмом.
Стоп: 1.0835.
Тейк: 1.0880.
Результат: -15 пунктов.
Что получилось / ошибка: сетап был верный, но вошёл раньше подтверждения \
на M1.

Шорт.
Почему вошёл: ложный пробой диапазона сверху.
Стоп: 1.0890.
Тейк: 1.0850.
Результат: +20 пунктов.
Что получилось / ошибка: чисто по плану, без нареканий.
"""


def build_user_content(raw_comment: str, image_bytes: bytes | None = None):
    """Добавить к исходной заметке скриншот сделки, если он есть."""
    if image_bytes is None:
        return raw_comment

    image_data = base64.b64encode(image_bytes).decode("ascii")
    return [
        {"type": "text", "text": raw_comment},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
        },
    ]


GEMINI_TIMEOUT_SECONDS = 45


def get_completion(messages: list[dict]) -> str:
    """Получить непустой текст от модели или явно сообщить об ошибке.

    Явный таймаут обязателен: без него при сетевых сбоях запрос может
    зависнуть на неопределённое время (бот будет висеть на "Оформляю
    пост…" сколько угодно), вместо того чтобы упасть с понятной ошибкой,
    которую уже обрабатывает вызывающий код.
    """
    response = gemini_client.chat.completions.create(
        model=GEMINI_MODEL,
        max_tokens=2048,
        messages=messages,
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("Gemini вернул пустой текст")
    return content.strip()


def format_post(raw_comment: str, image_bytes: bytes | None = None) -> str:
    """Первое форматирование сырого комментария в пост."""
    return get_completion(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_content(raw_comment, image_bytes)},
        ]
    )


def revise_post(
    raw_comment: str,
    current_text: str,
    instruction: str,
    image_bytes: bytes | None = None,
) -> str:
    """Переделать уже готовый черновик по правке от админа."""
    user_message = (
        f"Исходная заметка автора (это источник фактов):\n\n{raw_comment}\n\n"
        f"Текущий вариант поста:\n\n{current_text}\n\n"
        f"Внеси следующую правку и выведи полностью обновлённый пост целиком:\n"
        f"{instruction}\n\n"
        "Не теряй факты из исходной заметки, даже если их нет в текущем варианте."
    )
    return get_completion(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_content(user_message, image_bytes)},
        ]
    )


# --- Недельные/месячные отчёты ---------------------------------------------
#
# Вся арифметика (суммы, winrate, разбивка по инструментам) считается ниже
# чистыми функциями на Python. Gemini дальше получает эти числа уже
# готовыми и ничего не пересчитывает — та же дисциплина, что и в
# SYSTEM_PROMPT ("не придумывай факты"), только теперь и для цифр.

REPORT_SYSTEM_PROMPT = """\
Ты — трейдер, который раз в неделю/месяц подводит итог в своём \
Telegram-канале тем же живым, увлекательным языком практикующего трейдера, \
что и в обычных постах — не сухим отчётом и не канцеляритом.

Тебе присылают ГОТОВЫЕ, уже посчитанные цифры за период и сырой список \
заметок об ошибках. Твоя единственная задача с числами — вставить их в \
пост БЕЗ ИЗМЕНЕНИЙ, ничего не пересчитывая и не округляя по-своему. Твоя \
единственная задача с заметками об ошибках — проанализировать их и назвать \
ОДНУ главную повторяющуюся ошибку периода (или честно написать, что \
чёткого паттерна нет, если заметок мало или они все разные).

Оформи пост по такой структуре:

[Заголовок периода, например "Итоги недели" или "Итоги месяца"], [даты периода].

Итог за период: [сумма из данных].
Сделок: [число из данных].
Winrate: [процент из данных, или "недостаточно данных", если он не посчитан].

По инструментам:
[по каждому инструменту из данных — одна строка: название, чистый \
результат, и коротко хорошо/плохо шло дело]

Главная ошибка периода:
[1-2 абзаца — твой анализ по заметкам: назови ОДНУ повторяющуюся ошибку и \
коротко разверни, в чём она проявлялась. Если заметок недостаточно, честно \
скажи об этом, а не выдумывай.]

[Финал — 1-2 предложения вывода/мотивации на следующий период, можно с \
одним уместным эмодзи.]

Правила:
- Числа (суммы, число сделок, winrate, по инструментам) бери СТРОГО из \
переданных данных — никогда не пересчитывай, не округляй иначе и не \
выдумывай ни одной цифры сверх того, что дано.
- Анализ главной ошибки — единственное место, где ты рассуждаешь сам, но \
только по переданным заметкам, не придумывая ошибок, которых там нет.
- Только обычный текст, без Markdown и HTML-разметки.
- По-русски.
- Без хэштегов, приветствий, подписей и дисклеймеров — только сам пост.
- В ответе — только текст поста, без пояснений от себя.
- Длина готового поста — до 3 500 символов.
"""

NO_MISTAKES_PLACEHOLDER = "За период психологических ошибок и ошибок по сделкам не отмечено."


def aggregate_report_stats(sessions: list[dict], start_date: str, end_date: str) -> dict:
    """Посчитать статистику за период из списка сессий (формат — как
    возвращает get_sessions_in_range). Вся арифметика — здесь; дальше эти
    числа передаются в Gemini уже готовыми."""
    trade_count = 0
    wins = losses = breakevens = unparseable_trades = 0
    total_result = 0.0
    unparseable_day_results = 0
    instruments: dict[str, dict] = {}

    for session in sessions:
        stat = instruments.setdefault(
            session["instrument"],
            {
                "trade_count": 0,
                "wins": 0,
                "losses": 0,
                "breakevens": 0,
                "unparseable": 0,
                "net_result": 0.0,
                "parsed_day_count": 0,
            },
        )

        if session["day_result_value"] is None:
            unparseable_day_results += 1
        else:
            total_result += session["day_result_value"]
            stat["net_result"] += session["day_result_value"]
            stat["parsed_day_count"] += 1

        for trade in session["trades"]:
            trade_count += 1
            stat["trade_count"] += 1
            outcome = classify_trade_outcome(trade["result_value"])
            if outcome == "win":
                wins += 1
                stat["wins"] += 1
            elif outcome == "loss":
                losses += 1
                stat["losses"] += 1
            elif outcome == "breakeven":
                breakevens += 1
                stat["breakevens"] += 1
            else:
                unparseable_trades += 1
                stat["unparseable"] += 1

    winrate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else None

    instrument_stats = {}
    for name, stat in instruments.items():
        denom = stat["wins"] + stat["losses"]
        instrument_winrate = (stat["wins"] / denom * 100) if denom > 0 else None
        # Нет ни одного распознанного дневного итога ИЛИ чистый результат
        # ровно ноль — недостаточно сигнала, чтобы честно назвать "хорошо"
        # или "плохо" (а не наоборот, "недостаточно данных" аккуратно
        # отличается от "0 из-за отсутствия чисел").
        if stat["parsed_day_count"] == 0 or stat["net_result"] == 0:
            verdict = "insufficient_data"
        elif stat["net_result"] > 0:
            verdict = "good"
        else:
            verdict = "bad"
        instrument_stats[name] = {
            "trade_count": stat["trade_count"],
            "wins": stat["wins"],
            "losses": stat["losses"],
            "breakevens": stat["breakevens"],
            "unparseable": stat["unparseable"],
            "net_result": stat["net_result"],
            "winrate": instrument_winrate,
            "verdict": verdict,
        }

    return {
        "start_date": start_date,
        "end_date": end_date,
        "session_count": len(sessions),
        "trade_count": trade_count,
        "total_result": total_result,
        "unparseable_day_results": unparseable_day_results,
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "unparseable_trades": unparseable_trades,
        "winrate": winrate,
        "instruments": instrument_stats,
    }


def collect_mistake_notes(sessions: list[dict]) -> str:
    """Собрать психологические ошибки и ошибки по сделкам за период в один
    текстовый блок с датой/инструментом на каждой строке — сырой материал
    для анализа паттерна в Gemini. Значения вида "нет"/пустые пропускаются."""
    skip_values = {"", "нет", "нету", "-", "—"}
    lines = []
    for session in sessions:
        label = f"{session['session_date']} ({session['instrument']})"
        psych = session["psych_mistakes"].strip()
        if psych.lower() not in skip_values:
            lines.append(f"{label} — психология: {psych}")
        for trade in session["trades"]:
            mistake = trade["mistake"].strip()
            if mistake.lower() not in skip_values:
                lines.append(f"{label} — сделка: {mistake}")
    return "\n".join(lines)


def _format_instrument_line(name: str, stat: dict) -> str:
    winrate_text = f"{stat['winrate']:.0f}%" if stat["winrate"] is not None else "недостаточно данных"
    verdict_text = {
        "good": "хорошо",
        "bad": "плохо",
        "insufficient_data": "недостаточно данных",
    }[stat["verdict"]]
    return (
        f"{name}: {stat['net_result']:+.0f}$ ({stat['trade_count']} сделок, "
        f"winrate {winrate_text}) — {verdict_text}"
    )


def _format_report_user_message(period_label: str, stats: dict, mistake_notes: str) -> str:
    """Чистая сериализация готовых чисел в текст запроса к Gemini —
    отделена от build_report_text, чтобы тестироваться без вызова API."""
    winrate_text = f"{stats['winrate']:.0f}%" if stats["winrate"] is not None else "недостаточно данных"

    lines = [
        f"Период: {period_label} ({stats['start_date']} — {stats['end_date']}).",
        f"Итог за период: {stats['total_result']:+.0f}$.",
        f"Сделок: {stats['trade_count']}.",
        f"Winrate: {winrate_text}.",
        "",
        "По инструментам:",
    ]
    if stats["instruments"]:
        lines.extend(
            _format_instrument_line(name, stat) for name, stat in stats["instruments"].items()
        )
    else:
        lines.append("Сделок за период не было.")

    lines.append("")
    lines.append("Заметки об ошибках за период:")
    lines.append(mistake_notes)

    return "\n".join(lines)


def build_report_text(period_label: str, stats: dict, mistake_notes: str) -> str:
    """Оформить отчёт по готовым цифрам через Gemini (тот же клиент и \
таймаут, что и для обычных постов)."""
    user_message = _format_report_user_message(period_label, stats, mistake_notes)
    return get_completion(
        [
            {"role": "system", "content": REPORT_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
    )


# --- Конец блока отчётов -----------------------------------------------


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_USER_ID


def split_telegram_text(text: str) -> list[str]:
    """Разбить длинный пост, не превышая лимит обычного сообщения Telegram."""
    text = text.strip()
    if not text:
        raise ValueError("Черновик не содержит текста")

    chunks = []
    while len(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
        split_at = max(
            text.rfind("\n", 0, MAX_TELEGRAM_MESSAGE_LENGTH + 1),
            text.rfind(" ", 0, MAX_TELEGRAM_MESSAGE_LENGTH + 1),
        )
        if split_at <= MAX_TELEGRAM_MESSAGE_LENGTH // 2:
            split_at = MAX_TELEGRAM_MESSAGE_LENGTH
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    chunks.append(text)
    return chunks


def week_report_range(today: date) -> tuple[str, str]:
    """Последние 7 календарных дней, не считая сегодня — (today-7, today-1)."""
    start = today - timedelta(days=7)
    end = today - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def month_report_range(today: date) -> tuple[str, str]:
    """Весь предыдущий календарный месяц целиком."""
    first_of_this_month = today.replace(day=1)
    last_day_of_prev_month = first_of_this_month - timedelta(days=1)
    first_day_of_prev_month = last_day_of_prev_month.replace(day=1)
    return first_day_of_prev_month.isoformat(), last_day_of_prev_month.isoformat()


def draft_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{draft_id}"),
            ]
        ]
    )


def report_draft_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Опубликовать", callback_data=f"report_publish:{draft_id}"
                ),
                InlineKeyboardButton("❌ Отмена", callback_data=f"report_cancel:{draft_id}"),
            ]
        ]
    )


def choice_keyboard(options: list[str], callback_prefix: str) -> InlineKeyboardMarkup:
    """Кнопки выбора одного варианта из списка (по 2 в ряд), индекс — в callback_data."""
    buttons = [
        InlineKeyboardButton(text, callback_data=f"{callback_prefix}:{i}")
        for i, text in enumerate(options)
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное меню для действий с черновиком в личном чате с ботом."""
    return ReplyKeyboardMarkup(
        [
            ["▶️ Старт", "✏️ Править"],
            ["⏹ Отмена", "🔄 Заново"],
        ],
        resize_keyboard=True,
    )


async def send_draft(
    message, context: ContextTypes.DEFAULT_TYPE, text: str, photo_file_id: str | None = None
) -> None:
    """Отправить черновик и сохранить его в user_data.

    Если текст помещается в лимит подписи к фото — фото и текст уходят
    одним сообщением (фото с подписью и кнопками). Если пост длиннее лимита
    подписи — Telegram не позволяет уместить его в одно сообщение, тогда
    фото и текст отправляются раздельно, как раньше.
    """
    existing = context.user_data.get("draft")
    if photo_file_id is None:
        photo_file_id = existing["photo_file_id"] if existing else None
    if existing:
        await clear_draft_keyboard(context.bot, message.chat_id, existing)

    draft_id = uuid4().hex
    if len(text) <= MAX_TELEGRAM_CAPTION_LENGTH:
        sent = await message.reply_photo(
            photo=photo_file_id, caption=text, reply_markup=draft_keyboard(draft_id)
        )
    else:
        await message.reply_photo(photo=photo_file_id)
        chunks = split_telegram_text(text)
        for chunk in chunks[:-1]:
            await message.reply_text(chunk)
        sent = await message.reply_text(chunks[-1], reply_markup=draft_keyboard(draft_id))

    context.user_data["draft"] = {
        "photo_file_id": photo_file_id,
        "raw_comment": existing.get("raw_comment") if existing else None,
        "answers": existing.get("answers") if existing else None,
        "trades": existing.get("trades") if existing else None,
        "formatted_text": text,
        "message_id": sent.message_id,
        "id": draft_id,
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Привет! Нажми «▶️ Старт» — я по шагам спрошу детали сделки "
        "(инструмент, итог дня, количество сделок, новости, "
        "психологические ошибки, контекст, а затем по каждой сделке "
        "отдельно), попрошу скриншот и соберу готовый пост.\n\n"
        "Команды:\n"
        "/edit <что поправить> — переделать текущий черновик (можно и просто "
        "написать текст правки следующим сообщением, без команды)\n"
        "/post — опубликовать текущий черновик в канал\n"
        "/stop — отменить текущий опрос или черновик\n"
        "/restart — начать заново\n"
        "/weekreport — отчёт за последние 7 дней\n"
        "/monthreport — отчёт за прошлый календарный месяц\n\n"
        "Отчёты приходят и сами: по субботам в 8:00 — недельный, "
        "1-го числа в 8:00 — месячный.",
        reply_markup=menu_keyboard(),
    )


async def create_draft_from_source(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    raw_comment: str,
    photo_file_id: str,
    answers: dict,
    trades: list[dict],
) -> bool:
    """Собрать черновик из исходной заметки и скриншота сделки.

    answers/trades — структурированные ответы анкеты (не только текст
    raw_comment) — сохраняются в черновик, чтобы при публикации записать их
    в историю (save_session) для недельных/месячных отчётов.
    """
    await message.reply_text("Оформляю пост…")
    try:
        photo = await context.bot.get_file(photo_file_id)
        image_bytes = bytes(await photo.download_as_bytearray())
        formatted_text = await asyncio.to_thread(format_post, raw_comment, image_bytes)
    except Exception:
        logger.exception("Ошибка при подготовке черновика")
        await message.reply_text(
            "Не получилось подготовить черновик. Проверь подключение, GEMINI_API_KEY "
            "и попробуй ещё раз."
        )
        return False

    await send_draft(message, context, formatted_text, photo_file_id)
    context.user_data["draft"]["raw_comment"] = raw_comment
    context.user_data["draft"]["answers"] = answers
    context.user_data["draft"]["trades"] = trades
    await message.reply_text(
        "Если нужно что-то поправить — напиши следующим сообщением или "
        "командой /edit, что изменить. Когда всё устроит — жми «✅ Опубликовать» "
        "или пришли /post. Передумал — /stop уберёт черновик."
    )
    return True


# Шаги опроса-анкеты. Ключ — под ним ответ хранится в user_data["wizard"],
# подсказка — вопрос, который увидит админ.
HEADER_STEPS = [
    ("instrument", "Какой инструмент? (например, EUR/USD)"),
    ("day_result", "Итог дня? (например, -80$ или +150$)"),
    ("trade_count", "Сколько сделок было за сессию? (число от 1 до 20)"),
    ("news", 'Были важные новости? Если нет — напиши "нет".'),
    (
        "psych_mistakes",
        "Были психологические ошибки за сессию? (FOMO, месть рынку, "
        'нарушение риска, пересиживание и т.п.) Если нет — напиши "нет".',
    ),
    (
        "context",
        'Контекст дня/рынка (тренд, диапазон, важные уровни)? '
        'Если добавить нечего — напиши "нет".',
    ),
]

TRADE_STEPS = [
    ("direction", "Направление — лонг или шорт?"),
    ("why", "Почему вошёл? (сигнал / сетап)"),
    ("stop", "Стоп — где?"),
    ("take", "Тейк — где?"),
    ("result", "Результат сделки? (плюс/минус, пункты или $)"),
    ("mistake", "Что получилось / ошибка? Кратко."),
]

MAX_TRADE_COUNT = 20


def parse_trade_count(text: str) -> int | None:
    """Вернуть число сделок из ответа админа или None, если ввод невалиден."""
    text = text.strip()
    if not text.isdigit():
        return None
    value = int(text)
    if not (1 <= value <= MAX_TRADE_COUNT):
        return None
    return value


def parse_direction(text: str) -> str | None:
    """Нормализовать направление сделки или None, если не распознано."""
    normalized = text.strip().lower()
    if normalized in ("лонг", "long", "л"):
        return "Лонг"
    if normalized in ("шорт", "short", "ш"):
        return "Шорт"
    return None


_SIGNED_AMOUNT_PATTERN = re.compile(r"[+-]?\d+(?:[.,]\d+)?")


def parse_signed_amount(text: str) -> float | None:
    """Извлечь знаковое число из свободного текста результата
    ("-80$", "+150 $", "0", "-15 пунктов"). Берёт первое совпадение вида
    [+-]?\\d+(?:[.,]\\d+)? — запятая считается десятичным разделителем.
    Число без явного знака ("80") возвращается как положительное. Если в
    тексте вообще нет числа — None (например, "минус восемьдесят" или "нет")."""
    match = _SIGNED_AMOUNT_PATTERN.search(text)
    if match is None:
        return None
    return float(match.group().replace(",", "."))


def classify_trade_outcome(result_value: float | None) -> str:
    """'win' | 'loss' | 'breakeven' | 'unparseable' — используется для winrate."""
    if result_value is None:
        return "unparseable"
    if result_value > 0:
        return "win"
    if result_value < 0:
        return "loss"
    return "breakeven"


def build_raw_comment(answers: dict, trades: list[dict]) -> str:
    """Собрать ответы анкеты в текстовую заметку — вход для Gemini."""
    lines = [
        f"Инструмент: {answers['instrument']}",
        f"Итог дня: {answers['day_result']}",
        f"Количество сделок: {answers['trade_count']}",
        f"Новости: {answers['news']}",
        f"Психологические ошибки: {answers['psych_mistakes']}",
        f"Контекст: {answers['context']}",
    ]
    for i, trade in enumerate(trades, start=1):
        lines.append("")
        lines.append(f"Сделка {i}:")
        lines.append(f"Направление: {trade['direction']}")
        lines.append(f"Почему вошёл: {trade['why']}")
        lines.append(f"Стоп: {trade['stop']}")
        lines.append(f"Тейк: {trade['take']}")
        lines.append(f"Результат: {trade['result']}")
        lines.append(f"Что получилось / ошибка: {trade['mistake']}")
    return "\n".join(lines)


def start_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Завести пустое состояние опроса — первый шаг всегда просьба скриншота."""
    context.user_data["wizard"] = {
        "stage": "photo",  # "photo" -> "header" -> "trade"
        "header_index": 0,
        "trade_index": 0,
        "trade_field_index": 0,
        "photo_file_id": None,
        "answers": {},
        "trades": [],
        "current_trade": {},
    }


async def ask_current_wizard_step(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать вопрос для текущего шага активного опроса."""
    wizard = context.user_data["wizard"]
    if wizard["stage"] == "photo":
        await message.reply_text("Пришли скриншот сделки (фото).")
        return
    if wizard["stage"] == "header":
        key, prompt = HEADER_STEPS[wizard["header_index"]]
        options = HEADER_CHOICE_OPTIONS.get(key)
        if options:
            await message.reply_text(prompt, reply_markup=choice_keyboard(options, key))
            return
        await message.reply_text(prompt)
        return
    total = wizard["answers"]["trade_count"]
    _, prompt = TRADE_STEPS[wizard["trade_field_index"]]
    await message.reply_text(f"Сделка {wizard['trade_index'] + 1}/{total}. {prompt}")


async def begin_wizard(message, context: ContextTypes.DEFAULT_TYPE, intro_text: str) -> None:
    """Сбросить текущий черновик/опрос и начать анкету заново."""
    draft = context.user_data.pop("draft", None)
    context.user_data.pop("wizard", None)
    if draft:
        await clear_draft_keyboard(context.bot, message.chat_id, draft)
    await message.reply_text(intro_text, reply_markup=menu_keyboard())
    start_wizard(context)
    await ask_current_wizard_step(message, context)


async def finish_wizard(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Опрос закончен — собрать заметку и передать в обычный пайплайн оформления."""
    wizard = context.user_data.pop("wizard")
    raw_comment = build_raw_comment(wizard["answers"], wizard["trades"])
    await create_draft_from_source(
        message, context, raw_comment, wizard["photo_file_id"], wizard["answers"], wizard["trades"]
    )


async def handle_wizard_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработать фото, когда опрос ждёт именно его."""
    wizard = context.user_data["wizard"]
    wizard["photo_file_id"] = update.message.photo[-1].file_id
    wizard["stage"] = "header"
    await ask_current_wizard_step(update.message, context)


async def handle_wizard_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработать текстовый ответ на текущий вопрос опроса."""
    wizard = context.user_data["wizard"]
    message = update.message
    text = (message.text or "").strip()

    if wizard["stage"] == "photo":
        await message.reply_text("Сначала пришли скриншот сделки (фото), не текст.")
        return

    if wizard["stage"] == "header":
        key, _ = HEADER_STEPS[wizard["header_index"]]
        if key == "trade_count":
            value = parse_trade_count(text)
            if value is None:
                await message.reply_text(
                    f"Нужно целое число сделок от 1 до {MAX_TRADE_COUNT}, например 1 или 2."
                )
                return
            wizard["answers"][key] = value
        else:
            wizard["answers"][key] = text

        wizard["header_index"] += 1
        if wizard["header_index"] >= len(HEADER_STEPS):
            wizard["stage"] = "trade"
            wizard["trade_index"] = 0
            wizard["trade_field_index"] = 0
            wizard["current_trade"] = {}
        await ask_current_wizard_step(message, context)
        return

    # wizard["stage"] == "trade"
    key, _ = TRADE_STEPS[wizard["trade_field_index"]]
    if key == "direction":
        direction = parse_direction(text)
        if direction is None:
            await message.reply_text('Напиши "лонг" или "шорт".')
            return
        wizard["current_trade"][key] = direction
    else:
        wizard["current_trade"][key] = text

    wizard["trade_field_index"] += 1
    if wizard["trade_field_index"] >= len(TRADE_STEPS):
        wizard["trades"].append(wizard["current_trade"])
        wizard["current_trade"] = {}
        wizard["trade_index"] += 1
        wizard["trade_field_index"] = 0
        if wizard["trade_index"] >= wizard["answers"]["trade_count"]:
            await finish_wizard(message, context)
            return
    await ask_current_wizard_step(message, context)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    wizard = context.user_data.get("wizard")
    if wizard and wizard["stage"] == "photo":
        await handle_wizard_photo(update, context)
        return

    await update.message.reply_text(
        'Чтобы оформить пост, сначала нажми «▶️ Старт» — я сам попрошу '
        "скриншот в нужный момент."
    )


async def apply_revision(
    message, context: ContextTypes.DEFAULT_TYPE, instruction: str
) -> None:
    """Общая логика правки черновика — используется и обычным текстом, и /edit."""
    draft = context.user_data.get("draft")
    if not draft:
        await message.reply_text('Сначала создай черновик — нажми «▶️ Старт».')
        return

    await message.reply_text("Вношу правку…")
    try:
        photo = await context.bot.get_file(draft["photo_file_id"])
        image_bytes = bytes(await photo.download_as_bytearray())
        revised_text = await asyncio.to_thread(
            revise_post,
            draft["raw_comment"],
            draft["formatted_text"],
            instruction,
            image_bytes,
        )
    except Exception:
        logger.exception("Ошибка при обращении к Gemini API")
        await message.reply_text(
            "Не получилось обратиться к Gemini API. Попробуй ещё раз."
        )
        return

    await send_draft(message, context, revised_text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    text = update.message.text
    if text == "▶️ Старт":
        await begin_wizard(update.message, context, "Начинаем оформление поста по шаблону.")
        return
    if text == "✏️ Править":
        if context.user_data.get("draft"):
            await update.message.reply_text("Напиши следующим сообщением, что изменить в черновике.")
        else:
            await update.message.reply_text('Сначала создай черновик — нажми «▶️ Старт».')
        return
    if text == "⏹ Отмена":
        await stop_draft(update.message, context)
        return
    if text == "🔄 Заново":
        await begin_wizard(update.message, context, "Черновик сброшен. Начинаем заново.")
        return

    if context.user_data.get("wizard"):
        await handle_wizard_answer(update, context)
        return

    if context.user_data.get("draft"):
        await apply_revision(update.message, context, text)
        return

    await update.message.reply_text('Нажми «▶️ Старт», чтобы начать оформление поста.')


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    instruction = " ".join(context.args)
    if not instruction:
        await update.message.reply_text(
            "Напиши, что поправить, после команды, например:\n"
            "/edit сократи первый абзац"
        )
        return

    await apply_revision(update.message, context, instruction)


async def publish_draft(bot, draft: dict) -> None:
    """Отправить черновик в канал.

    Короткий пост уходит одним сообщением — фото с подписью. Длинный пост
    (больше лимита подписи Telegram) отправляется как раньше: фото, а следом
    текст отдельными сообщениями.
    """
    text = draft["formatted_text"]
    if len(text) <= MAX_TELEGRAM_CAPTION_LENGTH:
        await bot.send_photo(chat_id=CHANNEL_ID, photo=draft["photo_file_id"], caption=text)
        return

    await bot.send_photo(chat_id=CHANNEL_ID, photo=draft["photo_file_id"])
    for chunk in split_telegram_text(text):
        await bot.send_message(chat_id=CHANNEL_ID, text=chunk)


async def send_report_draft(
    context: ContextTypes.DEFAULT_TYPE, period_label: str, text: str
) -> None:
    """Прислать черновик отчёта админу в личку на подтверждение.

    В отличие от обычного черновика (send_draft) — без фото (у недели/месяца
    нет одного скриншота), поэтому всегда через чанки split_telegram_text, а
    не через photo-caption ветку. Хранится отдельно от дневного
    context.user_data["draft"], чтобы не пересекаться с ним.
    """
    existing = context.user_data.get("report_draft")
    if existing:
        await clear_draft_keyboard(context.bot, ADMIN_USER_ID, existing)

    draft_id = uuid4().hex
    chunks = split_telegram_text(text)
    for chunk in chunks[:-1]:
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=chunk)
    sent = await context.bot.send_message(
        chat_id=ADMIN_USER_ID, text=chunks[-1], reply_markup=report_draft_keyboard(draft_id)
    )

    context.user_data["report_draft"] = {
        "period_label": period_label,
        "formatted_text": text,
        "message_id": sent.message_id,
        "id": draft_id,
    }


async def publish_report(bot, report_draft: dict) -> None:
    """Отправить готовый отчёт в канал (текстом, чанками — как publish_draft,
    но без фото)."""
    for chunk in split_telegram_text(report_draft["formatted_text"]):
        await bot.send_message(chat_id=CHANNEL_ID, text=chunk)


async def clear_draft_keyboard(bot, chat_id: int, draft: dict) -> None:
    """Убрать кнопки под последним сообщением-черновиком (если оно ещё есть)."""
    if not draft.get("message_id"):
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=draft["message_id"], reply_markup=None
        )
    except Exception:
        pass


async def safe_answer(query) -> None:
    """Ответить на нажатие кнопки, не падая, если callback уже устарел.

    Телеграм отклоняет ответ на callback, если бот был выключен и не успел
    его обработать вовремя ("Query is too old..."). Это ожидаемая ситуация,
    а не баг — просто молча пропускаем её, любые другие ошибки пробрасываем.
    """
    try:
        await query.answer()
    except BadRequest as exc:
        if "query is too old" in str(exc).lower() or "query id is invalid" in str(exc).lower():
            logger.info("Пропущен устаревший callback: %s", exc)
        else:
            raise


async def handle_header_choice_selection(
    query, context: ContextTypes.DEFAULT_TYPE, key: str, payload: str
) -> None:
    """Обработать нажатие кнопки-варианта на шаге анкеты (инструмент, итог дня и т.п.)."""
    wizard = context.user_data.get("wizard")
    is_current_step = bool(
        wizard
        and wizard["stage"] == "header"
        and HEADER_STEPS[wizard["header_index"]][0] == key
    )
    if not is_current_step:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Этот выбор уже не актуален.")
        return

    options = HEADER_CHOICE_OPTIONS.get(key, [])
    try:
        value = options[int(payload)]
    except (ValueError, IndexError):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    await query.edit_message_reply_markup(reply_markup=None)

    if key == "news" and value == "Да":
        # Не сохраняем "Да" как ответ — остаёмся на этом же шаге и ждём
        # текстом, что за новость (handle_wizard_answer обработает как обычно).
        await query.message.reply_text(NEWS_FOLLOWUP_PROMPT)
        return

    wizard["answers"][key] = value
    wizard["header_index"] += 1
    await ask_current_wizard_step(query.message, context)


async def handle_report_callback(
    query, context: ContextTypes.DEFAULT_TYPE, action: str, draft_id: str
) -> None:
    """Обработать подтверждение/отмену черновика отчёта — зеркало обычной
    publish/cancel-логики в handle_callback, но для отдельного
    context.user_data["report_draft"]."""
    report_draft = context.user_data.get("report_draft")
    if not report_draft or draft_id != report_draft.get("id"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Этот черновик отчёта уже устарел и не будет использован.")
        return

    if action == "report_publish":
        try:
            await publish_report(context.bot, report_draft)
        except Exception:
            logger.exception("Ошибка при публикации отчёта")
            await query.message.reply_text(
                "Не удалось опубликовать отчёт. Проверь права бота в канале и попробуй ещё раз."
            )
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Отчёт опубликован в канале ✅")
    else:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Отчёт отменён.")

    context.user_data.pop("report_draft", None)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_USER_ID:
        await safe_answer(query)
        return

    await safe_answer(query)
    try:
        action, payload = query.data.split(":", 1)
    except (AttributeError, ValueError):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if action in HEADER_CHOICE_OPTIONS:
        await handle_header_choice_selection(query, context, action, payload)
        return

    if action in ("report_publish", "report_cancel"):
        await handle_report_callback(query, context, action, payload)
        return

    draft = context.user_data.get("draft")
    draft_id = payload
    if not draft or draft_id != draft.get("id"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Этот черновик уже устарел и не будет использован.")
        return

    if action == "publish":
        try:
            await publish_draft(context.bot, draft)
        except Exception:
            logger.exception("Ошибка при публикации поста")
            await query.message.reply_text(
                "Не удалось опубликовать пост. Проверь права бота в канале и попробуй ещё раз."
            )
            return
        persist_published_session(draft)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Опубликовано в канале ✅")
        context.user_data.pop("draft", None)

    elif action == "cancel":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Отменено. Черновик удалён.")
        context.user_data.pop("draft", None)


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    draft = context.user_data.get("draft")
    if not draft:
        await update.message.reply_text('Черновик не найден — нажми «▶️ Старт», чтобы создать пост.')
        return

    try:
        await publish_draft(context.bot, draft)
    except Exception:
        logger.exception("Ошибка при публикации поста")
        await update.message.reply_text(
            "Не удалось опубликовать пост. Проверь права бота в канале и попробуй ещё раз."
        )
        return
    persist_published_session(draft)
    await clear_draft_keyboard(context.bot, update.effective_chat.id, draft)
    await update.message.reply_text("Опубликовано в канале ✅")
    context.user_data.pop("draft", None)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    await stop_draft(update.message, context)


async def stop_draft(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отменить опрос или черновик — вызывается и командой, и кнопкой меню."""
    had_wizard = context.user_data.pop("wizard", None) is not None
    draft = context.user_data.pop("draft", None)
    if not draft:
        text = "Опрос отменён." if had_wizard else "Нечего отменять."
        await message.reply_text(text, reply_markup=menu_keyboard())
        return

    await clear_draft_keyboard(context.bot, message.chat_id, draft)
    await message.reply_text("Отменено. Черновик удалён.", reply_markup=menu_keyboard())


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await begin_wizard(update.message, context, "Черновик сброшен. Начинаем заново.")


async def run_report_pipeline(
    context: ContextTypes.DEFAULT_TYPE, period_label: str, start_date: str, end_date: str
) -> None:
    """Общий пайплайн отчёта: БД -> агрегация -> Gemini -> черновик на
    подтверждение. Используется и планировщиком, и ручными командами."""
    sessions = get_sessions_in_range(start_date, end_date)
    stats = aggregate_report_stats(sessions, start_date, end_date)
    mistake_notes = collect_mistake_notes(sessions) or NO_MISTAKES_PLACEHOLDER

    await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"Готовлю отчёт: {period_label}…")
    try:
        report_text = await asyncio.to_thread(build_report_text, period_label, stats, mistake_notes)
    except Exception:
        logger.exception("Ошибка при подготовке отчёта")
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text="Не получилось подготовить отчёт. Проверь подключение, GEMINI_API_KEY "
            "и попробуй ещё раз.",
        )
        return
    await send_report_draft(context, period_label, report_text)


async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Плановый недельный отчёт — суббота 08:00 (REPORT_TIMEZONE)."""
    today = datetime.now(ZoneInfo(REPORT_TIMEZONE)).date()
    start_date, end_date = week_report_range(today)
    await run_report_pipeline(context, f"Неделя {start_date} — {end_date}", start_date, end_date)


async def monthly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Плановый месячный отчёт — 1-е число, 08:00 (REPORT_TIMEZONE)."""
    today = datetime.now(ZoneInfo(REPORT_TIMEZONE)).date()
    start_date, end_date = month_report_range(today)
    await run_report_pipeline(context, f"Месяц {start_date} — {end_date}", start_date, end_date)


async def weekreport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной запуск недельного отчёта (тот же диапазон, что и по расписанию)."""
    if not is_admin(update):
        return
    today = datetime.now(ZoneInfo(REPORT_TIMEZONE)).date()
    start_date, end_date = week_report_range(today)
    await run_report_pipeline(context, f"Неделя {start_date} — {end_date}", start_date, end_date)


async def monthreport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной запуск месячного отчёта (тот же диапазон, что и по расписанию)."""
    if not is_admin(update):
        return
    today = datetime.now(ZoneInfo(REPORT_TIMEZONE)).date()
    start_date, end_date = month_report_range(today)
    await run_report_pipeline(context, f"Месяц {start_date} — {end_date}", start_date, end_date)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит все необработанные исключения обработчиков, чтобы они не падали молча."""
    logger.error("Необработанная ошибка при обработке %r", update, exc_info=context.error)


def main() -> None:
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        CommandHandler("edit", edit_command, filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("post", post_command, filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("stop", stop_command, filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("restart", restart_command, filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("weekreport", weekreport_command, filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("monthreport", monthreport_command, filters.ChatType.PRIVATE)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo)
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text
        )
    )
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    if application.job_queue is not None:
        tz = ZoneInfo(REPORT_TIMEZONE)
        application.job_queue.run_daily(
            weekly_report_job,
            time=dt_time(hour=8, minute=0, tzinfo=tz),
            days=(6,),  # PTB v20+: 0=воскресенье ... 6=суббота
            chat_id=ADMIN_USER_ID,
            user_id=ADMIN_USER_ID,
            name="weekly_report",
        )
        application.job_queue.run_monthly(
            monthly_report_job,
            when=dt_time(hour=8, minute=0, tzinfo=tz),
            day=1,
            chat_id=ADMIN_USER_ID,
            user_id=ADMIN_USER_ID,
            name="monthly_report",
        )
    else:
        logger.warning(
            "job_queue недоступен — переустанови зависимости "
            '(нужен python-telegram-bot[job-queue]), иначе отчёты по расписанию не будут работать.'
        )

    logger.info("Бот запущен, жду сообщения…")
    application.run_polling()


if __name__ == "__main__":
    main()
