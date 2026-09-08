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
               now() - updated_at AS age
        FROM collector_status WHERE id = 1""")
    cols = ["updated_at", "started_at", "knx_connected", "ha_connected",
            "knx_received", "knx_written", "knx_skipped", "knx_undecoded",
            "knx_dropped", "ha_received", "ha_written", "ha_skipped",
            "ha_dropped", "age"]
    st = dict(zip(cols, rows[0])) if rows else {}
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
    ga = dict(zip(cols, rows[0]))
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


@app.get("/knx/connection", response_class=HTMLResponse)
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
    return page(request, "connection.html", cfg=cfg, st=st, error=error,
                interfaces=interfaces, modes=MODES, mode=mode,
                is_secure=mode in SECURE_MODES,
                is_tunnel=mode in TUNNEL_MODES, saved=saved)


@app.post("/knx/connection")
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
    return RedirectResponse("/knx/connection?saved=ok", status_code=303)


@app.post("/knx/connection/keyring")
async def connection_keyring(file: UploadFile, password: str = Form(...),
                             _: str = Depends(auth)):
    import keyring_store
    try:
        keyring_store.store(await file.read(), password)
    except Exception:  # noqa: BLE001 — wrong password or not a keyring
        return RedirectResponse("/knx/connection?saved=invalid",
                                status_code=303)
    execute(db.KNX_DSN,
            "UPDATE settings SET value = %s, updated_at = now()"
            " WHERE key = 'knx.keyring_path'",
            (str(keyring_store.KEYRING_FILE),))
    return RedirectResponse("/knx/connection?saved=keyring", status_code=303)


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
    try:
        writer.SPOOL_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return RedirectResponse("/messages", status_code=303)


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


# ── settings ──────────────────────────────────────────────────────────────
@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, _: str = Depends(auth)):
    # knx.* lives on its own page — everything about one connection in one
    # place beats a flat key/value list.
    rows = query(db.KNX_DSN,
                 "SELECT key, value, note FROM settings"
                 " WHERE key NOT LIKE %s ORDER BY key", ("knx.%",))
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
    try:
        os.unlink(job["path"])
    except OSError:
        pass
    return RedirectResponse(f"/import?job={job_id}", status_code=303)


if __name__ == "__main__":
    if "--hash" in sys.argv:
        import getpass
        print(hash_password(getpass.getpass("password: ")))
        sys.exit(0)
    import uvicorn
    uvicorn.run(app, host=os.environ.get("UI_BIND", "0.0.0.0"),
                port=int(os.environ.get("UI_PORT", "8080")))
