import unittest

from bot import MAX_TELEGRAM_MESSAGE_LENGTH, split_telegram_text


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


if __name__ == "__main__":
    unittest.main()
