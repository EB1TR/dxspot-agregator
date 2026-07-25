"""Interpretación de comandos locales enviados por clientes."""

from __future__ import annotations

import re

from .constants import (
    ADS_DEFAULT_QUALITY,
    ADS_MAX_QUALITY,
    ADS_MAX_WINDOW_SECONDS,
    ADS_MIN_QUALITY,
    ADS_MIN_WINDOW_SECONDS,
    ADS_WINDOW_SECONDS,
    AMATEUR_BANDS_KHZ,
    BAND_SHORTCUTS,
    DXCC_PREFIX_PATTERN,
    parse_frequency_range,
)
from .models import (
    ClientMessage,
    ClientSession,
    FilterExpression,
    SpotFilter,
    SpotPredicate,
)


DXA_PROMPT = "DXA >"
CONFIGURATION_PAUSE_SECONDS = 5
STATUS_LINE_WIDTH = 80
STATUS_SEPARATOR = "-" * STATUS_LINE_WIDTH
CLIENT_TEXT = {
    "es": {
        "connection_established": "Conexión establecida",
        "command_accepted": "Comando aceptado",
        "command_rejected": "Comando no aceptado",
        "goodbye": "Gracias por utilizar DXSpot-Agregator.",
        "ready": "DXA LISTO",
        "status": "DXA ESTADO",
        "sources": "FUENTES",
        "human_spots": "SPOTS HUMANOS",
        "window": "VENTANA",
        "quality": "CALIDAD",
        "options": "OPCIONES",
        "beacon": "BALIZA",
        "filters": "FILTROS",
        "all_sources": "TODAS LAS FUENTES",
        "rbn_only": "SOLO RBN",
        "rule": "REGLA",
        "rules": "REGLAS",
    },
    "en": {
        "connection_established": "Connection established",
        "command_accepted": "Command accepted",
        "command_rejected": "Command rejected",
        "goodbye": "Thank you for using DXSpot-Agregator.",
        "ready": "DXA READY",
        "status": "DXA STATUS",
        "sources": "SOURCES",
        "human_spots": "HUMAN SPOTS",
        "window": "WINDOW",
        "quality": "QUALITY",
        "options": "OPTIONS",
        "beacon": "BEACON",
        "filters": "FILTERS",
        "all_sources": "ALL SOURCES",
        "rbn_only": "RBN ONLY",
        "rule": "RULE",
        "rules": "RULES",
    },
}


def prompt_message(message: str, client_callsign: str = "") -> str:
    prefix = (
        f"{client_callsign} de {DXA_PROMPT}"
        if client_callsign
        else DXA_PROMPT
    )
    return f"{prefix} {message}"


def client_text(language: str, key: str) -> str:
    return CLIENT_TEXT.get(language, CLIENT_TEXT["en"])[key]


def queue_unknown_command(session: ClientSession) -> str:
    session.queue.put_nowait(
        ClientMessage(
            payload=(
                prompt_message(
                    client_text(
                        session.language,
                        "command_rejected",
                    ),
                    session.callsign,
                )
                + "\r\n"
            ).encode("utf-8")
        )
    )
    return "handled"


def _filter_rule_line(spot_filter: SpotFilter) -> str:
    prefix = f"dxa rej/spot {spot_filter.number} "
    expression = spot_filter.command_text().removeprefix(prefix)
    return f"  {spot_filter.number}: {expression}"


def _state(enabled: bool) -> str:
    return "ON" if enabled else "OFF"


def _status_lines(session: ClientSession, title: str) -> list[str]:
    rule_count = len(session.spot_filters)
    text = CLIENT_TEXT.get(session.language, CLIENT_TEXT["en"])
    lines = [
        "",
        f"{title} - {session.callsign}",
        STATUS_SEPARATOR,
        (
            f"{text['sources']:<7} : "
            f"{text['human_spots']}: "
            f"{_state('dxcluster' in session.enabled_sources)}"
            f" | SKIMMER CW/RTTY: "
            f"{_state('rbn_cw' in session.enabled_sources)}"
        ),
        (
            "        : "
            f"SKIMMER FTx: "
            f"{_state('rbn_digital' in session.enabled_sources)}"
            f" | SKIMMER LOCAL: "
            f"{_state('rbn_local' in session.enabled_sources)}"
        ),
        (
            f"ADS     : {_state(session.ads_enabled)}"
            f" | {text['window']} {session.ads_window_seconds:g}s"
            f" | {text['quality']} Q{session.ads_min_spotters}"
        ),
        (
            f"{text['options']:<7} : "
            f"{text['beacon']} {_state(session.beacon_enabled)}"
            f" | SEEME {_state(session.seeme_enabled)}"
        ),
        (
            f"{text['filters']:<7} : "
            + (
                text["all_sources"]
                if session.filter_all_sources
                else text["rbn_only"]
            )
            + f" | {rule_count} "
            + (text["rule"] if rule_count == 1 else text["rules"])
        ),
    ]
    if session.spot_filters:
        lines.append("")
        lines.extend(
            _filter_rule_line(session.spot_filters[number])
            for number in sorted(session.spot_filters)
        )
    lines.extend((STATUS_SEPARATOR, ""))
    return lines


def queue_connection_summary(session: ClientSession) -> None:
    _queue_status_block(
        session,
        client_text(session.language, "ready"),
        prompt_message(
            client_text(session.language, "connection_established"),
            session.callsign,
        ),
    )


def queue_status(session: ClientSession) -> None:
    _queue_status_block(
        session,
        client_text(session.language, "status"),
        prompt_message(
            client_text(session.language, "command_accepted"),
            session.callsign,
        ),
    )


def _queue_status_block(
    session: ClientSession,
    title: str,
    prompt: str,
) -> None:
    payload = (
        prompt
        + "\r\n"
        + "\r\n".join(_status_lines(session, title))
        + "\r\n"
    ).encode("utf-8")
    session.queue.put_nowait(
        ClientMessage(
            payload=payload,
            pause_after_seconds=CONFIGURATION_PAUSE_SECONDS,
        )
    )


class FilterExpressionParser:
    def __init__(self, parts: tuple[str, ...]) -> None:
        self.tokens = tuple(
            re.findall(r"\(|\)|[^()\s]+", " ".join(parts))
        )
        self.index = 0

    def parse(self) -> FilterExpression | None:
        try:
            expression = self._parse_or()
            if self.index != len(self.tokens):
                return None
            return expression
        except ValueError:
            return None

    def _peek(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise ValueError
        self.index += 1
        return token

    def _parse_or(self) -> FilterExpression:
        expression = self._parse_and()
        while self._peek() == "or":
            self._take()
            expression = FilterExpression.combine(
                "or",
                expression,
                self._parse_and(),
            )
        return expression

    def _parse_and(self) -> FilterExpression:
        expression = self._parse_unary()
        while self._peek() == "and":
            self._take()
            expression = FilterExpression.combine(
                "and",
                expression,
                self._parse_unary(),
            )
        return expression

    def _parse_unary(self) -> FilterExpression:
        if self._peek() == "not":
            self._take()
            return FilterExpression.negate(self._parse_unary())
        if self._peek() == "(":
            self._take()
            expression = self._parse_or()
            if self._take() != ")":
                raise ValueError
            return expression
        return FilterExpression.condition(self._parse_predicate())

    def _parse_predicate(self) -> SpotPredicate:
        subject = "dx"
        if self._peek() == "by":
            self._take()
            subject = "by"
        field = self._take()
        if field in ("cq", "dxcc"):
            raw_values = self._take_value().split(",")
            if field == "cq":
                try:
                    values: frozenset[int | str] = frozenset(
                        int(value) for value in raw_values
                    )
                except ValueError as error:
                    raise ValueError from error
                if not values or any(
                    type(value) is not int or not 1 <= value <= 40
                    for value in values
                ):
                    raise ValueError
            else:
                values = frozenset(
                    value.upper() for value in raw_values
                )
                if not values or any(
                    DXCC_PREFIX_PATTERN.fullmatch(str(value)) is None
                    for value in values
                ):
                    raise ValueError
            return SpotPredicate(
                subject=subject,
                field=field,
                values=values,
            )
        if subject != "dx" or field != "on":
            raise ValueError
        if self._peek() == "freq":
            self._take()
            ranges = frozenset(self._take_value().split(","))
            if not ranges or any(
                parse_frequency_range(value) is None
                for value in ranges
            ):
                raise ValueError
            return SpotPredicate(
                subject="on",
                field="frequency",
                values=ranges,
            )
        requested_bands = self._take_value().split(",")
        expanded_bands: set[str] = set()
        for requested_band in requested_bands:
            if requested_band in BAND_SHORTCUTS:
                expanded_bands.update(BAND_SHORTCUTS[requested_band])
            elif requested_band in AMATEUR_BANDS_KHZ:
                expanded_bands.add(requested_band)
            else:
                raise ValueError
        if not expanded_bands:
            raise ValueError
        return SpotPredicate(
            subject="on",
            field="band",
            values=frozenset(expanded_bands),
        )

    def _take_value(self) -> str:
        value = self._take()
        if value in ("and", "or", "not", "(", ")"):
            raise ValueError
        return value


def handle_aggregator_command(
    session: ClientSession,
    command: str,
) -> str | None:
    parts = tuple(command.casefold().split())
    if not parts or parts[0] != "dxa":
        return None

    action = parts[1:]
    if action == ("bye",):
        return "disconnect"
    if action == ("status",):
        queue_status(session)
        return "handled"
    if action == ("status/default",):
        session.enabled_sources = {"dxcluster"}
        session.spot_filters.clear()
        session.filter_all_sources = False
        session.ads_enabled = False
        session.ads_window_seconds = ADS_WINDOW_SECONDS
        session.ads_min_spotters = ADS_DEFAULT_QUALITY
        session.beacon_enabled = True
        session.seeme_enabled = False
        queue_status(session)
        return "changed"
    if action == ("sh/filter",):
        queue_status(session)
        return "handled"
    if action == ("set/filter",):
        session.filter_all_sources = True
        queue_status(session)
        return "changed"
    if action == ("unset/filter",):
        session.filter_all_sources = False
        queue_status(session)
        return "changed"
    if action == ("set/ads",):
        session.ads_enabled = True
        queue_status(session)
        return "changed"
    if len(action) == 2 and action[0] == "set/ads":
        try:
            window_seconds = int(action[1])
        except ValueError:
            return queue_unknown_command(session)
        if not ADS_MIN_WINDOW_SECONDS <= window_seconds <= ADS_MAX_WINDOW_SECONDS:
            return queue_unknown_command(session)
        session.ads_window_seconds = float(window_seconds)
        session.ads_enabled = True
        queue_status(session)
        return "changed"
    if len(action) == 3 and action[0] == "set/ads":
        try:
            window_seconds = int(action[1])
            min_spotters = int(action[2])
        except ValueError:
            return queue_unknown_command(session)
        if (
            not ADS_MIN_WINDOW_SECONDS
            <= window_seconds
            <= ADS_MAX_WINDOW_SECONDS
            or not ADS_MIN_QUALITY <= min_spotters <= ADS_MAX_QUALITY
        ):
            return queue_unknown_command(session)
        session.ads_window_seconds = float(window_seconds)
        session.ads_min_spotters = min_spotters
        session.ads_enabled = True
        queue_status(session)
        return "changed"
    if action == ("unset/ads",):
        session.ads_enabled = False
        queue_status(session)
        return "changed"
    if action == ("set/beacon",):
        session.beacon_enabled = True
        queue_status(session)
        return "changed"
    if action == ("unset/beacon",):
        session.beacon_enabled = False
        queue_status(session)
        return "changed"
    if action == ("set/seeme",):
        session.seeme_enabled = True
        queue_status(session)
        return "changed"
    if action == ("unset/seeme",):
        session.seeme_enabled = False
        queue_status(session)
        return "changed"
    if action == ("clear/spot", "all"):
        session.spot_filters.clear()
        queue_status(session)
        return "changed"
    if len(action) == 2 and action[0] == "clear/spot":
        try:
            filter_number = int(action[1])
        except ValueError:
            return queue_unknown_command(session)
        if filter_number > 0:
            session.spot_filters.pop(filter_number, None)
            queue_status(session)
            return "changed"
        return queue_unknown_command(session)
    if action == ("set/skimmer",):
        session.enabled_sources.update(
            ("rbn_cw", "rbn_digital", "rbn_local")
        )
        queue_status(session)
        return "changed"
    if action == ("set/skimmer", "cw"):
        session.enabled_sources.add("rbn_cw")
        session.enabled_sources.discard("rbn_digital")
        queue_status(session)
        return "changed"
    if action == ("set/skimmer", "ftx"):
        session.enabled_sources.discard("rbn_cw")
        session.enabled_sources.add("rbn_digital")
        queue_status(session)
        return "changed"
    if action == ("unset/skimmer",):
        session.enabled_sources.difference_update(
            ("rbn_cw", "rbn_digital", "rbn_local")
        )
        queue_status(session)
        return "changed"
    if action == ("unset/skimmer", "cw"):
        session.enabled_sources.discard("rbn_cw")
        queue_status(session)
        return "changed"
    if action == ("unset/skimmer", "ftx"):
        session.enabled_sources.discard("rbn_digital")
        queue_status(session)
        return "changed"
    if action == ("set/skimmer", "lcl"):
        session.enabled_sources.add("rbn_local")
        queue_status(session)
        return "changed"
    if action == ("unset/skimmer", "lcl"):
        session.enabled_sources.discard("rbn_local")
        queue_status(session)
        return "changed"
    if len(action) >= 2 and action[0] == "rej/spot":
        try:
            filter_number = int(action[1])
        except ValueError:
            return queue_unknown_command(session)
        if filter_number < 1:
            return queue_unknown_command(session)
        expression = FilterExpressionParser(action[2:]).parse()
        if expression is None:
            return queue_unknown_command(session)
        session.spot_filters[filter_number] = SpotFilter(
            number=filter_number,
            reject=True,
            expression=expression,
        )
        queue_status(session)
        return "changed"
    return queue_unknown_command(session)
