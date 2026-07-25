"""Descarga, analiza y consulta la base oficial CTY.DAT."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path


MAX_DOWNLOAD_BYTES = 10_000_000
HEADER_PATTERN = re.compile(
    r"^(?P<country>.+?):\s*"
    r"(?P<cq>\d+):\s*"
    r"(?P<itu>\d+):\s*"
    r"(?P<continent>[A-Z]{2}):\s*"
    r"(?P<latitude>-?\d+(?:\.\d+)?):\s*"
    r"(?P<longitude>-?\d+(?:\.\d+)?):\s*"
    r"(?P<utc_offset>-?\d+(?:\.\d+)?):\s*"
    r"(?P<prefix>\*?[A-Z0-9/]+):\s*$",
    re.IGNORECASE,
)
MODIFIER_PATTERN = re.compile(r"[\(\[<\{~]")


@dataclass(frozen=True)
class CountryMatch:
    country: str
    dxcc_prefix: str
    cq_zone: int
    itu_zone: int
    continent: str
    latitude: float
    longitude: float
    utc_offset: float


class CountryDatabase:
    def __init__(self) -> None:
        self.exact_calls: dict[str, CountryMatch] = {}
        self.prefixes: dict[str, CountryMatch] = {}
        self.entity_count = 0
        self.version = "desconocida"

    @classmethod
    def from_text(cls, text: str) -> CountryDatabase:
        database = cls()
        current: CountryMatch | None = None

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            header = HEADER_PATTERN.match(line)
            if header:
                primary_prefix = header.group("prefix").lstrip("*").upper()
                current = CountryMatch(
                    country=header.group("country").strip(),
                    dxcc_prefix=primary_prefix,
                    cq_zone=int(header.group("cq")),
                    itu_zone=int(header.group("itu")),
                    continent=header.group("continent").upper(),
                    latitude=float(header.group("latitude")),
                    longitude=float(header.group("longitude")),
                    utc_offset=float(header.group("utc_offset")),
                )
                database.entity_count += 1
                database.prefixes.setdefault(primary_prefix, current)
                continue

            if current is None:
                continue
            for raw_alias in re.split(r"[,;]", line):
                database._add_alias(raw_alias.strip(), current)

        if database.entity_count == 0 or not database.prefixes:
            raise ValueError("CTY.DAT no contiene entidades válidas")
        return database

    def _add_alias(self, token: str, country: CountryMatch) -> None:
        if not token:
            return
        exact = token.startswith("=")
        if exact:
            token = token[1:]
        token = token.lstrip("*")
        base = MODIFIER_PATTERN.split(token, maxsplit=1)[0].strip().upper()
        if not base:
            return

        cq_match = re.search(r"\((\d+)\)", token)
        itu_match = re.search(r"\[(\d+)\]", token)
        coordinates = re.search(
            r"<(-?\d+(?:\.\d+)?)/(-?\d+(?:\.\d+)?)>",
            token,
        )
        continent = re.search(r"\{([A-Z]{2})\}", token, re.IGNORECASE)
        utc_offset = re.search(r"~(-?\d+(?:\.\d+)?)~", token)
        resolved = replace(
            country,
            cq_zone=int(cq_match.group(1)) if cq_match else country.cq_zone,
            itu_zone=int(itu_match.group(1)) if itu_match else country.itu_zone,
            latitude=(
                float(coordinates.group(1))
                if coordinates
                else country.latitude
            ),
            longitude=(
                float(coordinates.group(2))
                if coordinates
                else country.longitude
            ),
            continent=(
                continent.group(1).upper()
                if continent
                else country.continent
            ),
            utc_offset=(
                float(utc_offset.group(1))
                if utc_offset
                else country.utc_offset
            ),
        )
        if exact:
            self.exact_calls.setdefault(base, resolved)
            version_match = re.fullmatch(r"VER(\d{8})", base)
            if (
                version_match is not None
                and self.version == "desconocida"
            ):
                self.version = version_match.group(1)
        else:
            self.prefixes.setdefault(base, resolved)

    def resolve(self, callsign: str) -> CountryMatch | None:
        normalized = callsign.strip().upper()
        normalized = re.sub(r"-\d+$", "", normalized)
        normalized = normalized.rstrip(".,:;")
        if not normalized:
            return None

        exact = self.exact_calls.get(normalized)
        if exact is not None:
            return exact
        for length in range(len(normalized), 0, -1):
            match = self.prefixes.get(normalized[:length])
            if match is not None:
                return match
        return None


def download_latest(
    url: str,
    cache_path: Path,
    timeout: float,
) -> tuple[CountryDatabase, str, bool]:
    metadata_path = cache_path.with_name(f"{cache_path.name}.http.json")
    try:
        raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = (
            raw_metadata
            if isinstance(raw_metadata, dict)
            else {}
        )
    except (OSError, json.JSONDecodeError):
        metadata = {}
    headers = {"User-Agent": "DXSpot-Agregator/0.1"}
    etag = metadata.get("etag")
    last_modified = metadata.get("last_modified")
    if isinstance(etag, str) and etag:
        headers["If-None-Match"] = etag
    if isinstance(last_modified, str) and last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        request = urllib.request.Request(
            url,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_DOWNLOAD_BYTES + 1)
                response_etag = response.headers.get("ETag")
                response_last_modified = response.headers.get("Last-Modified")
        except urllib.error.HTTPError as error:
            if error.code != 304:
                raise
            cached_text = cache_path.read_text(encoding="utf-8-sig")
            database = CountryDatabase.from_text(cached_text)
            return database, "no hay nueva versión (HTTP 304)", False
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise ValueError("CTY.DAT supera el tamaño máximo permitido")
        text = payload.decode("utf-8-sig")
        database = CountryDatabase.from_text(text)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=cache_path.parent,
                prefix=f".{cache_path.name}.",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary_name = temporary.name
            os.replace(temporary_name, cache_path)
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass
        validators = {
            key: value
            for key, value in {
                "etag": response_etag,
                "last_modified": response_last_modified,
            }.items()
            if value
        }
        try:
            if validators:
                metadata_name = ""
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=cache_path.parent,
                        prefix=f".{metadata_path.name}.",
                        delete=False,
                    ) as metadata_file:
                        json.dump(
                            validators,
                            metadata_file,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        metadata_name = metadata_file.name
                    os.replace(metadata_name, metadata_path)
                finally:
                    if metadata_name:
                        try:
                            Path(metadata_name).unlink()
                        except OSError:
                            pass
            else:
                try:
                    metadata_path.unlink()
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        return database, "descargada", True
    except Exception as download_error:
        try:
            cached_text = cache_path.read_text(encoding="utf-8-sig")
            database = CountryDatabase.from_text(cached_text)
        except Exception:
            raise RuntimeError(
                f"no se pudo descargar CTY.DAT ni cargar la caché: "
                f"{download_error}"
            ) from download_error
        return (
            database,
            f"caché local; descarga fallida: {download_error}",
            False,
        )
