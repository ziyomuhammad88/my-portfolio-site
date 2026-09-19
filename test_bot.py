import types
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

from bot import (
    CHANNEL_ID,
    MAX_TELEGRAM_CAPTION_LENGTH,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    MAX_TRADE_COUNT,
    build_raw_comment,
    choice_keyboard,
    handle_header_choice_selection,
    parse_csv_list,
    parse_direction,
    parse_trade_count,
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


class ParseTradeCountTests(unittest.TestCase):
    def test_valid_number_is_parsed(self):
        self.assertEqual(parse_trade_count("2"), 2)

    def test_number_with_surrounding_whitespace_is_parsed(self):
        self.assertEqual(parse_trade_count("  3 \n"), 3)

    def test_non_numeric_text_is_rejected(self):
        self.assertIsNone(parse_trade_count("две"))

    def test_zero_is_rejected(self):
        self.assertIsNone(parse_trade_count("0"))

    def test_too_large_is_rejected(self):
        self.assertIsNone(parse_trade_count(str(MAX_TRADE_COUNT + 1)))

    def test_max_is_accepted(self):
        self.assertEqual(parse_trade_count(str(MAX_TRADE_COUNT)), MAX_TRADE_COUNT)


class ParseDirectionTests(unittest.TestCase):
    def test_recognizes_long_variants(self):
        for text in ["лонг", "Лонг", " ЛОНГ ", "long", "л"]:
            self.assertEqual(parse_direction(text), "Лонг")

    def test_recognizes_short_variants(self):
        for text in ["шорт", "Шорт", "short", "ш"]:
            self.assertEqual(parse_direction(text), "Шорт")

    def test_unrecognized_text_returns_none(self):
        self.assertIsNone(parse_direction("не знаю"))


class BuildRawCommentTests(unittest.TestCase):
    def test_header_fields_are_included(self):
        answers = {
            "instrument": "EUR/USD",
            "day_result": "-80$",
            "trade_count": 1,
            "news": "нет",
            "psych_mistakes": "пересидел сделку",
            "context": "нет",
        }
        trade = {
            "direction": "Лонг",
            "level": "1.0850",
            "why": "отбой от диапазона",
            "stop": "1.0835",
            "take": "1.0880",
            "result": "-15 пунктов",
            "mistake": "рано вошёл",
        }

        comment = build_raw_comment(answers, [trade])

        self.assertIn("Инструмент: EUR/USD", comment)
        self.assertIn("Итог дня: -80$", comment)
        self.assertIn("Количество сделок: 1", comment)
        self.assertIn("Сделка 1:", comment)
        self.assertIn("Направление: Лонг", comment)
        self.assertIn("Что получилось / ошибка: рано вошёл", comment)
        self.assertIn("Психологические ошибки: пересидел сделку", comment)

    def test_multiple_trades_are_all_included_in_order(self):
        answers = {
            "instrument": "AUD/USD",
            "day_result": "-100$",
            "trade_count": 2,
            "news": "нет",
            "psych_mistakes": "нет",
            "context": "нет",
        }
        trades = [
            {
                "direction": "Лонг",
                "level": "0.6500",
                "why": "сигнал 1",
                "stop": "0.6490",
                "take": "0.6520",
                "result": "+10 пунктов",
                "mistake": "без ошибок",
            },
            {
                "direction": "Шорт",
                "level": "0.6530",
                "why": "сигнал 2",
                "stop": "0.6540",
                "take": "0.6500",
                "result": "-5 пунктов",
                "mistake": "рано закрыл",
            },
        ]

        comment = build_raw_comment(answers, trades)

        self.assertLess(comment.index("Сделка 1:"), comment.index("Сделка 2:"))
        self.assertIn("Направление: Шорт", comment)
        self.assertIn("Что получилось / ошибка: рано закрыл", comment)


class ParseCsvListTests(unittest.TestCase):
    def test_splits_and_trims_comma_separated_list(self):
        self.assertEqual(
            parse_csv_list(" EURUSD, GBPUSD ,, XAUUSD "),
            ["EURUSD", "GBPUSD", "XAUUSD"],
        )

    def test_empty_string_gives_empty_list(self):
        self.assertEqual(parse_csv_list(""), [])


class ChoiceKeyboardTests(unittest.TestCase):
    def test_two_buttons_per_row_with_correct_callback_data(self):
        markup = choice_keyboard(["EURUSD", "GBPUSD", "XAUUSD"], "instrument")
        rows = markup.inline_keyboard

        self.assertEqual(len(rows), 2)
        self.assertEqual([b.text for b in rows[0]], ["EURUSD", "GBPUSD"])
        self.assertEqual([b.callback_data for b in rows[0]], ["instrument:0", "instrument:1"])
        self.assertEqual([b.text for b in rows[1]], ["XAUUSD"])
        self.assertEqual(rows[1][0].callback_data, "instrument:2")

    def test_callback_prefix_is_used_for_a_different_field(self):
        markup = choice_keyboard(["-100$", "+100$"], "day_result")
        rows = markup.inline_keyboard

        self.assertEqual([b.callback_data for b in rows[0]], ["day_result:0", "day_result:1"])


def make_wizard_context(**wizard_overrides) -> types.SimpleNamespace:
    wizard = {
        "stage": "header",
        "header_index": 0,
        "trade_index": 0,
        "trade_field_index": 0,
        "photo_file_id": "photo1",
        "answers": {},
        "trades": [],
        "current_trade": {},
    }
    wizard.update(wizard_overrides)
    return types.SimpleNamespace(user_data={"wizard": wizard})


class HandleHeaderChoiceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_selection_stores_answer_and_advances(self):
        context = make_wizard_context()
        query = AsyncMock()
        query.message = AsyncMock()

        with patch("bot.HEADER_CHOICE_OPTIONS", {"instrument": ["EURUSD", "GBPUSD"]}):
            await handle_header_choice_selection(query, context, "instrument", "1")

        wizard = context.user_data["wizard"]
        self.assertEqual(wizard["answers"]["instrument"], "GBPUSD")
        self.assertEqual(wizard["header_index"], 1)
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        query.message.reply_text.assert_awaited_once()

    async def test_works_for_a_different_field_like_day_result(self):
        context = make_wizard_context(header_index=1)  # шаг "day_result" в HEADER_STEPS
        query = AsyncMock()
        query.message = AsyncMock()

        with patch("bot.HEADER_CHOICE_OPTIONS", {"day_result": ["-100$", "+100$"]}):
            await handle_header_choice_selection(query, context, "day_result", "0")

        self.assertEqual(context.user_data["wizard"]["answers"]["day_result"], "-100$")

    async def test_stale_selection_when_no_wizard(self):
        context = types.SimpleNamespace(user_data={})
        query = AsyncMock()
        query.message = AsyncMock()

        await handle_header_choice_selection(query, context, "instrument", "0")

        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        query.message.reply_text.assert_awaited_once()
        self.assertNotIn("wizard", context.user_data)

    async def test_stale_selection_when_step_moved_on(self):
        context = make_wizard_context(header_index=1)  # уже не на шаге "instrument"
        query = AsyncMock()
        query.message = AsyncMock()

        with patch("bot.HEADER_CHOICE_OPTIONS", {"instrument": ["EURUSD", "GBPUSD"]}):
            await handle_header_choice_selection(query, context, "instrument", "0")

        self.assertNotIn("instrument", context.user_data["wizard"]["answers"])
        query.message.reply_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
