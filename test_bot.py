import unittest
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from bot import MAX_TELEGRAM_MESSAGE_LENGTH, safe_answer, split_telegram_text


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


if __name__ == "__main__":
    unittest.main()
