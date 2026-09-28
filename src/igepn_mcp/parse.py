"""Classify channel posts and parse the structured ones.

Formats seen in the channel (all handled):
  2023+   [PRELIMINAR]/[REVISADO]  Evento: / Ocurrido: / Mag.: 3.8M / Prof.: 10.0 km / Lat.: / Long.: / Localizado:
  2021    [CONFIRMADO] instead of [REVISADO] (same fields)
  2019    [CONFIRMADO]  Codigo: / Tiempo Local: / Mag.: 3.8 (MLv) / Zona: / Localizacion: 0.928° N 79.788° W / Prof.: 8.5(km)
Times in quake posts are Ecuador local time (UTC-5, no DST) - stored as UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import fold

EC_OFFSET = timedelta(hours=-5)  # continental Ecuador, "TL" (tiempo local) in the posts

QUAKE, VOLCANO_REPORT, ALERT, PROCUREMENT, OUTREACH, OTHER = (
    "quake", "volcano_report", "alert", "procurement", "outreach", "other"
)
DROPPED = {PROCUREMENT}  # not stored at all

_STATUS = re.compile(r"^\s*\[(PRELIMINAR|REVISADO|CONFIRMADO)\]", re.I)
_REPORT_HEAD = re.compile(r"^\s*Informe\s+(Diario|Semanal|Mensual)\s+#(\w+)(?:\s+N\S*\s*([\w-]+))?", re.I)
_ESPECIAL = re.compile(r"^\s*INFORME\s+VOLC[AÁ]NICO\s+ESPECIAL", re.I)


def classify(text: str) -> str:
    head = text[:300]
    if _STATUS.match(text) and re.search(r"(Evento|C[oó]digo)\s*:", text):
        return QUAKE
    if _REPORT_HEAD.match(text):
        return VOLCANO_REPORT
    if "#IGAlInstante" in head or _ESPECIAL.match(text):
        return ALERT
    if "requerimiento de informacion" in fold(head):
        return PROCUREMENT
    if re.search(r"#(VuelosyVolcanesIG|ComunidadIG)", head):
        return OUTREACH
    return OTHER


# ---------------------------------------------------------------- earthquakes

_Q_EVENTO = re.compile(r"(?:Evento|C[oó]digo)\s*:\s*(\S+)", re.I)
_Q_TIME = re.compile(r"(?:Ocurrido|Tiempo Local)\s*:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)", re.I)
_Q_MAG = re.compile(r"Mag\.?\s*:\s*(-?[\d.]+)\s*\(?\s*([A-Za-z]\w*)?", re.I)
_Q_DEPTH = re.compile(r"Prof\.?\s*:\s*([\d.]+)", re.I)
_Q_COORD = re.compile(r"(\d+(?:\.\d+)?)\s*°\s*([NSEW])\b")
_Q_PLACE = re.compile(
    r"(?:Localizado|Zona)\s*:\s*(.+?)(?=\s+(?:Localizaci[oó]n|Prof\.|Sinti|Lat\.|Long\.|Mag\.)|\n|$)", re.I
)
_Q_FELT = re.compile(r"Rep[oó]rtelo\s*:\s*(https?://\S+)", re.I)


@dataclass(frozen=True)
class Quake:
    evento_id: str
    status: str
    occurred_utc: str
    mag: float | None
    mag_type: str | None
    depth_km: float | None
    lat: float | None
    lon: float | None
    place: str | None
    felt_url: str | None


def local_to_utc(date: str, time: str) -> str:
    fmt = "%Y-%m-%d %H:%M:%S" if time.count(":") == 2 else "%Y-%m-%d %H:%M"
    return (datetime.strptime(f"{date} {time}", fmt) - EC_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")


def _float(s: str | None) -> float | None:
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


def parse_quake(text: str) -> Quake | None:
    status, evento, when = _STATUS.match(text), _Q_EVENTO.search(text), _Q_TIME.search(text)
    if not (status and evento and when):
        return None
    lat = lon = None
    for value, hemi in _Q_COORD.findall(text):
        v = float(value) * (-1 if hemi in "SW" else 1)
        if hemi in "NS" and lat is None:
            lat = v
        elif hemi in "EW" and lon is None:
            lon = v
    mag, depth, place, felt = _Q_MAG.search(text), _Q_DEPTH.search(text), _Q_PLACE.search(text), _Q_FELT.search(text)
    return Quake(
        evento_id=evento.group(1).strip(),
        status=status.group(1).upper(),
        occurred_utc=local_to_utc(when.group(1), when.group(2)),
        mag=_float(mag.group(1)) if mag else None,
        mag_type=(mag.group(2) if mag else None) or None,
        depth_km=_float(depth.group(1)) if depth else None,
        lat=lat,
        lon=lon,
        place=place.group(1).strip() if place else None,
        felt_url=felt.group(1) if felt else None,
    )


# ---------------------------------------------------------------- volcano periodic reports

_MONTHS = {m: i for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
     "septiembre", "octubre", "noviembre", "diciembre"], start=1)}
_MONTHS["setiembre"] = 9
_ES_DATE = re.compile(r"(\d{1,2})\s+de\s+([a-záéíóú]+)\s+(?:de|del)\s+(\d{4})", re.I)
_URL = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class VolcanoReport:
    volcano: str
    report_kind: str
    report_no: str | None
    report_date: str
    superficial_level: str | None
    superficial_trend: str | None
    interna_level: str | None
    interna_trend: str | None
    url: str | None


def volcano_name(tag_or_caps: str) -> str:
    """'ElReventador' / 'EL REVENTADOR' -> 'El Reventador' (same display name from hashtags and bulletins)."""
    s = re.sub(r"(?<=[a-záéíóúñ])(?=[A-ZÁÉÍÓÚÑ])", " ", tag_or_caps.lstrip("#").strip())
    return " ".join(w.capitalize() for w in s.split())


def spanish_date(text: str, posted_at: str) -> str:
    """'miércoles 23 de septiembre de 2026' -> '2026-09-23'; falls back to the post's local date."""
    if (m := _ES_DATE.search(text)) and (month := _MONTHS.get(fold(m.group(2)))):
        return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
    posted = datetime.strptime(posted_at, "%Y-%m-%dT%H:%M:%SZ") + EC_OFFSET
    return posted.strftime("%Y-%m-%d")


def _fields(text: str) -> dict[str, str]:
    """'Key: value' pairs, one per line; also splits 'Superficial: Alta   Tendencia Superficial: ...' lines."""
    out: dict[str, str] = {}
    for line in re.sub(r"\s+(Tendencia\b)", r"\n\1", text).splitlines():
        key, sep, value = line.partition(":")
        if sep and value.strip():
            out.setdefault(fold(key).strip(), value.strip())
    return out


def parse_volcano_report(text: str, posted_at: str) -> VolcanoReport | None:
    head = _REPORT_HEAD.match(text)
    if not head:
        return None
    f = _fields(text)
    urls = _URL.findall(text)
    return VolcanoReport(
        volcano=volcano_name(head.group(2)),
        report_kind=head.group(1).lower(),
        report_no=head.group(3),
        report_date=spanish_date(text, posted_at),
        superficial_level=f.get("superficial"),
        superficial_trend=f.get("tendencia superficial"),
        interna_level=f.get("interna"),
        interna_trend=f.get("tendencia interna"),
        url=urls[-1] if urls else None,
    )


# ---------------------------------------------------------------- free-text bulletins

_A_VOLCANO = re.compile(r"VOLC[AÁ]N\s+(.+?)(?:\s+No\.|\s*$)", re.I | re.M)


@dataclass(frozen=True)
class Alert:
    kind: str
    volcano: str | None
    title: str
    text: str
    url: str | None


def parse_alert(text: str) -> Alert:
    title = text.strip().split("\n", 1)[0].replace("#IGAlInstante", "").strip()
    m = _A_VOLCANO.search(title)
    urls = _URL.findall(text)
    return Alert(
        kind="especial" if _ESPECIAL.match(text) else "instante",
        volcano=volcano_name(m.group(1)) if m else None,
        title=title,
        text=text.strip(),
        url=urls[-1] if urls else None,
    )
