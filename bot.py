"""
Телеграм-бот для канала о трейдинге.

Логика:
1. Ты (админ) отправляешь боту в личку фото сделки с подписью-комментарием.
2. Бот отправляет комментарий в Gemini, тот переписывает его в аккуратный
   пост в стиле канала.
3. Бот присылает тебе черновик поста (с фото) и кнопки "Опубликовать" / "Отмена".
4. Если нужно что-то поправить — просто напиши следующим сообщением (или
   командой /edit <что поправить>), бот перегенерирует черновик и снова
   покажет его на подтверждение.
5. Только после нажатия "Опубликовать" (или команды /post) пост уходит в
   канал. Команда /stop отменяет и удаляет текущий черновик (аналог кнопки
   "Отмена").

Бот реагирует только на сообщения от ADMIN_USER_ID — это твоя защита от того,
что кто-то посторонний напишет боту и что-то опубликует в канал.
"""

import asyncio
import base64
import logging
import os
from uuid import uuid4

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

# Gemini отдаёт OpenAI-совместимый API — просто указываем свой base_url и
# обычный пакет openai работает как обычно.
gemini_client = OpenAI(
    api_key=GEMINI_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

SYSTEM_PROMPT = """\
Ты — трейдер, который ведёт свой Telegram-канал и оформляет посты о \
сессиях по чёткому шаблону.

Тебе присылают сырой комментарий о прошедшей сессии — там может быть \
описана одна или несколько сделок. Оформи его СТРОГО по шаблону ниже, не \
меняя порядок и названия полей:

Инструмент: [тикер/пара]
Итог дня: [общий результат дня, например -100$]
Количество сделок: [число]
Новости: [кратко, если упоминались; если новостей нет — напиши "нет"]

Контекст:
[1 абзац общего контекста дня/рынка — тренд, диапазон, важные уровни, \
логика дня. Бери только то, что есть в заметке; если контекста в ней нет \
— не пиши этот абзац вообще]

Дальше — отдельный блок на КАЖДУЮ сделку из заметки, блоки разделяй \
пустой строкой:

[Лонг/Шорт] от [уровень].
Почему вошёл: [сигнал / сетап].
Стоп: [где].
Тейк: [где].
Результат: [плюс/минус, пункты или $].
Что получилось / ошибка: [кратко].

Правила:
- Направление (Лонг/Шорт) и значения всех полей бери строго из заметки — \
не угадывай и не меняй их.
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

Контекст:
День был во флэте, крупных уровней сверху не трогали, торговал внутри \
диапазона последних двух дней.

Лонг от 1.0850.
Почему вошёл: отбой от нижней границы диапазона, подтверждение объёмом.
Стоп: 1.0835.
Тейк: 1.0880.
Результат: -15 пунктов.
Что получилось / ошибка: сетап был верный, но вошёл раньше подтверждения \
на M1.

Шорт от 1.0875.
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


def get_completion(messages: list[dict]) -> str:
    """Получить непустой текст от модели или явно сообщить об ошибке."""
    response = gemini_client.chat.completions.create(
        model=GEMINI_MODEL,
        max_tokens=2048,
        messages=messages,
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


def draft_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{draft_id}"),
            ]
        ]
    )


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
        "formatted_text": text,
        "message_id": sent.message_id,
        "id": draft_id,
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Привет! Пришли мне фото сделки с подписью-комментарием — я оформлю "
        "пост и покажу тебе черновик перед публикацией в канал.\n\n"
        "Команды:\n"
        "/edit <что поправить> — переделать текущий черновик (можно и просто "
        "написать текст правки следующим сообщением, без команды)\n"
        "/post — опубликовать текущий черновик в канал\n"
        "/stop — отменить и удалить текущий черновик\n"
        "/restart — начать заново",
        reply_markup=menu_keyboard(),
    )


async def create_draft_from_source(
    message, context: ContextTypes.DEFAULT_TYPE, raw_comment: str, photo_file_id: str
) -> bool:
    """Собрать черновик из исходной заметки и скриншота сделки."""
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
    await message.reply_text(
        "Если нужно что-то поправить — напиши следующим сообщением или "
        "командой /edit, что изменить. Когда всё устроит — жми «✅ Опубликовать» "
        "или пришли /post. Передумал — /stop уберёт черновик."
    )
    return True


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    caption = update.message.caption
    photo_file_id = update.message.photo[-1].file_id
    if not caption:
        context.user_data["pending_photo_file_id"] = photo_file_id
        await update.message.reply_text(
            "Скриншот принят. Теперь пришли следующим сообщением заметку о сделке — "
            "из неё я соберу пост. Так можно отправить более длинный текст, чем в подписи к фото."
        )
        return

    context.user_data.pop("pending_photo_file_id", None)
    await create_draft_from_source(update.message, context, caption, photo_file_id)


async def apply_revision(
    message, context: ContextTypes.DEFAULT_TYPE, instruction: str
) -> None:
    """Общая логика правки черновика — используется и обычным текстом, и /edit."""
    draft = context.user_data.get("draft")
    if not draft:
        await message.reply_text(
            "Сначала пришли фото сделки с подписью — тогда я подготовлю пост."
        )
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
        await restart_draft(update.message, context, "Пришли фото сделки с подписью или сначала фото, а затем заметку.")
        return
    if text == "✏️ Править":
        if context.user_data.get("draft"):
            await update.message.reply_text("Напиши следующим сообщением, что изменить в черновике.")
        else:
            await update.message.reply_text("Сначала создай черновик: пришли фото сделки и заметку.")
        return
    if text == "⏹ Отмена":
        await stop_draft(update.message, context)
        return
    if text == "🔄 Заново":
        await restart_draft(update.message, context, "Черновик сброшен. Пришли новое фото сделки и заметку.")
        return

    pending_photo_file_id = context.user_data.get("pending_photo_file_id")
    if pending_photo_file_id and not context.user_data.get("draft"):
        created = await create_draft_from_source(
            update.message, context, update.message.text, pending_photo_file_id
        )
        if created:
            context.user_data.pop("pending_photo_file_id", None)
        return
    await apply_revision(update.message, context, update.message.text)


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


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_USER_ID:
        await safe_answer(query)
        return

    await safe_answer(query)
    draft = context.user_data.get("draft")
    try:
        action, draft_id = query.data.split(":", 1)
    except (AttributeError, ValueError):
        await query.edit_message_reply_markup(reply_markup=None)
        return

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
        await update.message.reply_text(
            "Черновик не найден — сначала пришли фото сделки с подписью."
        )
        return

    try:
        await publish_draft(context.bot, draft)
    except Exception:
        logger.exception("Ошибка при публикации поста")
        await update.message.reply_text(
            "Не удалось опубликовать пост. Проверь права бота в канале и попробуй ещё раз."
        )
        return
    await clear_draft_keyboard(context.bot, update.effective_chat.id, draft)
    await update.message.reply_text("Опубликовано в канале ✅")
    context.user_data.pop("draft", None)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    await stop_draft(update.message, context)


async def stop_draft(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отменить черновик — вызывается и командой, и кнопкой меню."""
    pending_photo = context.user_data.pop("pending_photo_file_id", None)
    draft = context.user_data.pop("draft", None)
    if not draft:
        text = "Ожидание заметки отменено." if pending_photo else "Нечего отменять — черновика нет."
        await message.reply_text(text, reply_markup=menu_keyboard())
        return

    await clear_draft_keyboard(context.bot, message.chat_id, draft)
    await message.reply_text("Отменено. Черновик удалён.", reply_markup=menu_keyboard())


async def restart_draft(message, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Сбросить текущую работу и предложить создать новый пост."""
    context.user_data.pop("pending_photo_file_id", None)
    draft = context.user_data.pop("draft", None)
    if draft:
        await clear_draft_keyboard(context.bot, message.chat_id, draft)
    await message.reply_text(text, reply_markup=menu_keyboard())


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await restart_draft(update.message, context, "Черновик сброшен. Пришли новое фото сделки и заметку.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит все необработанные исключения обработчиков, чтобы они не падали молча."""
    logger.error("Необработанная ошибка при обработке %r", update, exc_info=context.error)


def main() -> None:
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
        MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo)
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text
        )
    )
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    logger.info("Бот запущен, жду сообщения…")
    application.run_polling()


if __name__ == "__main__":
    main()
