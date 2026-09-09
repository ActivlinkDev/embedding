import pytest
from fastapi import HTTPException
from routers.basket.currency import basket_currency, currency_guard_filter
from routers.basket.payment import _extract_currency

@pytest.mark.parametrize("items, expected", [
    ([], "GBP"),
    ([{"currency": " gbp "}, {"currency": "GBP"}], "GBP"),
    ([{}, {"currency": None}, {"currency": " "}], "GBP"),
    ([{"currency": "EUR"}, {"currency": "eur"}], "EUR"),
])
def test_single_currency(items, expected):
    assert basket_currency(items) == expected
    assert _extract_currency(items) == expected.lower()

@pytest.mark.parametrize("items", [
    [{"currency": "GBP"}, {"currency": "EUR"}],
    [{"currency": "EUR"}, {}],
    [{"currency": "GBP"}, {"currency": "GBP"}, {"currency": "USD"}],
])
def test_mixed_currency_is_rejected(items):
    for validate in (basket_currency, _extract_currency):
        with pytest.raises(HTTPException) as exc:
            validate(items)
        assert exc.value.status_code == 409
        assert "same currency" in exc.value.detail
