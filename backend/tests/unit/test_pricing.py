"""The price book: what a model call cost, and when it will not guess.

Two properties carry this file. The first is arithmetic - the same tokens must
always produce the same figure, in ``Decimal``, to the same precision. The
second is the one that actually matters in a bill: **an unpriced model produces
no cost, never a zero**, and a total over a mix of priced and unpriced calls
says how much of the usage it covers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.observability.pricing import (
    COST_PRECISION,
    Cost,
    ModelPrice,
    PriceBook,
    PricingError,
)

OPUS = "claude-opus-5"
NOW = datetime(2026, 9, 15, tzinfo=UTC)

BOOK_JSON = """
{
  "version": "2026-09-01",
  "prices": [
    {
      "model": "claude-opus-5",
      "input_per_million": "15.00",
      "output_per_million": "75.00",
      "currency": "USD",
      "effective_from": "2026-09-01T00:00:00Z"
    }
  ]
}
"""


def book() -> PriceBook:
    return PriceBook.from_json(BOOK_JSON)


# -- The arithmetic -----------------------------------------------------------


def test_a_known_model_is_priced_exactly() -> None:
    cost = book().estimate(OPUS, input_tokens=1_000, output_tokens=500, at=NOW)

    # 1000 * 15/1e6 + 500 * 75/1e6 = 0.015 + 0.0375
    assert cost is not None
    assert cost.amount == Decimal("0.052500")
    assert cost.currency == "USD"


def test_money_is_never_a_float() -> None:
    """The one property the whole module exists for."""
    cost = book().estimate(OPUS, input_tokens=7, output_tokens=3, at=NOW)

    assert cost is not None
    assert isinstance(cost.amount, Decimal)
    assert not isinstance(cost.amount, float)


def test_input_and_output_are_priced_at_their_own_rates() -> None:
    """Output is five times input in this book; the figures must show it."""
    input_only = book().estimate(OPUS, input_tokens=1_000_000, output_tokens=0, at=NOW)
    output_only = book().estimate(OPUS, input_tokens=0, output_tokens=1_000_000, at=NOW)

    assert input_only is not None
    assert output_only is not None
    assert input_only.amount == Decimal("15.000000")
    assert output_only.amount == Decimal("75.000000")


def test_a_small_call_is_not_rounded_away() -> None:
    """Six places, because two would price a thousand tokens at nothing."""
    cost = book().estimate(OPUS, input_tokens=100, output_tokens=0, at=NOW)

    assert cost is not None
    assert cost.amount == Decimal("0.001500")
    assert cost.amount > 0


def test_the_same_call_always_costs_the_same() -> None:
    first = book().estimate(OPUS, input_tokens=1_234, output_tokens=567, at=NOW)
    second = book().estimate(OPUS, input_tokens=1_234, output_tokens=567, at=NOW)

    assert first == second


def test_zero_tokens_cost_zero_when_the_model_is_priced() -> None:
    """Different from "unknown": the model *is* priced, and nothing was used."""
    cost = book().estimate(OPUS, input_tokens=0, output_tokens=0, at=NOW)

    assert cost is not None
    assert cost.amount == Decimal("0.000000")


def test_negative_token_counts_cannot_produce_a_credit() -> None:
    cost = book().estimate(OPUS, input_tokens=-5_000, output_tokens=-5_000, at=NOW)

    assert cost is not None
    assert cost.amount == Decimal("0.000000")


# -- Not guessing -------------------------------------------------------------


def test_an_unknown_model_has_no_cost_rather_than_a_zero_one() -> None:
    assert book().estimate("some-other-model", input_tokens=1_000, output_tokens=1_000) is None


def test_an_empty_book_prices_nothing_and_is_still_valid() -> None:
    """A deployment nobody has priced reports usage and no money."""
    empty = PriceBook.from_json("")

    assert empty.is_empty
    assert empty.estimate(OPUS, input_tokens=1_000, output_tokens=1_000) is None


def test_a_price_that_starts_later_does_not_apply_yet() -> None:
    later = PriceBook(
        version="future",
        prices=(
            ModelPrice(
                model=OPUS,
                input_per_million=Decimal("15"),
                output_per_million=Decimal("75"),
                currency="USD",
                effective_from=NOW + timedelta(days=30),
            ),
        ),
    )

    assert later.estimate(OPUS, input_tokens=1_000, output_tokens=0, at=NOW) is None


def test_the_most_recent_applicable_price_wins() -> None:
    versioned = PriceBook(
        version="v2",
        prices=(
            ModelPrice(
                model=OPUS,
                input_per_million=Decimal("10"),
                output_per_million=Decimal("10"),
                currency="USD",
                effective_from=NOW - timedelta(days=90),
            ),
            ModelPrice(
                model=OPUS,
                input_per_million=Decimal("20"),
                output_per_million=Decimal("20"),
                currency="USD",
                effective_from=NOW - timedelta(days=1),
            ),
        ),
    )

    cost = versioned.estimate(OPUS, input_tokens=1_000_000, output_tokens=0, at=NOW)

    assert cost is not None
    assert cost.amount == Decimal("20.000000")


def test_a_figure_says_which_book_produced_it() -> None:
    cost = book().estimate(OPUS, input_tokens=10, output_tokens=10, at=NOW)

    assert cost is not None
    assert cost.price_version == "2026-09-01"


# -- Totals -------------------------------------------------------------------


def test_a_total_skips_unpriced_entries_rather_than_counting_them_as_zero() -> None:
    priced = book().estimate(OPUS, input_tokens=1_000, output_tokens=0, at=NOW)

    total = book().total([priced, None, priced])

    assert total is not None
    assert total.amount == Decimal("0.030000")


def test_a_total_of_nothing_is_unknown_rather_than_zero() -> None:
    assert book().total([None, None]) is None
    assert book().total([]) is None


def test_currencies_are_not_silently_added_together() -> None:
    """This platform does not convert, and must not pretend to."""
    dollars = Cost(amount=Decimal("1.00"), currency="USD", price_version="v")
    euros = Cost(amount=Decimal("1.00"), currency="EUR", price_version="v")

    with pytest.raises(PricingError, match="does not convert"):
        book().total([dollars, euros])


# -- Reading the configuration ------------------------------------------------


def test_a_json_number_is_refused_because_it_is_already_a_float() -> None:
    """The reason prices are quoted as strings, enforced rather than documented."""
    with pytest.raises(PricingError, match="quoted string"):
        PriceBook.from_json(
            '{"version":"v","prices":[{"model":"m","input_per_million":15.0,'
            '"output_per_million":"1","currency":"USD",'
            '"effective_from":"2026-01-01T00:00:00Z"}]}'
        )


def test_malformed_json_is_refused() -> None:
    with pytest.raises(PricingError, match="valid JSON"):
        PriceBook.from_json("{oops")


def test_a_book_without_a_version_is_refused() -> None:
    """A figure that cannot say which prices produced it cannot be audited."""
    with pytest.raises(PricingError, match="version"):
        PriceBook.from_json('{"prices": []}')


def test_two_prices_for_one_model_on_one_date_are_refused() -> None:
    entry = (
        '{"model":"m","input_per_million":"1","output_per_million":"1",'
        '"currency":"USD","effective_from":"2026-01-01T00:00:00Z"}'
    )
    with pytest.raises(PricingError, match="two prices"):
        PriceBook.from_json(f'{{"version":"v","prices":[{entry},{entry}]}}')


def test_a_negative_price_is_refused() -> None:
    with pytest.raises(PricingError, match="negative"):
        ModelPrice(
            model="m",
            input_per_million=Decimal("-1"),
            output_per_million=Decimal("1"),
            currency="USD",
            effective_from=NOW,
        )


def test_a_currency_must_be_a_three_letter_code() -> None:
    with pytest.raises(PricingError, match="3-letter"):
        ModelPrice(
            model="m",
            input_per_million=Decimal("1"),
            output_per_million=Decimal("1"),
            currency="dollars",
            effective_from=NOW,
        )


def test_a_naive_effective_date_is_read_as_utc() -> None:
    parsed = PriceBook.from_json(
        '{"version":"v","prices":[{"model":"m","input_per_million":"1",'
        '"output_per_million":"1","currency":"USD",'
        '"effective_from":"2026-01-01T00:00:00"}]}'
    )

    assert parsed.prices[0].effective_from.tzinfo is not None


def test_the_precision_is_the_one_the_module_declares() -> None:
    cost = book().estimate(OPUS, input_tokens=1, output_tokens=0, at=NOW)

    assert cost is not None
    assert cost.amount.as_tuple().exponent == COST_PRECISION.as_tuple().exponent
