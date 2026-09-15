"""What a model call cost, when the deployment has said what a model costs.

A price book is configuration, not code, and not something a provider adapter
knows about. An adapter's job ends at reporting the tokens the vendor returned;
turning tokens into money is a business question with a different lifetime, a
different owner and a different blast radius when it is wrong.

**Keyed by model name.** Not by provider: ``agent_steps`` deliberately records
the model and not the vendor - *"which vendor answered is internal routing and
is deliberately absent from every outward contract"* - and model names are
unique across the vendors this platform speaks to. Keying on the model alone
keeps that boundary intact and still prices every call correctly.

**Versioned, and dated.** A price book carries a ``version`` that is recorded
alongside every figure it produces, and each price carries ``effective_from``.
Prices change; a figure that cannot say which price produced it cannot be
audited, and a book that silently starts answering differently is worse than
one that refuses.

**Decimal, always.** Money is ``Numeric(12, 2)`` everywhere else in this
codebase and ``Decimal`` in Python, for the reason ``app/tools/business/money.py``
gives: binary floating point cannot represent a tenth. Token arithmetic needs
more precision than two decimal places - a thousand tokens at $3 per million is
$0.003 - so figures here are quantised to six.

**An unpriced model costs "unknown", never zero.** A deployment that has
configured no prices reports complete token usage and no cost at all. Zero
would be a claim, and a false one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

# Prices are quoted per million tokens, which is how every vendor publishes
# them - quoting per token would mean a price book full of 0.000003.
TOKENS_PER_UNIT = Decimal(1_000_000)

# Six places. Enough that a single small call is not rounded to nothing, and
# bounded so a total is exact rather than asymptotic.
COST_PRECISION = Decimal("0.000001")

CURRENCY_LENGTH = 3


class PricingError(ValueError):
    """The configured price book cannot be read.

    Raised at startup, where a misconfiguration is a deployment problem
    somebody can fix, rather than at the first request, where it would be an
    outage.
    """


@dataclass(frozen=True)
class ModelPrice:
    """What one model costs, from one date."""

    model: str
    input_per_million: Decimal
    output_per_million: Decimal
    currency: str
    effective_from: datetime

    def __post_init__(self) -> None:
        if not self.model:
            raise PricingError("A price needs a model name.")
        if self.input_per_million < 0 or self.output_per_million < 0:
            raise PricingError(f"{self.model}: a price cannot be negative.")
        if len(self.currency) != CURRENCY_LENGTH or not self.currency.isalpha():
            raise PricingError(f"{self.model}: currency must be a 3-letter code.")


@dataclass(frozen=True)
class Cost:
    """A figure, the currency it is in, and which price book produced it."""

    amount: Decimal
    currency: str
    price_version: str


@dataclass(frozen=True)
class PriceBook:
    """Every price this deployment has been told about.

    Empty by default, and an empty book is a valid one: it reports usage with
    no cost, which is exactly right for a deployment nobody has priced.
    """

    version: str = "unset"
    prices: tuple[ModelPrice, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.prices

    def price_for(self, model: str, at: datetime | None = None) -> ModelPrice | None:
        """The price in force for *model* at *at*, or nothing.

        The most recent price whose ``effective_from`` has passed. A model with
        prices that all start in the future is unpriced *now*, which is the
        honest answer to "what did this cost today".
        """
        moment = at or datetime.now(UTC)
        candidates = [
            price
            for price in self.prices
            if price.model == model and price.effective_from <= moment
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda price: price.effective_from)

    def estimate(
        self,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
        at: datetime | None = None,
    ) -> Cost | None:
        """What one call cost, or nothing if the model has no price.

        Deterministic: the same tokens, model and moment always produce the
        same figure, because the only inputs are integers and a frozen table.
        """
        price = self.price_for(model, at)
        if price is None:
            return None

        amount = (
            Decimal(max(input_tokens, 0)) * price.input_per_million
            + Decimal(max(output_tokens, 0)) * price.output_per_million
        ) / TOKENS_PER_UNIT

        return Cost(
            amount=amount.quantize(COST_PRECISION),
            currency=price.currency,
            price_version=self.version,
        )

    def total(self, costs: Iterable[Cost | None]) -> Cost | None:
        """Add figures up, refusing to add different currencies together.

        ``None`` entries are skipped rather than treated as zero: a total over
        a mix of priced and unpriced calls is the total of the priced ones, and
        the caller is expected to report how many were not priced.
        """
        amount = Decimal(0)
        currency: str | None = None
        counted = 0

        for cost in costs:
            if cost is None:
                continue
            if currency is None:
                currency = cost.currency
            elif cost.currency != currency:
                raise PricingError(
                    f"Cannot add {cost.currency} to {currency}: this platform does "
                    "not convert currencies."
                )
            amount += cost.amount
            counted += 1

        if currency is None or counted == 0:
            return None

        return Cost(
            amount=amount.quantize(COST_PRECISION),
            currency=currency,
            price_version=self.version,
        )

    # -- Configuration ---------------------------------------------------------

    @classmethod
    def from_json(cls, configured: str) -> PriceBook:
        """Build a book from the ``LLM_PRICING`` setting.

        The shape, deliberately explicit so a reviewer can read a deployment's
        prices without consulting this file::

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

        Numbers are quoted as *strings* on purpose. A JSON number is a float by
        the time Python has parsed it, and a price that arrived as a float has
        already lost the property this module exists to preserve.
        """
        text = configured.strip()
        if not text:
            return cls()

        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PricingError(f"LLM_PRICING is not valid JSON: {exc.msg}") from exc

        if not isinstance(document, dict):
            raise PricingError("LLM_PRICING must be an object.")

        version = document.get("version")
        if not isinstance(version, str) or not version.strip():
            raise PricingError("LLM_PRICING needs a non-empty string 'version'.")

        entries = document.get("prices", [])
        if not isinstance(entries, list):
            raise PricingError("LLM_PRICING 'prices' must be a list.")

        prices = tuple(_parse_price(entry, index) for index, entry in enumerate(entries))
        _reject_duplicates(prices)

        return cls(version=version.strip(), prices=prices)


def _parse_price(entry: object, index: int) -> ModelPrice:
    if not isinstance(entry, dict):
        raise PricingError(f"LLM_PRICING price #{index} must be an object.")

    return ModelPrice(
        model=_string(entry, "model", index),
        input_per_million=_decimal(entry, "input_per_million", index),
        output_per_million=_decimal(entry, "output_per_million", index),
        currency=_string(entry, "currency", index).upper(),
        effective_from=_moment(entry, "effective_from", index),
    )


def _string(entry: dict[str, object], key: str, index: int) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PricingError(f"LLM_PRICING price #{index} needs a string {key!r}.")
    return value.strip()


def _decimal(entry: dict[str, object], key: str, index: int) -> Decimal:
    value = entry.get(key)
    if isinstance(value, float):
        raise PricingError(
            f"LLM_PRICING price #{index}: {key!r} must be a quoted string, not a "
            "JSON number - a number has already been through a float by now."
        )
    if not isinstance(value, str | int):
        raise PricingError(f"LLM_PRICING price #{index} needs a {key!r}.")

    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise PricingError(f"LLM_PRICING price #{index}: {key!r} is not a number.") from exc


def _moment(entry: dict[str, object], key: str, index: int) -> datetime:
    value = entry.get(key)
    if not isinstance(value, str):
        raise PricingError(f"LLM_PRICING price #{index} needs an ISO-8601 {key!r}.")

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PricingError(f"LLM_PRICING price #{index}: {key!r} is not ISO-8601.") from exc

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _reject_duplicates(prices: Sequence[ModelPrice]) -> None:
    """One price per model per date.

    Two prices for the same model on the same day would make the resolution
    order decide the bill, and the resolution order is an implementation
    detail nobody should be reading a price book against.
    """
    seen: set[tuple[str, datetime]] = set()
    for price in prices:
        key = (price.model, price.effective_from)
        if key in seen:
            raise PricingError(
                f"LLM_PRICING has two prices for {price.model!r} effective "
                f"{price.effective_from.isoformat()}."
            )
        seen.add(key)
