"""Persistencia atómica de perfiles de clientes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from .constants import (
    ADS_DEFAULT_QUALITY,
    ADS_MAX_QUALITY,
    ADS_MAX_WINDOW_SECONDS,
    ADS_MIN_QUALITY,
    ADS_MIN_WINDOW_SECONDS,
    ADS_WINDOW_SECONDS,
    SOURCE_KEYS,
)
from .models import (
    ClientSession,
    FilterExpression,
    SpotFilter,
    SpotPredicate,
)


ClientProfile = tuple[
    frozenset[str],
    dict[int, SpotFilter],
    bool,
    bool,
    float,
    bool,
    int,
    bool,
    str | None,
]


class ClientProfileStore:
    def __init__(
        self,
        path: Path,
        log: Callable[[str], None],
    ) -> None:
        self.path = path
        self.log = log
        self.profiles = self._load()

    @staticmethod
    def filter_from_dict(raw: object) -> SpotFilter | None:
        if not isinstance(raw, dict):
            return None
        number = raw.get("number")
        reject = raw.get("reject")
        if (
            type(number) is not int
            or number < 1
            or not isinstance(reject, bool)
        ):
            return None
        expression = FilterExpression.from_dict(raw.get("expression"))
        if expression is None:
            negate = raw.get("negate")
            predicate = SpotPredicate.from_dict(
                {
                    "subject": raw.get("subject"),
                    "field": raw.get("field"),
                    "values": raw.get("values"),
                }
            )
            if not isinstance(negate, bool) or predicate is None:
                return None
            expression = FilterExpression.condition(predicate)
            if negate:
                expression = FilterExpression.negate(expression)
        return SpotFilter(
            number=number,
            reject=reject,
            expression=expression,
        )

    def _load(self) -> dict[str, ClientProfile]:
        try:
            raw_profiles = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            self.log(f"perfiles de cliente: no se pudieron cargar: {error}")
            return {}
        if not isinstance(raw_profiles, dict):
            self.log("perfiles de cliente: formato de raíz no válido")
            return {}

        profiles: dict[str, ClientProfile] = {}
        from .config import callsign

        for raw_callsign, raw_profile in raw_profiles.items():
            if not isinstance(raw_callsign, str) or not isinstance(
                raw_profile,
                dict,
            ):
                continue
            try:
                normalized_callsign = callsign(
                    raw_callsign,
                    "callsign",
                ).upper()
            except ValueError:
                continue
            sources_raw = raw_profile.get("sources")
            if not isinstance(sources_raw, list) or not all(
                isinstance(source, str) and source in SOURCE_KEYS
                for source in sources_raw
            ):
                continue
            filter_all_sources = raw_profile.get(
                "filter_all_sources",
                False,
            )
            if not isinstance(filter_all_sources, bool):
                continue
            ads_enabled = raw_profile.get("ads_enabled", False)
            if not isinstance(ads_enabled, bool):
                continue
            ads_window_seconds = raw_profile.get(
                "ads_window_seconds",
                ADS_WINDOW_SECONDS,
            )
            if (
                type(ads_window_seconds) not in (int, float)
                or not ADS_MIN_WINDOW_SECONDS
                <= float(ads_window_seconds)
                <= ADS_MAX_WINDOW_SECONDS
            ):
                continue
            beacon_enabled = raw_profile.get("beacon_enabled", True)
            if not isinstance(beacon_enabled, bool):
                continue
            ads_min_spotters = raw_profile.get(
                "ads_min_spotters",
                ADS_DEFAULT_QUALITY,
            )
            if (
                type(ads_min_spotters) is not int
                or not ADS_MIN_QUALITY
                <= ads_min_spotters
                <= ADS_MAX_QUALITY
            ):
                continue
            seeme_enabled = raw_profile.get("seeme_enabled", False)
            if not isinstance(seeme_enabled, bool):
                continue
            language = raw_profile.get("language")
            if language not in ("es", "en"):
                language = None
            filters: dict[int, SpotFilter] = {}
            filters_raw = raw_profile.get("filters", [])
            if isinstance(filters_raw, list):
                for raw_filter in filters_raw:
                    spot_filter = self.filter_from_dict(raw_filter)
                    if spot_filter is not None:
                        filters[spot_filter.number] = spot_filter
            profiles[normalized_callsign] = (
                frozenset(sources_raw),
                filters,
                filter_all_sources,
                ads_enabled,
                float(ads_window_seconds),
                beacon_enabled,
                ads_min_spotters,
                seeme_enabled,
                language,
            )
        return profiles

    def get(self, callsign: str) -> ClientProfile | None:
        return self.profiles.get(callsign)

    def save(self, session: ClientSession) -> None:
        if not session.callsign:
            return
        self.profiles[session.callsign] = (
            frozenset(session.enabled_sources),
            dict(session.spot_filters),
            session.filter_all_sources,
            session.ads_enabled,
            session.ads_window_seconds,
            session.beacon_enabled,
            session.ads_min_spotters,
            session.seeme_enabled,
            session.language,
        )
        serialized: dict[str, dict[str, object]] = {}
        for callsign, (
            sources,
            filters,
            filter_all_sources,
            ads_enabled,
            ads_window_seconds,
            beacon_enabled,
            ads_min_spotters,
            seeme_enabled,
            language,
        ) in sorted(self.profiles.items()):
            serialized[callsign] = {
                "sources": [
                    source for source in SOURCE_KEYS if source in sources
                ],
                "filter_all_sources": filter_all_sources,
                "ads_enabled": ads_enabled,
                "ads_window_seconds": ads_window_seconds,
                "beacon_enabled": beacon_enabled,
                "ads_min_spotters": ads_min_spotters,
                "seeme_enabled": seeme_enabled,
                "language": language,
                "filters": [
                    {
                        "number": spot_filter.number,
                        "reject": spot_filter.reject,
                        "expression": spot_filter.expression.to_dict(),
                    }
                    for _, spot_filter in sorted(filters.items())
                ],
            }

        temporary_name = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                json.dump(
                    serialized,
                    temporary,
                    ensure_ascii=False,
                    indent=2,
                )
                temporary.write("\n")
                temporary_name = temporary.name
            os.replace(temporary_name, self.path)
        except OSError as error:
            self.log(
                f"perfil de {session.callsign}: no se pudo guardar: {error}"
            )
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass
