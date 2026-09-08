"""homearchive — web UI.

Runs as its own service next to the collector and talks to the same two
databases. Restarting it never interrupts data collection; the collector's
state is read from the collector_status heartbeat.

Configuration (environment):
    KNX_DSN, HA_DSN         as for the collector
    UI_USER                 login name, default "admin"
    UI_PASSWORD_HASH        from `python ui.py --hash`
    UI_BIND, UI_PORT        default 0.0.0.0:8080
    KNX_PROJECT_PASSWORD    optional default for .knxproj imports
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import uuid
from typing import Any
from urllib.parse import quote

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

import db

BASE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
app = FastAPI(title="homearchive")
security = HTTPBasic()

# In-process state of running .knxproj imports (parsing takes minutes).
JOBS: dict[str, dict[str, Any]] = {}


# ── authentication ────────────────────────────────────────────────────────
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"pbkdf2_sha256$240000${base64.b64encode(salt).decode()}$" \
           f"{base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt_b64), int(rounds)
        )
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:  # noqa: BLE001 — malformed hash means no access
        return False


def auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    user = os.environ.get("UI_USER", "admin")
    stored = os.environ.get("UI_PASSWORD_HASH", "")
    ok_user = hmac.compare_digest(credentials.username, user)
    ok_pass = bool(stored) and verify_password(credentials.password, stored)
    if not (ok_user and ok_pass):
        raise HTTPException(401, "unauthorized", {"WWW-Authenticate": "Basic"})
    return credentials.username


# ── helpers ───────────────────────────────────────────────────────────────
def query(dsn: str, sql: str, args: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.cursor().execute(sql, args).fetchall()


def execute(dsn: str, sql: str, args: tuple = ()) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.cursor().execute(sql, args)


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


# ── status ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def status(request: Request, _: str = Depends(auth)):
    rows = query(db.KNX_DSN, """
        SELECT updated_at, started_at, knx_connected, ha_connected,
               knx_received, knx_written, knx_skipped, knx_undecoded,
               knx_dropped, ha_received, ha_written, ha_skipped, ha_dropped,
               mqtt_connected, mqtt_received, mqtt_mapped, mqtt_unmatched,
               now() - updated_at AS age
        FROM collector_status WHERE id = 1""")
    cols = ["updated_at", "started_at", "knx_connected", "ha_connected",
            "knx_received", "knx_written", "knx_skipped", "knx_undecoded",
            "knx_dropped", "ha_received", "ha_written", "ha_skipped",
            "ha_dropped", "mqtt_connected", "mqtt_received", "mqtt_mapped",
            "mqtt_unmatched", "age"]
    st = dict(zip(cols, rows[0], strict=True)) if rows else {}
    no_dpt = query(db.KNX_DSN,
                   "SELECT address, name FROM knx_ga WHERE dpt IS NULL"
                   " ORDER BY address")
    imports = query(db.KNX_DSN,
                    "SELECT imported_at, source_file, added, changed, vanished"
                    " FROM knx_import_log ORDER BY imported_at DESC LIMIT 5")
    counts = query(db.KNX_DSN,
                   "SELECT count(*) FILTER (WHERE archive), count(*) FROM knx_ga")[0]
    # Which addresses actually produce raw values — the bare counter says
    # "unreadable" without saying what, which reads like a fault even when
    # the raw bytes are wanted.
    raw = query(db.KNX_DSN,
                "SELECT m.destination, g.name, g.dpt, count(*), g.note"
                " FROM knx_measurements m"
                " LEFT JOIN knx_ga g ON g.address = m.destination"
                " WHERE m.time > now() - interval '24 hours'"
                "   AND m.knxvalue LIKE '0x%%'"
                " GROUP BY 1, 2, 3, 5 ORDER BY 4 DESC LIMIT 10")
    ha_counts = query(db.HA_DSN,
                      "SELECT count(*) FILTER (WHERE archive), count(*)"
                      " FROM ha_entity")[0]
    return page(request, "status.html", st=st, no_dpt=no_dpt, imports=imports,
                ga_counts=counts, ha_counts=ha_counts, raw=raw)


# ── KNX group addresses ───────────────────────────────────────────────────
@app.get("/knx", response_class=HTMLResponse)
def knx_list(request: Request, q: str = "", only: str = "all",
             _: str = Depends(auth)):
    where, args = ["true"], []
    if q:
        where.append("(address ILIKE %s OR name ILIKE %s)")
        args += [f"%{q}%", f"%{q}%"]
    if only == "excluded":
        where.append("NOT archive")
    elif only == "nodpt":
        where.append("dpt IS NULL")
    elif only == "locked":
        where.append("locked")
    rows = query(db.KNX_DSN,
                 "SELECT address, name, dpt, archive, origin, last_seen, locked"
                 f" FROM knx_ga WHERE {' AND '.join(where)}"
                 " ORDER BY string_to_array(address, '/')::int[] LIMIT 500",
                 tuple(args))
    return page(request, "knx.html", rows=rows, q=q, only=only)


@app.post("/knx/toggle")
def knx_toggle(address: str = Form(...), q: str = Form(""),
               only: str = Form("all"), _: str = Depends(auth)):
    execute(db.KNX_DSN,
            "UPDATE knx_ga SET archive = NOT archive, updated_at = now()"
            " WHERE address = %s", (address,))
    return RedirectResponse(f"/knx?q={q}&only={only}", status_code=303)


@app.post("/knx/lock")
def knx_lock(address: str = Form(...), q: str = Form(""),
             only: str = Form("all"), _: str = Depends(auth)):
    """Protect name and DPT against the next .knxproj import, or release it."""
    execute(db.KNX_DSN,
            "UPDATE knx_ga SET locked = NOT locked, updated_at = now()"
            " WHERE address = %s", (address,))
    return RedirectResponse(f"/knx?q={q}&only={only}", status_code=303)


# ── single group address ──────────────────────────────────────────────────
def describe_dpt(dpt: str | None) -> tuple[str, str | None]:
    """Validate a DPT against xknx and describe what it decodes to."""
    if not dpt:
        return "kein DPT — Werte werden als Hex gespeichert", None
    from knx_source import transcoder_for
    t = transcoder_for(dpt)
    if t is None:
        return f"unbekannt — xknx kennt {dpt} nicht, Werte kaemen als Hex", None
    unit = getattr(t, "unit", None)
    return f"{t.__name__}" + (f", Einheit {unit}" if unit else ""), unit


# Group addresses contain slashes, so they travel as a query parameter:
# Starlette's greedy path converter would swallow the trailing segment.
@app.get("/knx/edit", response_class=HTMLResponse)
def knx_edit(request: Request, address: str, saved: str = "",
             _: str = Depends(auth)):
    rows = query(db.KNX_DSN,
                 "SELECT address, name, dpt, archive, origin, locked, note,"
                 " description, first_seen, last_seen"
                 " FROM knx_ga WHERE address = %s", (address,))
    if not rows:
        raise HTTPException(404, "unknown group address")
    cols = ["address", "name", "dpt", "archive", "origin", "locked", "note",
            "description", "first_seen", "last_seen"]
    ga = dict(zip(cols, rows[0], strict=True))
    stored_unit = query(db.KNX_DSN,
                        "SELECT unit FROM knx_dpt_unit WHERE dpt = %s",
                        (ga["dpt"],)) if ga["dpt"] else []
    desc, unit = describe_dpt(ga["dpt"])
    recent = query(db.KNX_DSN,
                   "SELECT time, knxvalue, knxunit FROM knx_measurements"
                   " WHERE destination = %s AND time > now() - interval '2 days'"
                   " ORDER BY time DESC LIMIT 8", (address,))
    return page(request, "knx_edit.html", ga=ga, desc=desc,
                stored_unit=stored_unit[0][0] if stored_unit else None,
                recent=recent, saved=saved)


@app.post("/knx/edit")
def knx_edit_save(address: str = Form(...), dpt: str = Form(""),
                  note: str = Form(""), locked: str = Form(""),
                  archive: str = Form(""), _: str = Depends(auth)):
    dpt = dpt.strip() or None
    if dpt:
        desc, _unit = describe_dpt(dpt)
        if desc.startswith("unbekannt"):
            return RedirectResponse(
                f"/knx/edit?address={quote(address)}&saved=invalid",
                status_code=303)
    execute(db.KNX_DSN,
            "UPDATE knx_ga SET dpt = %s, note = %s, locked = %s,"
            " archive = %s, updated_at = now() WHERE address = %s",
            (dpt, note or None, locked == "on", archive == "on", address))
    reload_collector()
    return RedirectResponse(f"/knx/edit?address={quote(address)}&saved=ok",
                            status_code=303)


def reload_collector() -> None:
    """Ask the collector to re-read the registry instead of waiting for its
    ten minute cycle. Best effort: the UI may lack permission to signal it."""
    import signal
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", "python -u service.py"],
                             capture_output=True, text=True, timeout=5)
        for pid in out.stdout.split():
            os.kill(int(pid), signal.SIGHUP)
    except Exception:  # noqa: BLE001 — reload is a convenience, not a promise
        pass


# ── Home Assistant entities ───────────────────────────────────────────────
@app.get("/ha", response_class=HTMLResponse)
def ha_list(request: Request, q: str = "", only: str = "all",
            _: str = Depends(auth)):
    where, args = ["true"], []
    if q:
        where.append("entity_id ILIKE %s")
        args.append(f"%{q}%")
    if only == "excluded":
        where.append("NOT archive")
    rows = query(db.HA_DSN,
                 "SELECT entity_id, domain, archive, first_seen, last_seen"
                 f" FROM ha_entity WHERE {' AND '.join(where)}"
                 " ORDER BY entity_id LIMIT 500", tuple(args))
    return page(request, "ha.html", rows=rows, q=q, only=only)


@app.post("/ha/toggle")
def ha_toggle(entity_id: str = Form(...), q: str = Form(""),
              only: str = Form("all"), _: str = Depends(auth)):
    execute(db.HA_DSN,
            "UPDATE ha_entity SET archive = NOT archive, updated_at = now()"
            " WHERE entity_id = %s", (entity_id,))
    return RedirectResponse(f"/ha?q={q}&only={only}", status_code=303)


# ── KNX connection ────────────────────────────────────────────────────────
# One page owns every knx.* setting. Which of them matter depends on the
# mode, so the page says so instead of leaving seven keys side by side.
MODES = [
    ("tunnel", "Tunnel über UDP",
     "Klassische KNXnet/IP-Tunnelverbindung, unverschlüsselt. "
     "Belegt einen Tunnel-Slot am Gateway."),
    ("tunnel_tcp", "Tunnel über TCP",
     "Wie oben, aber über TCP — robuster bei Paketverlust, ebenfalls "
     "unverschlüsselt."),
    ("tunnel_secure", "Tunnel mit KNX IP Secure",
     "Verschlüsselter Tunnel über TCP. Braucht den Keyring aus ETS und muss "
     "am Gateway freigeschaltet sein."),
    ("routing", "Routing über Multicast",
     "Hört auf 224.0.23.12 mit, ohne einen Tunnel-Slot zu belegen. Kein "
     "Gateway nötig, dafür muss Multicast im Netz durchkommen."),
    ("routing_secure", "Routing mit KNX IP Secure",
     "Verschlüsseltes Multicast. Braucht den Keyring und eine Secure-Linie."),
]
SECURE_MODES = {"tunnel_secure", "routing_secure"}
TUNNEL_MODES = {"tunnel", "tunnel_tcp", "tunnel_secure"}


@app.get("/settings/knx", response_class=HTMLResponse)
def connection_view(request: Request, saved: str = "", _: str = Depends(auth)):
    import keyring_store
    cfg = dict(query(db.KNX_DSN,
                     "SELECT key, value FROM settings WHERE key LIKE %s"
                     " ORDER BY key", ("knx.%",)))
    st = keyring_store.status()
    interfaces, error = [], None
    if st.get("present") and st.get("password_stored"):
        try:
            interfaces = keyring_store.describe(
                keyring_store.KEYRING_FILE, keyring_store.keyring_password())
        except Exception as exc:  # noqa: BLE001 — shown to the user
            error = str(exc)
    mode = cfg.get("knx.connection") or "tunnel"
    return page(request, "settings_knx.html", cfg=cfg, st=st, error=error,
                interfaces=interfaces, modes=MODES, mode=mode,
                is_secure=mode in SECURE_MODES,
                is_tunnel=mode in TUNNEL_MODES, saved=saved)


@app.post("/settings/knx")
def connection_save(connection: str = Form(...), gateway_host: str = Form(""),
                    gateway_port: str = Form(""),
                    individual_address: str = Form(""),
                    secure_user_id: str = Form(""), _: str = Depends(auth)):
    if connection not in {m for m, _label, _desc in MODES}:
        raise HTTPException(400, "unknown connection mode")
    for key, value in [
        ("knx.connection", connection),
        ("knx.gateway_host", gateway_host.strip()),
        ("knx.gateway_port", gateway_port.strip()),
        ("knx.individual_address", individual_address.strip()),
        ("knx.secure_user_id", secure_user_id.strip()),
    ]:
        execute(db.KNX_DSN,
                "UPDATE settings SET value = %s, updated_at = now()"
                " WHERE key = %s", (value, key))
    return RedirectResponse("/settings/knx?saved=ok", status_code=303)


@app.post("/settings/knx/keyring")
async def connection_keyring(file: UploadFile, password: str = Form(...),
                             _: str = Depends(auth)):
    import keyring_store
    try:
        keyring_store.store(await file.read(), password)
    except Exception:  # noqa: BLE001 — wrong password or not a keyring
        return RedirectResponse("/settings/knx?saved=invalid",
                                status_code=303)
    execute(db.KNX_DSN,
            "UPDATE settings SET value = %s, updated_at = now()"
            " WHERE key = 'knx.keyring_path'",
            (str(keyring_store.KEYRING_FILE),))
    return RedirectResponse("/settings/knx?saved=keyring", status_code=303)


# ── messages: raw values and dropped rows ─────────────────────────────────
@app.get("/messages", response_class=HTMLResponse)
def messages(request: Request, hours: int = 24, _: str = Depends(auth)):
    hours = max(1, min(hours, 168))
    raw = query(db.KNX_DSN,
                "SELECT m.time, m.source, m.destination, m.dpt, m.knxvalue,"
                "       g.name, g.note"
                " FROM knx_measurements m"
                " LEFT JOIN knx_ga g ON g.address = m.destination"
                " WHERE m.time > now() - make_interval(hours => %s)"
                "   AND m.knxvalue LIKE '0x%%'"
                " ORDER BY m.time DESC LIMIT 200", (hours,))

    import writer
    dropped, spool_size = [], 0
    try:
        spool_size = writer.SPOOL_FILE.stat().st_size
        lines = writer.SPOOL_FILE.read_text().splitlines()[-200:]
        for line in reversed(lines):
            try:
                dropped.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return page(request, "messages.html", raw=raw, dropped=dropped,
                hours=hours, spool_size=spool_size)


@app.post("/messages/clear")
def messages_clear(_: str = Depends(auth)):
    import writer
    with contextlib.suppress(OSError):
        writer.SPOOL_FILE.unlink(missing_ok=True)
    return RedirectResponse("/messages", status_code=303)


# ── MQTT data view ────────────────────────────────────────────────────────
@app.get("/mqtt", response_class=HTMLResponse)
def mqtt_data(request: Request, q: str = "", only: str = "all",
              _: str = Depends(auth)):
    """What actually arrives on the broker — the counterpart to the KNX and
    Home Assistant lists."""
    where, args = ["true"], []
    if q:
        where.append("topic ILIKE %s")
        args.append(f"%{q}%")
    if only == "unmatched":
        where.append("NOT matched")
    elif only == "matched":
        where.append("matched")
    rows = query(db.KNX_DSN,
                 "SELECT topic, first_seen, last_seen, messages, matched,"
                 " payload FROM mqtt_seen"
                 f" WHERE {' AND '.join(where)}"
                 " ORDER BY last_seen DESC LIMIT 200", tuple(args))
    configured = {r[0]: r[1] for r in query(
        db.KNX_DSN, "SELECT topic, id FROM mqtt_topic")}
    counts = query(db.KNX_DSN,
                   "SELECT count(*) FILTER (WHERE matched), count(*)"
                   " FROM mqtt_seen")[0]
    return page(request, "mqtt.html", rows=rows, q=q, only=only,
                configured=configured, counts=counts,
                flatten=_flatten)


@app.post("/mqtt/forget")
def mqtt_forget(topic: str = Form(...), _: str = Depends(auth)):
    execute(db.KNX_DSN, "DELETE FROM mqtt_seen WHERE topic = %s", (topic,))
    return RedirectResponse("/mqtt", status_code=303)


# ── exclusions ────────────────────────────────────────────────────────────
# Patterns use SQL LIKE syntax, matched with fnmatch at runtime ('%' -> '*').
KNX_EXAMPLES = [
    ("0/0/%", "alle Zentralfunktionen der Hauptgruppe 0"),
    ("20/1/%", "Strom Ein/Aus der ganzen Küche (E16)"),
    ("%/6/%", "die Mittelgruppe Sensorik in allen Hauptgruppen"),
    ("%/7/2%", "alle Adressen der Mittelgruppe 7, die mit 2 beginnen"),
]
HA_EXAMPLES = [
    ("automation.%", "die ganze Domain automation"),
    ("sensor.flightradar24_%", "alle Flightradar-Sensoren"),
    ("%_uptime", "jede Entity, deren Name auf _uptime endet"),
    ("sensor.awtrix%free_ram", "Platzhalter dürfen auch in der Mitte stehen"),
]


@app.get("/exclusions", response_class=HTMLResponse)
def exclusions(request: Request, _: str = Depends(auth)):
    rows = query(db.KNX_DSN,
                 "SELECT id, kind, pattern, note FROM archive_exclude_pattern"
                 " ORDER BY kind, pattern")
    # How many entries each pattern actually hits — a pattern that matches
    # nothing is almost always a typo, and you cannot see that from the text.
    knx, ha = [], []
    for pid, kind, pattern, note in rows:
        if kind == "knx":
            hits = query(db.KNX_DSN,
                         "SELECT count(*) FROM knx_ga WHERE address LIKE %s",
                         (pattern,))[0][0]
            knx.append((pid, pattern, note, hits))
        else:
            hits = query(db.HA_DSN,
                         "SELECT count(*) FROM ha_entity WHERE entity_id LIKE %s",
                         (pattern,))[0][0]
            ha.append((pid, pattern, note, hits))
    return page(request, "exclusions.html", knx=knx, ha=ha,
                knx_examples=KNX_EXAMPLES, ha_examples=HA_EXAMPLES)


@app.post("/exclusions/add")
def exclusion_add(kind: str = Form(...), pattern: str = Form(...),
                  note: str = Form(""), _: str = Depends(auth)):
    if kind in ("knx", "ha") and pattern.strip():
        execute(db.KNX_DSN,
                "INSERT INTO archive_exclude_pattern (kind, pattern, note)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (kind, pattern.strip(), note or None))
    return RedirectResponse("/exclusions", status_code=303)


@app.post("/exclusions/delete")
def exclusion_delete(id: int = Form(...), _: str = Depends(auth)):
    execute(db.KNX_DSN, "DELETE FROM archive_exclude_pattern WHERE id = %s",
            (id,))
    return RedirectResponse("/exclusions", status_code=303)


# ── MQTT ──────────────────────────────────────────────────────────────────
TARGET_DBS = {"knx_data": db.KNX_DSN, "ha_data": db.HA_DSN,
              "power_data": db.POWER_DSN}


def _columns_of(target_db: str, table: str) -> list[str]:
    dsn = TARGET_DBS.get(target_db)
    if not dsn:
        return []
    return [r[0] for r in query(
        dsn, "SELECT column_name FROM information_schema.columns"
             " WHERE table_schema='public' AND table_name=%s"
             "   AND column_name <> 'time' ORDER BY ordinal_position", (table,))]


def _tables_of(target_db: str) -> list[str]:
    """Tables that can receive rows — plain tables and hypertables, no views."""
    dsn = TARGET_DBS.get(target_db)
    if not dsn:
        return []
    return [r[0] for r in query(
        dsn, "SELECT table_name FROM information_schema.tables"
             " WHERE table_schema='public' AND table_type='BASE TABLE'"
             " ORDER BY table_name")]


def _all_tables() -> dict[str, list[str]]:
    return {name: _tables_of(name) for name in TARGET_DBS}


def _flatten(payload, prefix="") -> list[tuple[str, object]]:
    """Every leaf of the payload as a dotted path — the list the mapping is
    built from, so nobody has to type paths by hand."""
    out = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            out += _flatten(value, f"{prefix}{key}.")
    else:
        out.append((prefix.rstrip("."), payload))
    return out


@app.get("/settings/mqtt", response_class=HTMLResponse)
def mqtt_config(request: Request, saved: str = "", _: str = Depends(auth)):
    cfg = dict(query(db.KNX_DSN,
                     "SELECT key, value FROM settings WHERE key LIKE %s"
                     " ORDER BY key", ("mqtt.%",)))
    topics = query(db.KNX_DSN,
                   "SELECT id, topic, target_db, target_table, time_path,"
                   " time_is_local, enabled, note, last_seen, last_error"
                   " FROM mqtt_topic ORDER BY id")
    fields = {}
    for t in topics:
        fields[t[0]] = query(db.KNX_DSN,
                             "SELECT id, json_path, column_name, scale, note"
                             " FROM mqtt_field WHERE topic_id=%s"
                             " ORDER BY column_name", (t[0],))
    import mqtt_source
    return page(request, "settings_mqtt.html", cfg=cfg, topics=topics, fields=fields,
                saved=saved,
                password_stored=mqtt_source.PASSWORD_FILE.exists(),
                target_dbs=sorted(TARGET_DBS), tables=_all_tables())


@app.post("/settings/mqtt/broker")
def mqtt_broker(request: Request, enabled: str = Form(""),
                host: str = Form(""), port: str = Form("1883"),
                tls: str = Form(""), tls_insecure: str = Form(""),
                ca_cert: str = Form(""), username: str = Form(""),
                client_id: str = Form("homearchive"), qos: str = Form("0"),
                password: str = Form(""), _: str = Depends(auth)):
    for key, value in [("mqtt.enabled", "true" if enabled else "false"),
                       ("mqtt.host", host.strip()),
                       ("mqtt.port", port.strip() or "1883"),
                       ("mqtt.tls", "true" if tls else "false"),
                       ("mqtt.tls_insecure", "true" if tls_insecure else "false"),
                       ("mqtt.ca_cert", ca_cert.strip()),
                       ("mqtt.username", username.strip()),
                       ("mqtt.client_id", client_id.strip() or "homearchive"),
                       ("mqtt.qos", qos.strip() or "0")]:
        execute(db.KNX_DSN,
                "UPDATE settings SET value=%s, updated_at=now() WHERE key=%s",
                (value, key))
    if password:
        import mqtt_source
        mqtt_source.STATE_DIR.mkdir(parents=True, exist_ok=True)
        mqtt_source.PASSWORD_FILE.write_text(password)
        mqtt_source.PASSWORD_FILE.chmod(0o600)
    return RedirectResponse("/settings/mqtt?saved=broker", status_code=303)


@app.post("/settings/mqtt/topic")
def mqtt_topic_add(topic: str = Form(...), target_db: str = Form(...),
                   target_table: str = Form(...), time_path: str = Form(""),
                   note: str = Form(""), _: str = Depends(auth)):
    if target_db not in TARGET_DBS:
        raise HTTPException(400, "unknown target database")
    rows = query(db.KNX_DSN,
                 "INSERT INTO mqtt_topic (topic, target_db, target_table,"
                 " time_path, note) VALUES (%s,%s,%s,%s,%s)"
                 " ON CONFLICT (topic) DO UPDATE SET target_db=excluded.target_db,"
                 " target_table=excluded.target_table,"
                 " time_path=excluded.time_path, note=excluded.note"
                 " RETURNING id",
                 (topic.strip(), target_db, target_table.strip(),
                  time_path.strip() or None, note or None))
    # Straight into the mapping — that is the next step, and hiding it behind
    # a second click made it undiscoverable.
    return RedirectResponse(f"/mqtt/map?topic_id={rows[0][0]}", status_code=303)


@app.post("/settings/mqtt/topic/delete")
def mqtt_topic_delete(id: int = Form(...), _: str = Depends(auth)):
    execute(db.KNX_DSN, "DELETE FROM mqtt_topic WHERE id=%s", (id,))
    return RedirectResponse("/settings/mqtt", status_code=303)


@app.post("/settings/mqtt/topic/toggle")
def mqtt_topic_toggle(id: int = Form(...), _: str = Depends(auth)):
    execute(db.KNX_DSN,
            "UPDATE mqtt_topic SET enabled = NOT enabled WHERE id=%s", (id,))
    return RedirectResponse("/settings/mqtt", status_code=303)


@app.get("/mqtt/map", response_class=HTMLResponse)
def mqtt_map(request: Request, topic_id: int, saved: str = "",
             _: str = Depends(auth)):
    rows = query(db.KNX_DSN,
                 "SELECT id, topic, target_db, target_table, sample_payload,"
                 " sample_at, sample_source FROM mqtt_topic WHERE id=%s",
                 (topic_id,))
    if not rows:
        raise HTTPException(404, "unknown topic")
    tid, topic, tdb, table, sample, sample_at, sample_source = rows[0]
    fields = query(db.KNX_DSN,
                   "SELECT id, json_path, column_name, scale, note"
                   " FROM mqtt_field WHERE topic_id=%s ORDER BY column_name",
                   (tid,))
    mapped = {f[1] for f in fields}
    used = {f[2] for f in fields}
    leaves = _flatten(sample) if sample else []
    return page(request, "mqtt_map.html", tid=tid, topic=topic, target_db=tdb,
                target_table=table, fields=fields, mapped=mapped, used=used,
                leaves=leaves, sample_at=sample_at,
                sample_source=sample_source, saved=saved,
                columns=_columns_of(tdb, table))


@app.post("/mqtt/map/sample")
def mqtt_map_sample(topic_id: int = Form(...), payload: str = Form(...),
                    _: str = Depends(auth)):
    """Paste a telegram to build the mapping before the broker is connected."""
    try:
        json.loads(payload)
    except ValueError:
        return RedirectResponse(f"/mqtt/map?topic_id={topic_id}&saved=invalid",
                                status_code=303)
    execute(db.KNX_DSN,
            "UPDATE mqtt_topic SET sample_payload=%s::jsonb, sample_at=now(),"
            " sample_source='manual' WHERE id=%s", (payload, topic_id))
    return RedirectResponse(f"/mqtt/map?topic_id={topic_id}", status_code=303)


@app.post("/mqtt/map/add")
def mqtt_map_add(topic_id: int = Form(...), json_path: str = Form(...),
                 column_name: str = Form(...), scale: str = Form("1"),
                 _: str = Depends(auth)):
    try:
        factor = float(scale)
    except ValueError:
        factor = 1.0
    execute(db.KNX_DSN,
            "INSERT INTO mqtt_field (topic_id, json_path, column_name, scale)"
            " VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (topic_id, column_name) DO UPDATE"
            " SET json_path=excluded.json_path, scale=excluded.scale",
            (topic_id, json_path.strip(), column_name.strip(), factor))
    return RedirectResponse(f"/mqtt/map?topic_id={topic_id}", status_code=303)


@app.post("/mqtt/map/delete")
def mqtt_map_delete(id: int = Form(...), topic_id: int = Form(...),
                    _: str = Depends(auth)):
    execute(db.KNX_DSN, "DELETE FROM mqtt_field WHERE id=%s", (id,))
    return RedirectResponse(f"/mqtt/map?topic_id={topic_id}", status_code=303)


# ── settings ──────────────────────────────────────────────────────────────
SETTINGS_TABS = [("general", "Allgemein"), ("knx", "KNX"),
                 ("mqtt", "MQTT"), ("import", "ETS-Import")]


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, _: str = Depends(auth)):
    # knx.* and mqtt.* live on their own pages — the same setting in two
    # places invites contradictory edits.
    rows = query(db.KNX_DSN,
                 "SELECT key, value, note FROM settings"
                 " WHERE key NOT LIKE %s AND key NOT LIKE %s ORDER BY key",
                 ("knx.%", "mqtt.%"))
    return page(request, "settings.html", rows=rows)


@app.post("/settings/save")
async def settings_save(request: Request, _: str = Depends(auth)):
    form = await request.form()
    for key, value in form.items():
        if key.startswith("v:"):
            execute(db.KNX_DSN,
                    "UPDATE settings SET value = %s, updated_at = now()"
                    " WHERE key = %s", (value, key[2:]))
    return RedirectResponse("/settings", status_code=303)


# ── .knxproj import ───────────────────────────────────────────────────────
def run_import(job_id: str, path: str, password: str | None) -> None:
    job = JOBS[job_id]
    try:
        from import_knxproj import load_project
        job["state"] = "parsing"
        project = load_project(path, password)
        job["project"] = project
        with psycopg.connect(db.KNX_DSN) as conn:
            cur = conn.cursor()
            cur.execute("SELECT address, name, dpt, locked FROM knx_ga")
            existing = {r[0]: {"name": r[1], "dpt": r[2], "locked": r[3]}
                        for r in cur.fetchall()}
        added, changed, pinned = [], [], []
        for addr, new in project.items():
            old = existing.get(addr)
            if old is None:
                added.append((addr, new["dpt"], new["name"]))
            elif old["locked"]:
                if old["name"] != new["name"] or old["dpt"] != new["dpt"]:
                    pinned.append((addr, old["dpt"], new["dpt"], old["name"]))
            elif old["name"] != new["name"] or old["dpt"] != new["dpt"]:
                changed.append((addr, old["dpt"], new["dpt"], old["name"],
                                new["name"]))
        job.update(state="ready", added=added, changed=changed, pinned=pinned,
                   vanished=[a for a in existing if a not in project])
    except Exception as exc:  # noqa: BLE001 — shown to the user
        job.update(state="error", error=str(exc))


@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request, job: str = "", _: str = Depends(auth)):
    return page(request, "import.html", job=JOBS.get(job), job_id=job)


@app.post("/import")
async def import_upload(file: UploadFile, password: str = Form(""),
                        _: str = Depends(auth)):
    tmp = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.knxproj")
    with open(tmp, "wb") as fh:
        fh.write(await file.read())
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"state": "queued", "file": file.filename, "path": tmp}
    threading.Thread(
        target=run_import,
        args=(job_id, tmp, password or os.environ.get("KNX_PROJECT_PASSWORD")),
        daemon=True,
    ).start()
    return RedirectResponse(f"/import?job={job_id}", status_code=303)


@app.post("/import/apply")
def import_apply(job_id: str = Form(...), _: str = Depends(auth)):
    job = JOBS.get(job_id)
    if not job or job.get("state") != "ready":
        raise HTTPException(400, "no parsed project for this job")
    project = job["project"]
    with psycopg.connect(db.KNX_DSN, autocommit=True) as conn:
        cur = conn.cursor()
        cur.executemany(
            """INSERT INTO knx_ga (address, name, dpt, description, origin,
                                   updated_at)
               VALUES (%s, %s, %s, %s, 'ets', now())
               ON CONFLICT (address) DO UPDATE SET
                   name = excluded.name, dpt = excluded.dpt,
                   description = excluded.description, origin = 'ets',
                   updated_at = now()
               WHERE NOT knx_ga.locked""",
            [(a, g["name"], g["dpt"], g["description"])
             for a, g in project.items()],
        )
        cur.execute(
            """INSERT INTO knx_import_log (source_file, added, changed,
                                           vanished)
               VALUES (%s, %s, %s, %s)""",
            (job["file"], len(job["added"]), len(job["changed"]),
             len(job["vanished"])),
        )
    job["state"] = "applied"
    with contextlib.suppress(OSError):
        os.unlink(job["path"])
    return RedirectResponse(f"/import?job={job_id}", status_code=303)


if __name__ == "__main__":
    if "--hash" in sys.argv:
        import getpass
        print(hash_password(getpass.getpass("password: ")))
        sys.exit(0)
    import uvicorn
    uvicorn.run(app, host=os.environ.get("UI_BIND", "0.0.0.0"),
                port=int(os.environ.get("UI_PORT", "8080")))
