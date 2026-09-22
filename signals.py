"""
Strict Signal Contract + market-data DTO (Pillar 1 foundation)
======================================================================
Arbitration is only as good as the contract every strategy must honor. A
Signal that fails `validate()` is rejected before it can influence a trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    FLAT = "flat"

    @property
    def sign(self) -> int:
        return {Direction.BUY: 1, Direction.SELL: -1, Direction.FLAT: 0}[self]


class Regime(str, Enum):
    TREND = "trend"
    RANGE = "range"
    UNKNOWN = "unknown"


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    strategy_id: str
    symbol: str
    direction: Direction
    confidence: float          # normalized 0..1
    entry: float
    sl: float
    tp: float
    timeframe: str
    regime_tag: Regime
    timestamp: datetime
    expiry: datetime

    # ----- contract enforcement ------------------------------------------- #
    def validate(self) -> "Signal":
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"{self.strategy_id}: confidence {self.confidence} not in 0..1")
        if self.entry <= 0 or self.sl <= 0 or self.tp <= 0:
            raise ValueError(f"{self.strategy_id}: non-positive price level")
        if self.expiry <= self.timestamp:
            raise ValueError(f"{self.strategy_id}: expiry must be after timestamp")
        if self.direction == Direction.BUY:
            if not (self.sl < self.entry < self.tp):
                raise ValueError(f"{self.strategy_id}: BUY needs sl < entry < tp")
        elif self.direction == Direction.SELL:
            if not (self.tp < self.entry < self.sl):
                raise ValueError(f"{self.strategy_id}: SELL needs tp < entry < sl")
        else:
            raise ValueError(f"{self.strategy_id}: FLAT cannot be an order signal")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now > self.expiry

    @property
    def risk_per_unit(self) -> float:
        """Price distance from entry to stop — drives position sizing."""
        return abs(self.entry - self.sl)

    @property
    def reward_per_unit(self) -> float:
        return abs(self.tp - self.entry)
