# CLAUDE.md — igepn-mcp

> Handoff brief. Designed in the homelab war-room session (2026-09-28); build here.
> **Twin of `football-mcp`** (structured source → SQLite → FastMCP typed tools) — this is the **local
> seismic/volcanic** specialization. Unlike `news-mcp` (rolling window of RSS text), this **accumulates** — the
> store is a growing geophysical catalog, a historical asset.
> Repo = **code** (Python/FastMCP + poller). Deployment lives in the separate **homelab** repo — see "Deployment split".

## What this is
A **stateful, SQL-backed MCP server** giving a local LLM ("Yuki") **current and historical Ecuadorian earthquake
and volcano data**, sourced from the **IGEPN public Telegram channel** and served from local SQLite that a **3-minute
poller** refreshes. Two parts: a **poller** (fetch → classify → parse → append) and an **MCP query server** (reads
SQLite, typed tools). Python + FastMCP + `httpx` + stdlib `sqlite3`.

**IGEPN** = Instituto Geofísico de la Escuela Politécnica Nacional — Ecuador's national seismic/volcanic monitoring
authority (since 1983). Its denser local network catches small local quakes the global nets miss, reports in Spanish
with **human place names**, and — uniquely — publishes **volcano alert levels** (USGS has no equivalent).

## The star query
**"¿qué fue ese temblor hace unos minutos?"** — Yuki answers from the freshest IGEPN report: magnitude, depth,
location ("a 25 km de Cuenca"), and whether it's preliminary or revised. That single question is the reason this
exists; everything else (volcano status, historical analysis) is upside on the same store.

## Why it exists / where it fits
Consumer is Yuki (a grounded Gemma-4 12B home assistant). Grounding tiers by freshness + type:
- **openzim** (offline Wikipedia) → deep / historical, static snapshot.
- **news-mcp** → curated current events, RSS text, rolling window.
- **football-mcp** → structured sport, accumulates.
- **igepn-mcp (this)** → **structured local seismic/volcanic, accumulates.**
- **web_search / USGS** → broad + global fallback (see "USGS" below).

## Source — the IGEPN public Telegram channel
**`https://t.me/s/SismosVolcanesIGEPN`** — "Sismos & Volcanes - IGEPN".
- **Transport = the public `t.me/s/` preview page (keyless).** It's Telegram's own public, indexable preview served
  to any honest client — **with-the-grain** (not a bot-block bypass, no UA spoofing). This is the right call for a
  *query* (pull) use; no account, no secret. See [[data-access-with-the-grain]] in the homelab memory.
- **Upgrade path (only if needed later):** the official **MTProto client API** (Telethon/GramJS + a free API
  ID/hash from `my.telegram.org`, using a **dedicated throwaway account**) — for push, or if the preview ever
  truncates/gets flaky. Not needed for v1.
- Be a polite poller: honest `User-Agent` (identify the tool, e.g. `igepn-mcp/0.1 (+repo-url)`); a down fetch = skip
  the run, don't crash.

## Post taxonomy — classify, then keep or drop
The channel mixes signal and noise. From a 78-post preview sample (2026-09-28): ~25 sismos, ~23 volcano daily
reports, ~7 instant alerts, **~13 procurement notices (noise)**, ~10 other. Classify by the leading marker:

| Class | Marker | Action |
|---|---|---|
| **Earthquake** | starts `[PRELIMINAR]` / `[REVISADO]` + has `Evento:` | **parse → structured** |
| **Volcano daily** | `Informe Diario #<Volcán>` | **parse → structured** |
| **Instant alert** | `#IGAlInstante` (lahar / ash / felt) | **keep as text** |
| **Procurement** | `Requerimiento de Información - ADQUISICIÓN/CONTRATACIÓN` | **DROP** |
| **Outreach** | `#VuelosyVolcanesIG` / `#ComunidadIG` / photos | optional / low priority |

### Earthquake post — the format (stable, machine-parseable)
```
[PRELIMINAR]
Evento: igepn2026rymx
Ocurrido: 2026-09-13 02:53:03
Mag.: 3.8M
Prof.: 10.0 km
Lat.: 3.127° S
Long.: 79.066° W
Localizado: a 25.91 km de Cuenca, Azuay
Sintió este sismo? Repórtelo: https://servicios.igepn.edu.ec/url/...
```
Field mapping: `status` = `[PRELIMINAR]`|`[REVISADO]` · `evento_id` (Evento:) · `occurred` (Ocurrido:, **local
time Ecuador UTC−5 — verify `TL`/tz and normalize to UTC**) · `mag` + `mag_type` (`3.8M`, `3.3MLv` → number +
type) · `depth_km` (Prof.) · `lat`/`lon` (**S/W → negative decimal**) · `place` (Localizado:) · `felt_url`.

### Volcano daily report — the format
```
Informe Diario #ElReventador N° 2026-266
miércoles 23 de septiembre de 2026
Nivel de Actividad:
Superficial: Alta          Tendencia Superficial: Sin cambio
Interna: Moderada          Tendencia Interna: Sin cambio
Revisarlo en: https://servicios.igepn.edu.ec/url/...
```
Fields: `volcano` (from the hashtag) · `report_no` (N°) · `date` · `superficial_level` + `superficial_trend` ·
`interna_level` + `interna_trend` · `url`. Levels ∈ {Baja, Moderada, Alta, ...}; trends ∈ {Ascendente, Descendente,
Sin cambio}.

## Refresh — watermark-incremental, every 3 minutes
- Each `t.me/s/` post carries a stable **message id** (`data-post="SismosVolcanesIGEPN/<id>"`).
- Store the **max message id** seen (the watermark). Each poll: walk posts **newest → oldest, stop at the first id
  ≤ watermark.** Don't re-scrape the whole page. (~480 fetches/day — polite by construction.)
- **Two keys, two jobs:** message-id watermark = "what's new"; **`Evento` id = "what's corrected"** — a
  `[REVISADO]` arrives as a *new* message (new id, above the watermark) carrying the same `Evento`, so the
  incremental walk catches it and the current-event view (below) reflects the correction.
- Edge (note, don't solve now): if IGEPN ever **edits a post in place** below the watermark, the incremental walk
  won't see it. Their correction pattern is a new REVISADO post, so this is a non-issue in practice.
- Pagination edge: if more new posts exist than one preview page holds (rare in 3 min), page back with `?before=`,
  still stopping at the watermark.

## SQLite schema — append-only raw + derived current view (ACCUMULATE)
The data is **golden for later analysis** (coords, depth, magnitude, revision timing) → keep everything; never prune.
Storage is trivial (~100 bytes/row → a few MB for years).

- **`posts_raw`** — append-only, immutable. Every kept post exactly as captured: `msg_id PK, posted_at, class,
  raw_text, fetched_at`. The golden analytical log; nothing is ever overwritten or deleted here.
- **`quakes`** — parsed earthquake rows, **one per (evento_id, status)** so BOTH the preliminary and the revised
  record survive for analysis (revision-delta / latency is itself a dataset): `msg_id, evento_id, status,
  occurred_utc, mag, mag_type, depth_km, lat, lon, place, felt_url`.
- **`v_quake_current`** — a **view** (or materialized on write): latest row per `evento_id` (`[REVISADO]` wins over
  `[PRELIMINAR]`). This is what the fast family query reads.
- **`volcano_reports`** — parsed daily reports: `msg_id, volcano, report_no, report_date, superficial_level,
  superficial_trend, interna_level, interna_trend, url`. Keep history (trend over time).
- **`alerts`** — `#IGAlInstante` instant alerts: `msg_id, volcano, posted_at, text, url` (raw text).

> **Why keep both PRELIMINAR and REVISADO:** the query answer serves the corrected value (via `v_quake_current`),
> but analysis wants the correction history (how much/how fast IGEPN revises). Append-only raw + a current-view is
> the minimal way to get both — not gold-plating.

## MCP tools — STRUCTURED (not text-to-SQL). Good docstrings = the model's decision boundary.
**Family tier (build first):**
- `last_quake()` — the single most recent earthquake (current value). *The "¿qué fue ese temblor?" answer.*
- `latest_quakes(hours=24, min_mag=None, felt_only=False, n=15)` — recent earthquakes from `v_quake_current`.
- `volcano_status(volcano=None)` — latest level + trend per volcano (all, or one).
- `ig_alerts(hours=48)` — recent instant alerts (lahars/ash/felt).

**Analytical tier (Daniel's door, later — same store):**
- `quakes_near(lat, lon, radius_km, days=30)` — spatial query (haversine).
- `seismicity_stats(days=30, bbox=None)` — counts, magnitude distribution, rate vs. a rolling baseline (swarm hint).
- `volcano_trend(volcano, days=30)` — level/trend history for a volcano.
- `revision_stats(days=90)` — PRELIMINAR→REVISADO magnitude/depth deltas + revision latency (the meta-dataset).

Return compact JSON (one text block, `ensure_ascii=False`) to spare the 12B's context — mirror news-mcp's output style.

## Images — inframe/link route (human), not vision-input (deferred)
Two different "image" paths (learned in the war-room; the OWUI **weather tool** uses #2):
1. **image → model** (MCP `ImageContent` base64 → Gemma *sees* it): fragile through mcpo→OWUI; better on the native
   door. **Deferred.**
2. **visual → human** (return a media **URL** / small HTML card → OWUI renders it **inframe**): reliable, **zero
   model-context cost**.
For this server: any image need ("muéstrame la foto del Reventador", a shakemap) → **return the media URL** and let
the UI render it (path #2). Never attach images to a *list* tool (base64 bloats context). Reserve path #1 only for a
future "have Gemma *read* this map", and only on the native door.

## USGS / EMSC — the global backdrop (complementary, NOT this server)
IGEPN is the **local query authority** (this server). **USGS FDSN** (`earthquake.usgs.gov/fdsnws/event/1/`) and
**EMSC** (`seismicportal.eu`) are the fastest **structured global** feeds — Daniel's call is to treat them as a
**news/global-context tier**, not fold them here. They're the sub-minute structured backstop if ever wanted; IGEPN's
Spanish local report ("a 25 km de Cuenca") wins for the family door. Keep them out of igepn-mcp's scope for v1.

## Principles / ops
- **No API keys** (public preview is open). Polite poller (honest UA). A down fetch = skip, don't crash.
- **Accumulate, never prune** (historical asset — unlike news-mcp). Start capturing **soon**: the `t.me/s/` preview
  only shows a recent window, so every day unbuilt is stream (and PRELIMINAR→REVISADO timing) you can't recover.
- **Transport:** stdio for dev; **streamable-http + bearer** for mcpo → OWUI (mirror `openzim-mcp` / `news-mcp`).
- **Faithful presentation:** Yuki should report only what the source says, keep the preliminary/revised distinction,
  attribute to IGEPN + include the date/time.

## Deployment split
Code here (server + poller). Deploy in **homelab** `containers/stacks/igepn-mcp/` (compose + the 3-min poll
timer/interval + SQLite volume + channel config), mirroring `openzim-mcp` / `news-mcp`. No secrets (keyless).
Planned network slot (localNetwork 172.82.0.0/16, following the others — openzim .57, mcpo .58, news .59):
pick the next free (e.g. **172.82.0.60**, host `8645` → container `8000`) at deploy time; wire into mcpo's config
(`/docker/container-data/mcpo/config.json`, `--hot-reload`) as a new upstream.

## Historical backfill — OPEN (live-first; decide later)
v1 captures **live only** (forward from now via `t.me/s/`) — that forward stream is the primary asset, so start it
soon. Backfilling *older* history is a separate, later question. **Not Nitter** — it's for Twitter/X (wrong platform
for this Telegram channel), it's the bypass [[data-access-with-the-grain]] rules out, and it's mostly dead since the
2024 guest-token shutdown. In-grain options, best first:
1. **MTProto channel archive (same source, deeper API)** — the `t.me/s/` preview is a short window, but the official
   Telegram client API (Telethon `iter_messages`, free API id/hash, dedicated account) walks the channel's **full
   history** to its start. A one-time backfill through the sanctioned API — same channel, same authority. The clean
   twin of the live path.
2. **IGEPN's own services/catalog** — `servicios.igepn.edu.ec` (every post links there) — the authoritative
   historical seismic catalog if it's queryable; better than reconstructing from posts.
3. **USGS/EMSC FDSN** — a complete historical *earthquake* catalog by time + bbox (Ecuador). (Volcano-status history
   isn't there → that stays Telegram / IGEPN-site only.)

## First steps
1. Confirm the parse on a live preview fetch: quake fields (verify `Ocurrido` timezone), volcano-report fields,
   `#IGAlInstante` shape; confirm the procurement/outreach drop rules.
2. Scaffold: deps (FastMCP, httpx, stdlib sqlite3), schema (append-only raw + parsed tables + current view), poller
   (fetch → classify → parse → append + watermark), MCP server (family tools reading SQLite).
3. Test stdio with a seeded DB (paste real preview posts) → then streamable-http + mcpo → OWUI.
4. Hand deploy (compose + 3-min timer) to homelab. Add the analytical tools once data has accumulated.

## Origin
Design rationale (query-not-push, watermark-incremental polite polling, append-only-for-analysis, the two-image-paths
insight, USGS-as-backdrop, the grounding-tier map) came from the homelab session. Deeper context: homelab
`someday/todo.md`, `hosts/yuki.md` (the grounding stack + the family door), and the "The Grounded Box" artifact.

## License
**MPL-2.0** (matching the other public MCP repos — file-level weak copyleft: improvements to these files flow back,
commercial-friendly).

## Build status (2026-09-28)
- **v0.1 built** — poller (`poll` / `backfill` / `reparse`) + MCP server with the 4 family tools, stdio + streamable-http
  + bearer, Dockerfile, 15 offline tests (fixture = real posts). Verified live: poll → 0 unparsed, watermark stops
  re-fetch; stdio client + HTTP (`/healthz` 200, 401 without token, handshake with token).
- **Findings from the live preview (supersede the brief above where they differ):**
  - `Ocurrido` **is local time UTC−5** (post 12357 published 13:49Z for an event at 08:46 local). Stored as UTC;
    tools return both `occurred_local_ec` and `occurred_utc` (don't make the 12B do timezone math).
  - **`?before=` pages all the way back to the channel's first post (2019)** → the "Historical backfill — OPEN"
    question is answered in-grain with the *same keyless transport*: `igepn-mcp backfill` (resumable, 3 s/page,
    ~620 pages total). MTProto not needed.
  - **Format drift**, all parsed: 2021 uses `[CONFIRMADO]` instead of `[REVISADO]`; 2019 uses `Código:` /
    `Tiempo Local:` / `Mag.: 3.8 (MLv)` / `Zona:` / `Localización: lat lon` / `Prof.: 8.5(km)`.
  - More volcano report kinds: `Informe Diario|Semanal|Mensual #Volcán` (→ `volcano_reports.report_kind`), and
    `INFORME VOLCÁNICO ESPECIAL` (→ `alerts.kind='especial'`). Photo-only posts and `#Condolencias` → class `other`.
  - A REVISADO can arrive with no PRELIMINAR (small quakes), and revisions can move an event a lot (12385→12386:
    Naranjal, Guayas M3.6 → Tumbes, Perú M4.0). `last_quake` returns the `preliminary` estimate alongside.
  - Quake posts carry **no "felt" flag** (every post has the report-if-felt link), so `felt_only` was dropped
    from `latest_quakes`; replaced by `place` (accent-insensitive: "Manabí", "Quito").
- **Design deltas:** `quakes` is keyed by `msg_id` (one row per *report*, not per (evento, status) — an event can
  get more than one report); parsed tables are *derived* from `posts_raw` and rebuildable (`reparse`). Tool
  responses include a data-freshness block (warning if the last successful poll is >20 min old). TLS via
  `truststore` (OS trust store). Only the Windows dev laptop needs it — **Netskope** TLS inspection there makes
  certifi/curl/uv reject certs (`uv sync --system-certs` on that box). The homelab has no such proxy; truststore is
  harmless there (it reads the container's normal CA bundle).
- **Next:** homelab deploy (compose, `IGEPN_POLL_MINUTES=3`, volume); run the full `backfill` once; analytical tools
  once data has accumulated.
