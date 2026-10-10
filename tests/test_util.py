from datetime import date

from app.services.util import adjust_business_day, add_months, fmt_eur, italian_holidays, parse_amount


def test_parse_amount_formats():
    assert parse_amount("1.234,56") == 123456
    assert parse_amount("1,234.56") == 123456
    assert parse_amount("-12,5") == -1250
    assert parse_amount("€ 3") == 300
    assert parse_amount("(45,00)") == -4500
    assert parse_amount("100.00") == 10000
    assert parse_amount("1.234") == 123400
    assert parse_amount("0.125") == 13
    assert parse_amount("12-") == -1200
    assert parse_amount("") is None
    assert parse_amount("abc") is None
    assert parse_amount(12.34) == 1234
    assert parse_amount("1.234", decimal_sep=".") == 123
    assert parse_amount("1.234,5", decimal_sep=",") == 123450


def test_fmt_eur():
    assert fmt_eur(123456) == "1.234,56 €"
    assert fmt_eur(-5) == "-0,05 €"
    assert fmt_eur(100, sign=True) == "+1,00 €"


def test_add_months_clamps_day():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 15), 3, day=6) == date(2027, 2, 6)


def test_business_days():
    assert date(2026, 4, 6) in italian_holidays(2026)  # Pasquetta 2026
    assert adjust_business_day(date(2026, 8, 15), "successivo") == date(2026, 8, 17)
    assert adjust_business_day(date(2026, 11, 1), "precedente") == date(2026, 10, 30)
