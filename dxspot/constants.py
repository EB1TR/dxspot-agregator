"""Constantes y clasificaciones compartidas por la aplicación."""

from __future__ import annotations

import re


APP_VERSION = "0.1.0"
SOURCE_KEYS = ("dxcluster", "rbn_cw", "rbn_digital", "rbn_local")
FILTERED_SOURCE_KEYS = frozenset(("rbn_cw", "rbn_digital", "rbn_local"))
ADS_SOURCE_KEYS = FILTERED_SOURCE_KEYS
ADS_FREQUENCY_MARGIN_KHZ = 0.2
ADS_WINDOW_SECONDS = 5.0
ADS_MIN_WINDOW_SECONDS = 1
ADS_MAX_WINDOW_SECONDS = 60
ADS_DEFAULT_QUALITY = 1
ADS_MIN_QUALITY = 1
ADS_MAX_QUALITY = 100
ONE_DECIMAL_FREQUENCY_SOURCE_KEYS = frozenset(("rbn_cw", "rbn_digital"))
SOURCE_LABELS = {
    "dxcluster": "DXSPOT",
    "rbn_cw": "RBNCW",
    "rbn_digital": "RBNMGM",
    "rbn_local": "RBNLCL",
}
SOURCE_LABEL_WIDTH = 6
DEFAULT_COUNTRY_FILE_URL = "https://www.country-files.com/cty/cty.dat"
DX_SPOT_PATTERN = re.compile(
    r"^\s*DX\s+de\s+"
    r"(?P<spotter>[A-Z0-9/]+(?:-\d+)?)(?:-#)?\s*:\s*"
    r"(?P<frequency>\d+(?:\.\d+)?)\s+"
    r"(?P<dx>[A-Z0-9/]+(?:-\d+)?)",
    re.IGNORECASE,
)
DX_FREQUENCY_DECIMAL_COLUMN = 23
BEACON_COMMENT_PATTERN = re.compile(
    r"\b(?:BEACON|NCDXF)\b",
    re.IGNORECASE,
)
DXCC_PREFIX_PATTERN = re.compile(
    r"^(?=[A-Z0-9/]*[A-Z])[A-Z0-9]+(?:/[A-Z0-9]+)*$"
)
FREQUENCY_RANGE_PATTERN = re.compile(
    r"^(?P<lower>\d+(?:\.\d+)?)/(?P<upper>\d+(?:\.\d+)?)$"
)
AMATEUR_BANDS_KHZ = {
    "160m": (1_800.0, 2_000.0),
    "80m": (3_500.0, 4_000.0),
    "60m": (5_250.0, 5_450.0),
    "40m": (7_000.0, 7_300.0),
    "30m": (10_100.0, 10_150.0),
    "20m": (14_000.0, 14_350.0),
    "17m": (18_068.0, 18_168.0),
    "15m": (21_000.0, 21_450.0),
    "12m": (24_890.0, 24_990.0),
    "10m": (28_000.0, 29_700.0),
}
BAND_SHORTCUTS = {
    "hf": frozenset(AMATEUR_BANDS_KHZ),
}


def source_label(source_key: str) -> str:
    return SOURCE_LABELS[source_key].ljust(SOURCE_LABEL_WIDTH)


def source_login(login: str, source_key: str) -> str:
    if source_key == "rbn_local":
        return re.sub(r"-\d+$", "", login)
    return login


def dxspot_client_login(login: str) -> str:
    """Reemplaza cualquier SSID del cliente por el reservado para DXSPOT."""
    return re.sub(r"-\d+$", "", login.strip().upper()) + "-77"


def callsigns_match(login: str, spotted: str) -> bool:
    expected = re.sub(r"-\d+$", "", login.strip().upper())
    return bool(expected) and expected in spotted.strip().upper()


def frequency_band(frequency_khz: float) -> str | None:
    for band, (lower, upper) in AMATEUR_BANDS_KHZ.items():
        if lower <= frequency_khz <= upper:
            return band
    return None


def parse_frequency_range(value: str) -> tuple[float, float] | None:
    match = FREQUENCY_RANGE_PATTERN.fullmatch(value)
    if match is None:
        return None
    lower = float(match.group("lower"))
    upper = float(match.group("upper"))
    if lower <= 0 or lower > upper:
        return None
    return lower, upper
