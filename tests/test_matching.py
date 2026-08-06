"""Сопоставление названий между сканами: что считать той же позицией, а что новой.

Запуск: python -m unittest discover -s tests
"""

import unittest

from services.ingest import (
    PROMO_MAX_EXTRA_TOKENS,
    PROMO_SIMILARITY_THRESHOLD,
    find_similar,
    key_of,
)
from services.discovery import is_ignored_market
from services.pricing import is_ignored, parse_price


def same_service(a: str, b: str) -> bool:
    return find_similar(key_of(a), [key_of(b)]) is not None


def same_promo(a: str, b: str) -> bool:
    return (
        find_similar(key_of(a), [key_of(b)], PROMO_SIMILARITY_THRESHOLD, PROMO_MAX_EXTRA_TOKENS)
        is not None
    )


class ServiceMatching(unittest.TestCase):
    def test_different_services_stay_separate(self):
        pairs = [
            ("SMM продвижение", "SEO продвижение"),
            ("Таргетированная реклама ТГ", "Таргетированная реклама ВК"),
            ("Solana Development", "Golang Development"),
            ("Hire ML Developers", "Hire Golang Developers"),
            ("AI Development", "Adaptive AI Development Company"),
            ("Разработка чат-ботов (Стандарт)", "Разработка чат-ботов (Безлимит)"),
            # Тарифы одной услуги — разные позиции с разными ценами.
            ("Разработка чат-ботов — тариф Стандарт", "Разработка чат-ботов — тариф Безлимит"),
            ("Продвижение на Ozon", "Продвижение на Wildberries"),
        ]
        for a, b in pairs:
            with self.subTest(pair=(a, b)):
                self.assertFalse(same_service(a, b))

    def test_rewordings_match(self):
        pairs = [
            ("Разработка сайта", "Разработка сайтов"),
            ("Разработка чат-ботов (Стандарт)", "Разработка чат-ботов"),
            ("Machine Learning Development", "Machine Learning Model Development"),
        ]
        for a, b in pairs:
            with self.subTest(pair=(a, b)):
                self.assertTrue(same_service(a, b))


class PromoMatching(unittest.TestCase):
    def test_reworded_promo_matches(self):
        self.assertTrue(
            same_promo(
                "Бесплатный аудит в подарок",
                "При обсуждении проекта — бесплатный аудит в подарок",
            )
        )
        self.assertTrue(
            same_promo(
                "Личная консультация интернет-маркетолога с опытом более 8 лет",
                "Личная консультация интернет маркетолога с опытом более 8 лет",
            )
        )

    def test_different_promos_stay_separate(self):
        self.assertFalse(
            same_promo(
                "Бесплатный аудит рекламного кабинета",
                "Скидка 45% на дополнительный функционал",
            )
        )


class PriceParsing(unittest.TestCase):
    def test_formatting_does_not_change_price(self):
        self.assertEqual(parse_price("от 18000 руб/мес")["low"], parse_price("от 18 000 руб/мес")["low"])

    def test_currency_and_period(self):
        usd = parse_price("from $2,000 per project")
        self.assertEqual(usd["currency"], "USD")
        self.assertEqual(usd["low"], 2000.0)
        self.assertEqual(usd["period"], "project")

        monthly = parse_price("от 4000 руб/мес")
        self.assertEqual(monthly["currency"], "RUB")
        self.assertEqual(monthly["period"], "month")

    def test_range(self):
        parsed = parse_price("$1,500 - $3,000")
        self.assertEqual((parsed["low"], parsed["high"]), (1500.0, 3000.0))

    def test_text_without_numbers(self):
        self.assertIsNone(parse_price("индивидуальная цена"))


class IgnoredMarkets(unittest.TestCase):
    """Рынки, за которыми не следим: рублёвые цены и сайты в их доменных зонах."""

    def test_rouble_prices_are_ignored(self):
        for text in ("от 4000 руб/мес", "18 000 ₽", "25000 RUB за проект"):
            with self.subTest(price=text):
                self.assertTrue(is_ignored(text))

    def test_other_currencies_stay(self):
        for text in ("from $2,000", "від 15000 грн", "€1500 per project"):
            with self.subTest(price=text):
                self.assertFalse(is_ignored(text))

    def test_domain_zones(self):
        self.assertTrue(is_ignored_market("l7agency.ru"))
        self.assertTrue(is_ignored_market("example.by"))
        self.assertFalse(is_ignored_market("ukr-bot.com"))
        self.assertFalse(is_ignored_market("aetherix.com.ua"))


if __name__ == "__main__":
    unittest.main()
