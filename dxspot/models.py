"""Modelos de estado, sesiones, spots y filtros."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .constants import (
    ADS_DEFAULT_QUALITY,
    ADS_WINDOW_SECONDS,
    AMATEUR_BANDS_KHZ,
    BAND_SHORTCUTS,
    DXCC_PREFIX_PATTERN,
    parse_frequency_range,
)
from .countries import CountryMatch


RATE_WINDOW_SECONDS = 10.0
RATE_EMA_ALPHA = 0.025


@dataclass
class SocketStats:
    state: str = "ESPERANDO"
    connected_at: float | None = None
    last_rx: float | None = None
    last_tx: float | None = None
    lines_rx: int = 0
    bytes_rx: int = 0
    lines_tx: int = 0
    bytes_tx: int = 0
    reconnects: int = 0
    last_error: str = ""
    rx_samples: deque[tuple[float, int, int]] = field(default_factory=deque)
    tx_samples: deque[tuple[float, int, int]] = field(default_factory=deque)
    _rx_smoothed_lines: float = 0
    _rx_smoothed_bytes: float = 0
    _rx_rate_second: int | None = None
    _tx_smoothed_lines: float = 0
    _tx_smoothed_bytes: float = 0
    _tx_rate_second: int | None = None

    def record_rx(self, byte_count: int, *, line: bool = False) -> None:
        now = time.monotonic()
        self.last_rx = now
        self.bytes_rx += byte_count
        self.lines_rx += int(line)
        self.rx_samples.append((now, byte_count, int(line)))

    def record_tx(self, byte_count: int, *, line: bool = False) -> None:
        now = time.monotonic()
        self.last_tx = now
        self.bytes_tx += byte_count
        self.lines_tx += int(line)
        self.tx_samples.append((now, byte_count, int(line)))

    @staticmethod
    def _prune(
        samples: deque[tuple[float, int, int]],
        now: float,
    ) -> None:
        while samples and now - samples[0][0] > RATE_WINDOW_SECONDS:
            samples.popleft()

    @staticmethod
    def _smoothed_rate(
        samples: deque[tuple[float, int, int]],
        now: float,
        previous_lines: float,
        previous_bytes: float,
        previous_second: int | None,
    ) -> tuple[float, float, int]:
        current_second = int(now)
        if previous_second == current_second:
            return previous_lines, previous_bytes, current_second

        raw_lines = (
            sum(lines for _, _, lines in samples)
            * 60
            / RATE_WINDOW_SECONDS
        )
        raw_bytes = (
            sum(size for _, size, _ in samples)
            / RATE_WINDOW_SECONDS
        )
        if previous_second is None:
            smoothed_lines = raw_lines
            smoothed_bytes = raw_bytes
        else:
            elapsed_seconds = max(1, current_second - previous_second)
            effective_alpha = 1 - (
                (1 - RATE_EMA_ALPHA) ** elapsed_seconds
            )
            smoothed_lines = (
                previous_lines
                + effective_alpha * (raw_lines - previous_lines)
            )
            smoothed_bytes = (
                previous_bytes
                + effective_alpha * (raw_bytes - previous_bytes)
            )
        if raw_lines == 0 and smoothed_lines < 1:
            smoothed_lines = 0
        if raw_bytes == 0 and smoothed_bytes < 1:
            smoothed_bytes = 0
        return smoothed_lines, smoothed_bytes, current_second

    def rx_rate(self) -> tuple[int, float]:
        now = time.monotonic()
        self._prune(self.rx_samples, now)
        (
            self._rx_smoothed_lines,
            self._rx_smoothed_bytes,
            self._rx_rate_second,
        ) = self._smoothed_rate(
            self.rx_samples,
            now,
            self._rx_smoothed_lines,
            self._rx_smoothed_bytes,
            self._rx_rate_second,
        )
        return round(self._rx_smoothed_lines), self._rx_smoothed_bytes

    def tx_rate(self) -> tuple[int, float]:
        now = time.monotonic()
        self._prune(self.tx_samples, now)
        (
            self._tx_smoothed_lines,
            self._tx_smoothed_bytes,
            self._tx_rate_second,
        ) = self._smoothed_rate(
            self.tx_samples,
            now,
            self._tx_smoothed_lines,
            self._tx_smoothed_bytes,
            self._tx_rate_second,
        )
        return round(self._tx_smoothed_lines), self._tx_smoothed_bytes


@dataclass(frozen=True)
class SpotRecord:
    received_at: datetime
    source_key: str
    line: str
    spotter: str
    dx: str
    frequency_khz: float
    band: str | None
    spotter_country: CountryMatch | None
    dx_country: CountryMatch | None
    beacon: bool = False


@dataclass(frozen=True)
class SpotPredicate:
    subject: str
    field: str
    values: frozenset[int | str]

    def command_text(self) -> str:
        if self.subject == "on":
            if self.field == "frequency":
                ordered_ranges = sorted(
                    (str(value) for value in self.values),
                    key=lambda value: parse_frequency_range(value) or (0, 0),
                )
                return f"on freq {','.join(ordered_ranges)}"
            else:
                shortcut = next(
                    (
                        name
                        for name, bands in BAND_SHORTCUTS.items()
                        if self.values == bands
                    ),
                    None,
                )
                if shortcut is not None:
                    band_text = shortcut
                else:
                    ordered_values = (
                        band
                        for band in AMATEUR_BANDS_KHZ
                        if band in self.values
                    )
                    band_text = ",".join(ordered_values)
                return f"on {band_text}"
        ordered_values = sorted(
            self.values,
            key=lambda value: (
                isinstance(value, str),
                str(value),
            ),
        )
        subject = "by " if self.subject == "by" else ""
        return (
            f"{subject}{self.field} "
            f"{','.join(str(value) for value in ordered_values)}"
        )

    def matches(self, spot: SpotRecord) -> bool:
        if self.subject == "on" and self.field == "frequency":
            ranges = (
                parse_frequency_range(str(value))
                for value in self.values
            )
            return any(
                frequency_range is not None
                and frequency_range[0]
                <= spot.frequency_khz
                <= frequency_range[1]
                for frequency_range in ranges
            )
        if self.subject == "on" and self.field == "band":
            actual_value: int | str | None = spot.band
        else:
            country = (
                spot.spotter_country
                if self.subject == "by"
                else spot.dx_country
            )
            if country is None:
                actual_value = None
            elif self.field == "cq":
                actual_value = country.cq_zone
            elif self.field == "dxcc":
                actual_value = country.dxcc_prefix
            else:
                actual_value = None
        return actual_value in self.values

    def to_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "field": self.field,
            "values": sorted(
                self.values,
                key=lambda value: (
                    isinstance(value, str),
                    str(value),
                ),
            ),
        }

    @classmethod
    def from_dict(cls, raw: object) -> SpotPredicate | None:
        if not isinstance(raw, dict):
            return None
        subject = raw.get("subject")
        field_name = raw.get("field")
        values_raw = raw.get("values")
        if (
            not isinstance(subject, str)
            or not isinstance(field_name, str)
            or not isinstance(values_raw, list)
            or not values_raw
        ):
            return None
        if subject in ("by", "dx") and field_name == "cq":
            valid = all(
                type(value) is int and 1 <= value <= 40
                for value in values_raw
            )
        elif subject in ("by", "dx") and field_name == "dxcc":
            valid = all(
                isinstance(value, str)
                and DXCC_PREFIX_PATTERN.fullmatch(value) is not None
                for value in values_raw
            )
        elif subject == "on" and field_name == "band":
            valid = all(
                isinstance(value, str) and value in AMATEUR_BANDS_KHZ
                for value in values_raw
            )
        elif subject == "on" and field_name == "frequency":
            valid = all(
                isinstance(value, str)
                and parse_frequency_range(value) is not None
                for value in values_raw
            )
        else:
            valid = False
        if not valid:
            return None
        return cls(
            subject=subject,
            field=field_name,
            values=frozenset(values_raw),
        )


@dataclass(frozen=True)
class FilterExpression:
    operator: str
    predicate: SpotPredicate | None = None
    children: tuple[FilterExpression, ...] = ()

    @classmethod
    def condition(cls, predicate: SpotPredicate) -> FilterExpression:
        return cls(operator="predicate", predicate=predicate)

    @classmethod
    def negate(cls, expression: FilterExpression) -> FilterExpression:
        return cls(operator="not", children=(expression,))

    @classmethod
    def combine(
        cls,
        operator: str,
        left: FilterExpression,
        right: FilterExpression,
    ) -> FilterExpression:
        return cls(operator=operator, children=(left, right))

    @property
    def precedence(self) -> int:
        return {
            "or": 1,
            "and": 2,
            "not": 3,
            "predicate": 4,
        }.get(self.operator, 0)

    def command_text(self, parent_precedence: int = 0) -> str:
        if self.operator == "predicate" and self.predicate is not None:
            text = self.predicate.command_text()
        elif self.operator == "not" and len(self.children) == 1:
            text = (
                "not "
                + self.children[0].command_text(self.precedence)
            )
        elif self.operator in ("and", "or") and len(self.children) >= 2:
            text = f" {self.operator} ".join(
                child.command_text(self.precedence)
                for child in self.children
            )
        else:
            return ""
        if self.precedence < parent_precedence:
            return f"({text})"
        return text

    def matches(self, spot: SpotRecord) -> bool:
        if self.operator == "predicate" and self.predicate is not None:
            return self.predicate.matches(spot)
        if self.operator == "not" and len(self.children) == 1:
            return not self.children[0].matches(spot)
        if self.operator == "and" and len(self.children) >= 2:
            return all(child.matches(spot) for child in self.children)
        if self.operator == "or" and len(self.children) >= 2:
            return any(child.matches(spot) for child in self.children)
        return False

    def to_dict(self) -> dict[str, Any]:
        if self.operator == "predicate" and self.predicate is not None:
            return {
                "operator": "predicate",
                "predicate": self.predicate.to_dict(),
            }
        return {
            "operator": self.operator,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, raw: object) -> FilterExpression | None:
        if not isinstance(raw, dict):
            return None
        operator = raw.get("operator")
        if operator == "predicate":
            predicate = SpotPredicate.from_dict(raw.get("predicate"))
            return cls.condition(predicate) if predicate is not None else None
        children_raw = raw.get("children")
        if not isinstance(children_raw, list):
            return None
        children = tuple(
            expression
            for child_raw in children_raw
            if (expression := cls.from_dict(child_raw)) is not None
        )
        if len(children) != len(children_raw):
            return None
        if operator == "not" and len(children) == 1:
            return cls(operator="not", children=children)
        if operator in ("and", "or") and len(children) >= 2:
            return cls(operator=operator, children=children)
        return None


@dataclass(frozen=True)
class SpotFilter:
    number: int
    reject: bool
    expression: FilterExpression

    def command_text(self) -> str:
        return (
            f"dxa rej/spot {self.number} "
            f"{self.expression.command_text()}"
        )

    def rejects(self, spot: SpotRecord) -> bool:
        return self.reject and self.expression.matches(spot)


@dataclass(frozen=True)
class ClientMessage:
    payload: bytes
    pause_after_seconds: float = 0
    count_as_delivery: bool = False


@dataclass
class AdsGroup:
    identifier: int
    dx: str
    frequency_khz: float
    payload: bytes
    spotters: set[str]
    timer: asyncio.TimerHandle | None = None


@dataclass
class ClientSession:
    identifier: int
    peer: str
    writer: asyncio.StreamWriter
    queue: asyncio.Queue[bytes | ClientMessage | None]
    callsign: str = ""
    language: str = "en"
    enabled_sources: set[str] = field(
        default_factory=lambda: {"dxcluster"}
    )
    spot_filters: dict[int, SpotFilter] = field(default_factory=dict)
    filter_all_sources: bool = False
    ads_enabled: bool = False
    ads_window_seconds: float = ADS_WINDOW_SECONDS
    ads_min_spotters: int = ADS_DEFAULT_QUALITY
    beacon_enabled: bool = True
    seeme_enabled: bool = False
    ads_groups: dict[int, AdsGroup] = field(default_factory=dict)
    input_stream: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=200)
    )
    output_stream: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=200)
    )
    delivery_rate_history: deque[tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=600)
    )
    next_ads_group_identifier: int = 1
    connected_at: float = field(default_factory=time.monotonic)
    last_rx: float = field(default_factory=time.monotonic)
    stats: SocketStats = field(default_factory=SocketStats)
    dxspot_writer: asyncio.StreamWriter | None = None
    dxspot_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dxspot_stats: SocketStats = field(default_factory=SocketStats)
