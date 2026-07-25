"""Renderizado del dashboard de terminal."""

from __future__ import annotations

import re
import textwrap
import time
import unicodedata
from datetime import datetime
from typing import Any

from .constants import APP_VERSION, dxspot_client_login


ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_DIM = "\x1b[2m"
ANSI_RED = "\x1b[31m"
ANSI_GREEN = "\x1b[32m"
ANSI_YELLOW = "\x1b[33m"
ANSI_CYAN = "\x1b[36m"

CLI_SOURCE_LABELS = {
    "dxcluster": "SPOTS HUMANOS",
    "rbn_cw": "SKIMMER CW/RTTY",
    "rbn_digital": "SKIMMER FTx",
    "rbn_local": "SKIMMER LOCAL",
}


def clean_text(text: str) -> str:
    """Elimina controles que pueden sonar o alterar el cursor."""
    return "".join(
        character
        for character in text.expandtabs(8)
        if unicodedata.category(character) != "Cc"
    )


def terminal_width(text: str) -> int:
    plain = ANSI_PATTERN.sub("", text)
    return sum(
        0
        if (
            unicodedata.combining(character)
            or unicodedata.category(character) == "Cc"
        )
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in plain
    )


def truncate_text(text: str, width: int) -> str:
    fitted: list[str] = []
    used = 0
    for character in clean_text(text):
        cell_width = terminal_width(character)
        if used + cell_width > width:
            break
        fitted.append(character)
        used += cell_width
    return "".join(fitted)


def fit_text(text: str, width: int) -> str:
    fitted = truncate_text(text, width)
    return fitted + " " * max(0, width - terminal_width(fitted))


def format_age(timestamp: float | None) -> str:
    if timestamp is None:
        return "—"
    age = max(0, int(time.monotonic() - timestamp))
    if age < 60:
        return f"{age}s"
    minutes, seconds = divmod(age, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02}m"


def _event_style(message: str) -> str:
    normalized = message.casefold()
    if any(
        marker in normalized
        for marker in (
            "error",
            "fall",
            "rechaz",
            "descart",
            "desconect",
            "cerró",
        )
    ):
        return ANSI_RED
    if any(
        marker in normalized
        for marker in (
            "conectado",
            "escuchando",
            "entidades",
        )
    ):
        return ANSI_GREEN
    return ""


def activity_panel(
    application: Any,
    width: int,
    height: int,
) -> list[str]:
    inner = width - 2
    title = truncate_text(
        " ACTIVIDAD · ←/→ CAMBIAR · A ACTIVIDAD ",
        inner,
    )
    lines = [
        f"{ANSI_BOLD}{ANSI_CYAN}┌{title}"
        f"{'─' * max(0, inner - terminal_width(title))}┐{ANSI_RESET}"
    ]
    available_rows = max(0, height - 2)
    blocks: list[tuple[list[str], str]] = []
    for event in application.system_events:
        timestamp = datetime.fromisoformat(event["timestamp"]).strftime(
            "%H:%M:%S.%f"
        )[:-3]
        prefix = f"[{timestamp}] "
        message = clean_text(event["message"])
        wrapped = textwrap.wrap(
            message,
            width=max(1, inner - len(prefix)),
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        block = [
            prefix + wrapped[0],
            *(" " * len(prefix) + continuation for continuation in wrapped[1:]),
        ]
        blocks.append((block, _event_style(message)))

    selected: list[tuple[str, str]] = []
    used_rows = 0
    for block, style in reversed(blocks):
        if used_rows + len(block) > available_rows:
            continue
        selected[0:0] = [(entry, style) for entry in block]
        used_rows += len(block)
        if used_rows == available_rows:
            break

    for entry, style in selected:
        lines.append(_bordered_row(entry, inner, style=style))
    while len(lines) < height - 1:
        lines.append(_bordered_row("", inner))
    lines.append(f"{ANSI_CYAN}└{'─' * inner}┘{ANSI_RESET}")
    return lines[:height]


def stream_panel(
    title_text: str,
    entries: Any,
    width: int,
    height: int,
) -> list[str]:
    inner = width - 2
    title = truncate_text(
        f" {title_text} · ←/→ CAMBIAR · A ACTIVIDAD ",
        inner,
    )
    lines = [
        f"{ANSI_BOLD}{ANSI_CYAN}┌{title}"
        f"{'─' * max(0, inner - terminal_width(title))}┐{ANSI_RESET}"
    ]
    available_rows = max(0, height - 2)
    blocks: list[list[str]] = []
    for timestamp_value, message_value in entries:
        timestamp = datetime.fromisoformat(timestamp_value).strftime(
            "%H:%M:%S.%f"
        )[:-3]
        prefix = f"[{timestamp}] "
        message = clean_text(message_value)
        wrapped = textwrap.wrap(
            message,
            width=max(1, inner - len(prefix)),
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
        ) or [""]
        blocks.append(
            [
                prefix + wrapped[0],
                *(
                    " " * len(prefix) + continuation
                    for continuation in wrapped[1:]
                ),
            ]
        )

    selected: list[str] = []
    used_rows = 0
    for block in reversed(blocks):
        if used_rows + len(block) > available_rows:
            continue
        selected[0:0] = block
        used_rows += len(block)
        if used_rows == available_rows:
            break
    lines.extend(_bordered_row(entry, inner) for entry in selected)
    while len(lines) < height - 1:
        lines.append(_bordered_row("", inner))
    lines.append(f"{ANSI_CYAN}└{'─' * inner}┘{ANSI_RESET}")
    return lines[:height]


def dashboard_right_panel(
    application: Any,
    width: int,
    height: int,
) -> list[str]:
    selected_key = application.dashboard_view_key
    if selected_key == "activity":
        return activity_panel(application, width, height)
    for key, label, entries in application.dashboard_streams():
        if key != selected_key:
            continue
        if key.startswith("in:") and not key.startswith("in:client:"):
            source_key = key.removeprefix("in:")
            label = f"IN · {CLI_SOURCE_LABELS[source_key]}"
        return stream_panel(label, entries, width, height)
    application.dashboard_view_key = "activity"
    return activity_panel(application, width, height)


def _pad_rendered(text: str, width: int) -> str:
    return text + " " * max(0, width - terminal_width(text))


def _state_style(state: str) -> str:
    normalized = state.upper()
    if normalized in {"ACTIVO", "CONECTADO"}:
        return ANSI_BOLD + ANSI_GREEN
    if normalized == "DESCONECTADO":
        return ANSI_BOLD + ANSI_RED
    if normalized == "DESACTIVADO":
        return ANSI_DIM
    return ANSI_BOLD + ANSI_YELLOW


def _bordered_row(
    text: str,
    inner: int,
    *,
    style: str = "",
) -> str:
    content = fit_text(text, inner)
    return (
        f"{ANSI_CYAN}│{ANSI_RESET}"
        f"{style}{content}{ANSI_RESET if style else ''}"
        f"{ANSI_CYAN}│{ANSI_RESET}"
    )


CARD_HEIGHT = 10


def _card_pair_row(
    left: str,
    right: str,
    inner: int,
    *,
    left_style: str = "",
    right_style: str = "",
) -> str:
    right_text = truncate_text(right, max(0, inner // 2))
    right_width = terminal_width(right_text)
    left_width = max(0, inner - right_width - (1 if right_text else 0))
    left_text = truncate_text(left, left_width)
    gap = " " * max(
        0,
        inner - terminal_width(left_text) - right_width,
    )
    return (
        f"{ANSI_CYAN}│{ANSI_RESET}"
        f"{left_style}{left_text}{ANSI_RESET if left_style else ''}"
        f"{gap}"
        f"{right_style}{right_text}{ANSI_RESET if right_style else ''}"
        f"{ANSI_CYAN}│{ANSI_RESET}"
    )


def _metric_card(
    title: str,
    subtitle: str,
    state: str,
    metrics: list[tuple[str, str, str]],
    width: int,
) -> list[str]:
    inner = width - 2
    lines = [
        f"{ANSI_CYAN}┌{'─' * inner}┐{ANSI_RESET}",
        _card_pair_row(
            title,
            state,
            inner,
            left_style=ANSI_BOLD + ANSI_CYAN,
            right_style=_state_style(state),
        ),
        _bordered_row(subtitle, inner, style=ANSI_DIM),
        f"{ANSI_CYAN}├{'─' * inner}┤{ANSI_RESET}",
    ]
    lines.extend(
        _card_pair_row(
            label,
            value,
            inner,
            left_style=ANSI_DIM,
            right_style=value_style,
        )
        for label, value, value_style in metrics
    )
    while len(lines) < CARD_HEIGHT - 1:
        lines.append(_bordered_row("", inner))
    lines.append(f"{ANSI_CYAN}└{'─' * inner}┘{ANSI_RESET}")
    return lines[:CARD_HEIGHT]


def _grid_columns(width: int) -> int:
    if width >= 105:
        return 3
    if width >= 68:
        return 2
    return 1


def _card_row(
    cards: list[
        tuple[str, str, str, list[tuple[str, str, str]]]
    ],
    width: int,
    columns: int,
) -> list[str]:
    gaps = columns - 1
    available = width - gaps
    base_width, extra = divmod(available, columns)
    widths = [
        base_width + (1 if index < extra else 0)
        for index in range(columns)
    ]
    rendered = [
        _metric_card(*cards[index], widths[index])
        if index < len(cards)
        else [" " * widths[index]] * CARD_HEIGHT
        for index in range(columns)
    ]
    return [
        " ".join(card[line_index] for card in rendered)
        for line_index in range(CARD_HEIGHT)
    ]


def metrics_card_panel(
    application: Any,
    width: int,
    max_height: int,
) -> list[str]:
    columns = _grid_columns(width)
    source_cards = []
    for source in application.config.sources:
        if source.key == "dxcluster":
            continue
        stats = application.source_stats[source.key]
        line_rate, _ = stats.rx_rate()
        source_cards.append(
            (
                CLI_SOURCE_LABELS[source.key],
                f"{source.host}:{source.port}",
                stats.state,
                [
                    ("LÍNEAS/MIN", str(line_rate), ANSI_BOLD + ANSI_CYAN),
                    ("ÚLTIMA", format_age(stats.last_rx), ""),
                    ("RECONEXIONES", str(stats.reconnects), ""),
                ],
            )
        )

    lines = [
        f"{ANSI_BOLD}{ANSI_CYAN}"
        f"{fit_text('FUENTES COMUNES', width)}{ANSI_RESET}"
    ]
    for start in range(0, len(source_cards), columns):
        if start:
            lines.append("")
        lines.extend(
            _card_row(
                source_cards[start:start + columns],
                width,
                columns,
            )
        )

    if len(lines) + 2 >= max_height:
        return lines[:max_height]
    lines.extend(
        (
            "",
            f"{ANSI_BOLD}{ANSI_CYAN}"
            f"{fit_text('CLIENTES', width)}{ANSI_RESET}",
        )
    )

    sessions = sorted(
        application.clients.values(),
        key=lambda session: session.identifier,
    )
    if not sessions:
        lines.append(
            f"{ANSI_DIM}{fit_text('sin clientes conectados', width)}"
            f"{ANSI_RESET}"
        )
        return lines[:max_height]

    client_cards = []
    for session in sessions:
        delivery_rate, _ = session.stats.tx_rate()
        client_cards.append(
            (
                session.callsign,
                dxspot_client_login(session.callsign),
                session.dxspot_stats.state,
                [
                    ("ENTREGA/MIN", str(delivery_rate), ANSI_BOLD + ANSI_GREEN),
                    (
                        "ÚLTIMA",
                        format_age(session.dxspot_stats.last_rx),
                        "",
                    ),
                    ("RECONEXIONES", str(session.dxspot_stats.reconnects), ""),
                    (
                        "COLA",
                        f"{session.queue.qsize()}/{session.queue.maxsize}",
                        ANSI_BOLD + ANSI_YELLOW
                        if session.queue.qsize()
                        else "",
                    ),
                ],
            )
        )

    shown = 0
    for start in range(0, len(client_cards), columns):
        needed = CARD_HEIGHT + (1 if shown else 0)
        if len(lines) + needed > max_height:
            break
        if shown:
            lines.append("")
        row_cards = client_cards[start:start + columns]
        lines.extend(_card_row(row_cards, width, columns))
        shown += len(row_cards)
    if shown < len(client_cards) and len(lines) < max_height:
        lines.append(
            f"{ANSI_DIM}"
            f"{fit_text(f'… {len(client_cards) - shown} clientes más', width)}"
            f"{ANSI_RESET}"
        )
    return lines[:max_height]


def render_dashboard(application: Any, columns: int, rows: int) -> str:
    width = max(20, columns - 1)
    header = (
        f"DXSPOT AGREGATOR v{APP_VERSION} · "
        f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} · "
        f"CTY {application.country_database.version}"
    )
    lines = [
        f"{ANSI_CYAN}╭{'─' * (width - 2)}╮{ANSI_RESET}",
        (
            f"{ANSI_CYAN}│{ANSI_RESET}{ANSI_BOLD}"
            f"{fit_text(header, width - 2)}{ANSI_RESET}"
            f"{ANSI_CYAN}│{ANSI_RESET}"
        ),
        f"{ANSI_CYAN}╰{'─' * (width - 2)}╯{ANSI_RESET}",
    ]
    if rows >= 36:
        lines.append("")

    body_height = rows - len(lines)
    use_activity_column = width >= 220 and body_height >= 12
    if use_activity_column:
        activity_width = round((width - 1) * 0.45)
        left_width = width - activity_width - 1
        left_lines = metrics_card_panel(
            application,
            left_width,
            body_height,
        )
        while len(left_lines) < body_height:
            left_lines.append("")
        right_lines = dashboard_right_panel(
            application,
            activity_width,
            body_height,
        )
        lines.extend(
            f"{_pad_rendered(left, left_width)} {right}"
            for left, right in zip(left_lines, right_lines)
        )
    else:
        metrics_height = (
            max(12, round(body_height * 0.6))
            if body_height >= 24
            else body_height
        )
        lines.extend(
            metrics_card_panel(
                application,
                width,
                metrics_height,
            )
        )
        remaining_height = rows - len(lines)
        if remaining_height >= 4:
            lines.append("")
            remaining_height -= 1
            lines.extend(
                dashboard_right_panel(
                    application,
                    width,
                    remaining_height,
                )
            )
    return "\n".join(lines[:rows])
