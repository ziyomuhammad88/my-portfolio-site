import unittest
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from bot import (
    CHANNEL_ID,
    MAX_TELEGRAM_CAPTION_LENGTH,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    publish_draft,
    safe_answer,
    split_telegram_text,
)


class SplitTelegramTextTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(split_telegram_text("Короткий пост"), ["Короткий пост"])

    def test_long_text_is_split_without_loss(self):
        source = ("Сделка по плану. " * 600).strip()
        chunks = split_telegram_text(source)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= MAX_TELEGRAM_MESSAGE_LENGTH for chunk in chunks))
        self.assertEqual(" ".join(chunks), source)

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            split_telegram_text("   \n")


class SafeAnswerTests(unittest.IsolatedAsyncioTestCase):
    """Регрессия на баг: бот падал с необработанным исключением, если callback
    (нажатие кнопки) устарел, например пока бот был выключен."""

    async def test_swallows_stale_query_error(self):
        query = AsyncMock()
        query.answer.side_effect = BadRequest(
            "Query is too old and response timeout expired or query id is invalid"
        )

        await safe_answer(query)  # не должно поднять исключение

        query.answer.assert_awaited_once()

    async def test_reraises_other_bad_request(self):
        query = AsyncMock()
        query.answer.side_effect = BadRequest("Some other Telegram error")

        with self.assertRaises(BadRequest):
            await safe_answer(query)

    async def test_calls_through_on_success(self):
        query = AsyncMock()

        await safe_answer(query)

        query.answer.assert_awaited_once()


class PublishDraftTests(unittest.IsolatedAsyncioTestCase):
    """Регрессия на баг: фото и текст поста уходили в канал двумя разными
    сообщениями вместо одного (фото с подписью)."""

    async def test_short_text_is_sent_as_single_photo_with_caption(self):
        bot_mock = AsyncMock()
        draft = {"photo_file_id": "file123", "formatted_text": "Короткий пост"}

        await publish_draft(bot_mock, draft)

        bot_mock.send_photo.assert_awaited_once_with(
            chat_id=CHANNEL_ID, photo="file123", caption="Короткий пост"
        )
        bot_mock.send_message.assert_not_awaited()

    async def test_long_text_is_sent_as_photo_then_separate_messages(self):
        bot_mock = AsyncMock()
        long_text = ("Сделка по плану. " * 100).strip()
        self.assertGreater(len(long_text), MAX_TELEGRAM_CAPTION_LENGTH)
        draft = {"photo_file_id": "file123", "formatted_text": long_text}

        await publish_draft(bot_mock, draft)

        bot_mock.send_photo.assert_awaited_once_with(chat_id=CHANNEL_ID, photo="file123")
        bot_mock.send_message.assert_awaited()


if __name__ == "__main__":
    unittest.main()
