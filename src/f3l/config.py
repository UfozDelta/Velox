"""Safety limits, read from the environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

from .models import D, fmt


class SafetyError(Exception):
    """An order breaks one of the configured safety limits."""


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


@dataclass(frozen=True)
class Settings:
    max_leverage: int = 20
    max_order_usd: Decimal = Decimal("10000")
    confirm_above_usd: Decimal = Decimal("1000")
    default_leverage: int = 1

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        try:
            return cls(
                max_leverage=int(_env("F3L_MAX_LEVERAGE", str(cls.max_leverage))),
                max_order_usd=D(_env("F3L_MAX_ORDER_USD", str(cls.max_order_usd))),
                confirm_above_usd=D(_env("F3L_CONFIRM_ABOVE_USD", str(cls.confirm_above_usd))),
                default_leverage=int(_env("F3L_DEFAULT_LEVERAGE", str(cls.default_leverage))),
            )
        except (ValueError, InvalidOperation) as exc:
            raise SafetyError(f"invalid F3L_* setting in environment: {exc}") from None

    def check(self, total_usd: Decimal, leverage: int | None, max_leverage: int | None = None) -> None:
        """Raise SafetyError if an order (or group of orders) is over a limit."""
        if leverage is not None:
            if leverage < 1:
                raise SafetyError("leverage must be at least 1")
            if leverage > self.max_leverage:
                raise SafetyError(
                    f"leverage {leverage}x is above your limit of {self.max_leverage}x "
                    "(raise F3L_MAX_LEVERAGE to allow it)"
                )
            if max_leverage and leverage > max_leverage:
                raise SafetyError(f"leverage {leverage}x is above the contract maximum of {max_leverage}x")
        if total_usd > self.max_order_usd:
            raise SafetyError(
                f"order value ${fmt(total_usd)} is above your limit of ${fmt(self.max_order_usd)} "
                "(raise F3L_MAX_ORDER_USD to allow it)"
            )

    def needs_confirmation(self, total_usd: Decimal) -> bool:
        return total_usd >= self.confirm_above_usd
