import tempfile
import types
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

import bot
from bot import (
    CHANNEL_ID,
    GEMINI_TIMEOUT_SECONDS,
    MAX_TELEGRAM_CAPTION_LENGTH,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    MAX_TRADE_COUNT,
    NO_MISTAKES_PLACEHOLDER,
    aggregate_report_stats,
    build_raw_comment,
    choice_keyboard,
    classify_trade_outcome,
    collect_mistake_notes,
    get_completion,
    get_sessions_in_range,
    handle_header_choice_selection,
    init_db,
    month_report_range,
    parse_csv_list,
    parse_direction,
    parse_signed_amount,
    parse_trade_count,
    publish_draft,
    publish_report,
    _format_report_user_message,
    safe_answer,
    save_session,
    send_draft,
    split_telegram_text,
    week_report_range,
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
                "why": "сигнал 1",
                "stop": "0.6490",
                "take": "0.6520",
                "result": "+10 пунктов",
                "mistake": "без ошибок",
            },
            {
                "direction": "Шорт",
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

    async def test_news_no_stores_answer_and_advances(self):
        context = make_wizard_context(header_index=3)  # шаг "news" в HEADER_STEPS
        query = AsyncMock()
        query.message = AsyncMock()

        with patch("bot.HEADER_CHOICE_OPTIONS", {"news": bot.NEWS_OPTIONS}):
            await handle_header_choice_selection(query, context, "news", "0")  # "Нет"

        wizard = context.user_data["wizard"]
        self.assertEqual(wizard["answers"]["news"], "Нет")
        self.assertEqual(wizard["header_index"], 4)

    async def test_news_yes_asks_followup_instead_of_advancing(self):
        context = make_wizard_context(header_index=3)  # шаг "news" в HEADER_STEPS
        query = AsyncMock()
        query.message = AsyncMock()

        with patch("bot.HEADER_CHOICE_OPTIONS", {"news": bot.NEWS_OPTIONS}):
            await handle_header_choice_selection(query, context, "news", "1")  # "Да"

        wizard = context.user_data["wizard"]
        self.assertNotIn("news", wizard["answers"])
        self.assertEqual(wizard["header_index"], 3)  # шаг не сдвинулся
        query.message.reply_text.assert_awaited_once_with(bot.NEWS_FOLLOWUP_PROMPT)

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


class GetCompletionTests(unittest.TestCase):
    """Регрессия на баг: без таймаута зависший запрос к Gemini держал бота
    на "Оформляю пост…" неопределённо долго вместо понятной ошибки."""

    def _fake_response(self, content: str):
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        )

    def test_passes_a_timeout_to_the_api_call(self):
        with patch.object(
            bot.gemini_client.chat.completions,
            "create",
            return_value=self._fake_response("  готовый пост  "),
        ) as create_mock:
            result = get_completion([{"role": "user", "content": "hi"}])

        self.assertEqual(result, "готовый пост")
        self.assertEqual(create_mock.call_args.kwargs["timeout"], GEMINI_TIMEOUT_SECONDS)

    def test_empty_content_raises_value_error(self):
        with patch.object(
            bot.gemini_client.chat.completions, "create", return_value=self._fake_response("   ")
        ):
            with self.assertRaises(ValueError):
                get_completion([{"role": "user", "content": "hi"}])


class ParseSignedAmountTests(unittest.TestCase):
    def test_negative_dollar_amount(self):
        self.assertEqual(parse_signed_amount("-80$"), -80.0)

    def test_positive_dollar_amount_with_space(self):
        self.assertEqual(parse_signed_amount("+150 $"), 150.0)

    def test_unsigned_number_is_positive(self):
        self.assertEqual(parse_signed_amount("100"), 100.0)

    def test_zero(self):
        self.assertEqual(parse_signed_amount("0$"), 0.0)

    def test_points_suffix(self):
        self.assertEqual(parse_signed_amount("-15 пунктов"), -15.0)

    def test_comma_decimal(self):
        self.assertEqual(parse_signed_amount("-80,5$"), -80.5)

    def test_no_number_returns_none(self):
        self.assertIsNone(parse_signed_amount("нет"))

    def test_word_based_sign_unsupported(self):
        self.assertIsNone(parse_signed_amount("минус восемьдесят"))


class ClassifyTradeOutcomeTests(unittest.TestCase):
    def test_none_is_unparseable(self):
        self.assertEqual(classify_trade_outcome(None), "unparseable")

    def test_positive_is_win(self):
        self.assertEqual(classify_trade_outcome(10.0), "win")

    def test_negative_is_loss(self):
        self.assertEqual(classify_trade_outcome(-10.0), "loss")

    def test_zero_is_breakeven(self):
        self.assertEqual(classify_trade_outcome(0.0), "breakeven")


class WeekReportRangeTests(unittest.TestCase):
    def test_trailing_seven_days_excluding_today(self):
        today = date(2026, 9, 19)  # суббота
        start, end = week_report_range(today)
        self.assertEqual((start, end), ("2026-09-12", "2026-09-18"))


class MonthReportRangeTests(unittest.TestCase):
    def test_previous_calendar_month(self):
        today = date(2026, 9, 1)
        start, end = month_report_range(today)
        self.assertEqual((start, end), ("2026-08-01", "2026-08-31"))

    def test_january_rolls_back_to_previous_december(self):
        today = date(2027, 1, 1)
        start, end = month_report_range(today)
        self.assertEqual((start, end), ("2026-12-01", "2026-12-31"))

    def test_leap_february(self):
        today = date(2028, 3, 1)  # 2028 — високосный год
        start, end = month_report_range(today)
        self.assertEqual((start, end), ("2028-02-01", "2028-02-29"))


def make_session(instrument: str, day_result_value: float | None, trades: list[dict]) -> dict:
    return {"instrument": instrument, "day_result_value": day_result_value, "trades": trades}


def make_trade(result_value: float | None) -> dict:
    return {"result_value": result_value}


class AggregateReportStatsTests(unittest.TestCase):
    def test_empty_period_has_no_zero_division(self):
        stats = aggregate_report_stats([], "2026-09-01", "2026-09-07")

        self.assertEqual(stats["session_count"], 0)
        self.assertEqual(stats["trade_count"], 0)
        self.assertEqual(stats["total_result"], 0.0)
        self.assertIsNone(stats["winrate"])
        self.assertEqual(stats["instruments"], {})

    def test_sums_day_results_and_counts_trades(self):
        sessions = [
            make_session("EURUSD", -80.0, [make_trade(-15.0), make_trade(20.0)]),
            make_session("EURUSD", 50.0, [make_trade(50.0)]),
            make_session("XAUUSD", -30.0, [make_trade(-30.0)]),
        ]

        stats = aggregate_report_stats(sessions, "2026-09-01", "2026-09-07")

        self.assertEqual(stats["session_count"], 3)
        self.assertEqual(stats["trade_count"], 4)
        self.assertEqual(stats["total_result"], -60.0)
        self.assertEqual(stats["wins"], 2)
        self.assertEqual(stats["losses"], 2)
        self.assertAlmostEqual(stats["winrate"], 50.0)

    def test_instrument_verdicts(self):
        sessions = [
            make_session("EURUSD", 100.0, [make_trade(100.0)]),
            make_session("XAUUSD", -50.0, [make_trade(-50.0)]),
            make_session("GBPUSD", None, [make_trade(None)]),
        ]

        stats = aggregate_report_stats(sessions, "2026-09-01", "2026-09-07")

        self.assertEqual(stats["instruments"]["EURUSD"]["verdict"], "good")
        self.assertEqual(stats["instruments"]["XAUUSD"]["verdict"], "bad")
        self.assertEqual(stats["instruments"]["GBPUSD"]["verdict"], "insufficient_data")
        self.assertEqual(stats["unparseable_day_results"], 1)
        self.assertEqual(stats["unparseable_trades"], 1)

    def test_breakeven_and_unparseable_trades_excluded_from_winrate_denominator(self):
        sessions = [make_session("EURUSD", 0.0, [make_trade(0.0), make_trade(10.0)])]

        stats = aggregate_report_stats(sessions, "2026-09-01", "2026-09-07")

        self.assertEqual(stats["breakevens"], 1)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 0)
        self.assertEqual(stats["winrate"], 100.0)


class CollectMistakeNotesTests(unittest.TestCase):
    def test_skips_no_mistake_values_and_includes_real_notes(self):
        sessions = [
            {
                "session_date": "2026-09-01",
                "instrument": "EURUSD",
                "psych_mistakes": "нет",
                "trades": [{"mistake": "нет"}, {"mistake": "Вошёл раньше сигнала"}],
            },
            {
                "session_date": "2026-09-02",
                "instrument": "XAUUSD",
                "psych_mistakes": "Пересидел убыточную сделку",
                "trades": [{"mistake": "нету"}],
            },
        ]

        lines = collect_mistake_notes(sessions).splitlines()

        self.assertEqual(len(lines), 2)
        self.assertIn("Вошёл раньше сигнала", lines[0])
        self.assertIn("Пересидел убыточную сделку", lines[1])

    def test_all_skipped_gives_empty_string(self):
        sessions = [
            {
                "session_date": "2026-09-01",
                "instrument": "EURUSD",
                "psych_mistakes": "нет",
                "trades": [{"mistake": "-"}],
            }
        ]
        self.assertEqual(collect_mistake_notes(sessions), "")


class FormatReportUserMessageTests(unittest.TestCase):
    def test_includes_computed_numbers_unchanged(self):
        stats = {
            "start_date": "2026-09-12",
            "end_date": "2026-09-18",
            "total_result": -60.0,
            "trade_count": 4,
            "winrate": 50.0,
            "instruments": {
                "EURUSD": {
                    "trade_count": 3,
                    "wins": 1,
                    "losses": 1,
                    "breakevens": 1,
                    "unparseable": 0,
                    "net_result": -30.0,
                    "winrate": 50.0,
                    "verdict": "bad",
                },
            },
        }

        message = _format_report_user_message("Неделя", stats, "заметка про FOMO")

        self.assertIn("-60$", message)
        self.assertIn("Сделок: 4", message)
        self.assertIn("50%", message)
        self.assertIn("EURUSD", message)
        self.assertIn("заметка про FOMO", message)

    def test_empty_instruments_says_no_trades(self):
        stats = {
            "start_date": "2026-09-12",
            "end_date": "2026-09-18",
            "total_result": 0.0,
            "trade_count": 0,
            "winrate": None,
            "instruments": {},
        }

        message = _format_report_user_message("Неделя", stats, NO_MISTAKES_PLACEHOLDER)

        self.assertIn("Сделок за период не было", message)
        self.assertIn("недостаточно данных", message)


class BuildReportTextTests(unittest.TestCase):
    def test_calls_gemini_with_report_prompt_and_computed_numbers(self):
        stats = {
            "start_date": "2026-09-12",
            "end_date": "2026-09-18",
            "total_result": -60.0,
            "trade_count": 4,
            "winrate": 50.0,
            "instruments": {},
        }

        with patch("bot.get_completion", return_value="Готовый отчёт") as completion_mock:
            result = bot.build_report_text("Неделя", stats, "заметка")

        self.assertEqual(result, "Готовый отчёт")
        messages = completion_mock.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], bot.REPORT_SYSTEM_PROMPT)
        self.assertIn("-60$", messages[1]["content"])
        self.assertIn("заметка", messages[1]["content"])


class PublishReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_chunked_text_no_photo(self):
        bot_mock = AsyncMock()

        await publish_report(bot_mock, "Итоги недели: всё по плану.")

        bot_mock.send_message.assert_awaited_once_with(
            chat_id=CHANNEL_ID, text="Итоги недели: всё по плану."
        )
        bot_mock.send_photo.assert_not_called()


def make_callback_update(data: str, user_id: int | None = None):
    query = AsyncMock()
    query.data = data
    query.from_user = types.SimpleNamespace(
        id=user_id if user_id is not None else bot.ADMIN_USER_ID
    )
    query.message = AsyncMock()
    update = types.SimpleNamespace(callback_query=query)
    return update, query


class HandleCallbackDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Регрессионный тест: publish:/cancel: по-прежнему маршрутизируются в
    обычную логику дневного черновика (handle_header_choice_selection'у не
    должны попадать посторонние action)."""

    async def test_existing_publish_action_still_routes_to_daily_draft_logic(self):
        update, query = make_callback_update("publish:draft-1")
        draft = {
            "id": "draft-1",
            "photo_file_id": "p",
            "formatted_text": "текст",
            "answers": {},
            "trades": [],
        }
        context = types.SimpleNamespace(user_data={"draft": draft}, bot=AsyncMock())

        with patch("bot.publish_draft", new=AsyncMock()) as publish_mock, patch(
            "bot.persist_published_session"
        ):
            await bot.handle_callback(update, context)

        publish_mock.assert_awaited_once()
        self.assertNotIn("draft", context.user_data)


class RunReportPipelineTests(unittest.IsolatedAsyncioTestCase):
    """Отчёты публикуются сразу, без подтверждения — только уведомления
    админу по ходу."""

    async def test_auto_publishes_and_notifies_admin(self):
        context = types.SimpleNamespace(user_data={}, bot=AsyncMock())

        with patch("bot.get_sessions_in_range", return_value=[]), patch(
            "bot.build_report_text", return_value="Готовый отчёт"
        ), patch("bot.publish_report", new=AsyncMock()) as publish_mock:
            await bot.run_report_pipeline(context, "Неделя", "2026-09-01", "2026-09-07")

        publish_mock.assert_awaited_once_with(context.bot, "Готовый отчёт")
        self.assertGreaterEqual(context.bot.send_message.await_count, 2)

    async def test_gemini_failure_notifies_admin_without_publishing(self):
        context = types.SimpleNamespace(user_data={}, bot=AsyncMock())

        with patch("bot.get_sessions_in_range", return_value=[]), patch(
            "bot.build_report_text", side_effect=RuntimeError("boom")
        ), patch("bot.publish_report", new=AsyncMock()) as publish_mock:
            await bot.run_report_pipeline(context, "Неделя", "2026-09-01", "2026-09-07")

        publish_mock.assert_not_awaited()

    async def test_publish_failure_notifies_admin(self):
        context = types.SimpleNamespace(user_data={}, bot=AsyncMock())

        with patch("bot.get_sessions_in_range", return_value=[]), patch(
            "bot.build_report_text", return_value="Готовый отчёт"
        ), patch("bot.publish_report", new=AsyncMock(side_effect=RuntimeError("no rights"))):
            await bot.run_report_pipeline(context, "Неделя", "2026-09-01", "2026-09-07")

        last_call_text = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Не удалось опубликовать", last_call_text)


class SendDraftPreservesStructuredDataTests(unittest.IsolatedAsyncioTestCase):
    """Регрессия: send_draft пересобирал context.user_data["draft"] с нуля и
    раньше терял answers/trades при повторном вызове (например, при /edit)."""

    async def test_answers_and_trades_survive_a_second_call(self):
        message = AsyncMock()
        message.chat_id = 1
        message.reply_photo = AsyncMock(return_value=types.SimpleNamespace(message_id=42))
        context = types.SimpleNamespace(user_data={}, bot=AsyncMock())

        await send_draft(message, context, "Первый вариант", photo_file_id="photo1")
        context.user_data["draft"]["answers"] = {"instrument": "EURUSD"}
        context.user_data["draft"]["trades"] = [{"direction": "Лонг"}]

        await send_draft(message, context, "Отредактированный вариант")

        self.assertEqual(context.user_data["draft"]["answers"], {"instrument": "EURUSD"})
        self.assertEqual(context.user_data["draft"]["trades"], [{"direction": "Лонг"}])


class SessionDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._patcher = patch.object(bot, "DB_PATH", f"{self._tmp_dir.name}/test_history.db")
        self._patcher.start()
        init_db()

    def tearDown(self):
        self._patcher.stop()
        self._tmp_dir.cleanup()

    def _answers(self, **overrides) -> dict:
        answers = {
            "instrument": "EURUSD",
            "day_result": "-80$",
            "trade_count": 1,
            "news": "нет",
            "psych_mistakes": "нет",
            "context": "флэт",
        }
        answers.update(overrides)
        return answers

    def test_save_and_query_round_trip(self):
        trades = [
            {
                "direction": "Лонг",
                "why": "отбой",
                "stop": "1.0835",
                "take": "1.0880",
                "result": "-15 пунктов",
                "mistake": "рано вошёл",
            }
        ]

        save_session(self._answers(), trades, "2026-09-15")
        sessions = get_sessions_in_range("2026-09-01", "2026-09-30")

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session["instrument"], "EURUSD")
        self.assertEqual(session["day_result_value"], -80.0)
        self.assertEqual(len(session["trades"]), 1)
        self.assertEqual(session["trades"][0]["result_value"], -15.0)
        self.assertEqual(session["trades"][0]["mistake"], "рано вошёл")

    def test_sessions_outside_range_are_excluded(self):
        save_session(self._answers(day_result="0$", trade_count=0), [], "2026-08-15")

        sessions = get_sessions_in_range("2026-09-01", "2026-09-30")

        self.assertEqual(sessions, [])

    def test_trades_ordered_by_trade_index(self):
        trades = [
            {"direction": "Лонг", "why": "1", "stop": "1", "take": "1", "result": "+1", "mistake": "нет"},
            {"direction": "Шорт", "why": "2", "stop": "2", "take": "2", "result": "-1", "mistake": "нет"},
        ]
        save_session(self._answers(day_result="0$", trade_count=2), trades, "2026-09-10")

        sessions = get_sessions_in_range("2026-09-01", "2026-09-30")

        self.assertEqual([t["direction"] for t in sessions[0]["trades"]], ["Лонг", "Шорт"])


if __name__ == "__main__":
    unittest.main()
