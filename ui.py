"""knx-ha2TimeScale — web UI.

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
import os
import secrets
import sys
import tempfile
import threading
import uuid
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

import db

BASE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
app = FastAPI(title="knx-ha2TimeScale")
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
    ha_counts = query(db.HA_DSN,
                      "SELECT count(*) FILTER (WHERE archive), count(*)"
                      " FROM ha_entity")[0]
    return page(request, "status.html", st=st, no_dpt=no_dpt, imports=imports,
                ga_counts=counts, ha_counts=ha_counts)


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


# ── patterns ──────────────────────────────────────────────────────────────
@app.get("/patterns", response_class=HTMLResponse)
def patterns(request: Request, _: str = Depends(auth)):
    rows = query(db.KNX_DSN,
                 "SELECT id, kind, pattern, note FROM archive_exclude_pattern"
                 " ORDER BY kind, pattern")
    return page(request, "patterns.html", rows=rows)


@app.post("/patterns/add")
def pattern_add(kind: str = Form(...), pattern: str = Form(...),
                note: str = Form(""), _: str = Depends(auth)):
    if kind in ("knx", "ha") and pattern.strip():
        execute(db.KNX_DSN,
                "INSERT INTO archive_exclude_pattern (kind, pattern, note)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (kind, pattern.strip(), note or None))
    return RedirectResponse("/patterns", status_code=303)


@app.post("/patterns/delete")
def pattern_delete(id: int = Form(...), _: str = Depends(auth)):
    execute(db.KNX_DSN, "DELETE FROM archive_exclude_pattern WHERE id = %s",
            (id,))
    return RedirectResponse("/patterns", status_code=303)


# ── settings ──────────────────────────────────────────────────────────────
@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, _: str = Depends(auth)):
    rows = query(db.KNX_DSN,
                 "SELECT key, value, note FROM settings ORDER BY key")
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
