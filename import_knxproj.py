"""Import group addresses from an ETS .knxproj into knx_ga.

Dry run by default: prints the diff and writes nothing. Pass --apply to commit.
ETS is authoritative — an import overwrites name and DPT for every address it
carries. The archive flag is never touched, so exclusions survive an import.

Addresses that exist in knx_ga but not in the project are reported as
"vanished" and left alone; historic measurements still reference them.

The project password is read from ~/.knx.pwd or $KNX_PROJECT_PASSWORD.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

from xknxproject import XKNXProj

from db import knx_conn, normalise_dpt


def read_password(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if env := os.environ.get("KNX_PROJECT_PASSWORD"):
        return env
    pwfile = pathlib.Path.home() / ".knx.pwd"
    if pwfile.exists():
        return pwfile.read_text().strip()
    return None


def load_project(path: str, password: str | None) -> dict[str, dict]:
    project = XKNXProj(path=path, password=password).parse()
    out = {}
    for addr, ga in project["group_addresses"].items():
        out[addr] = {
            "name": ga.get("name") or None,
            "dpt": normalise_dpt(ga.get("dpt")),
            "description": ga.get("description") or None,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("knxproj", help="path to the .knxproj file")
    ap.add_argument("--password", help="ETS project password")
    ap.add_argument("--apply", action="store_true", help="write changes")
    ap.add_argument("--limit", type=int, default=25, help="diff lines per section")
    args = ap.parse_args()

    password = read_password(args.password)
    print(f"parsing {args.knxproj} ...", flush=True)
    project = load_project(args.knxproj, password)
    print(f"  {len(project)} group addresses in project", flush=True)

    with knx_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT address, name, dpt, origin FROM knx_ga")
        existing = {r[0]: {"name": r[1], "dpt": r[2], "origin": r[3]}
                    for r in cur.fetchall()}

        added, changed, unchanged = [], [], 0
        for addr, new in project.items():
            old = existing.get(addr)
            if old is None:
                added.append((addr, new))
            elif old["name"] != new["name"] or old["dpt"] != new["dpt"]:
                changed.append((addr, old, new))
            else:
                unchanged += 1
        vanished = [a for a in existing if a not in project]

        print()
        print(f"new:       {len(added)}")
        print(f"changed:   {len(changed)}")
        print(f"unchanged: {unchanged}")
        print(f"vanished:  {len(vanished)}  (kept, not deleted)")

        if added:
            print("\n--- new ---")
            for addr, new in added[: args.limit]:
                print(f"  {addr:<10} {new['dpt'] or '-':<8} {new['name']}")
            if len(added) > args.limit:
                print(f"  ... {len(added) - args.limit} more")

        if changed:
            print("\n--- changed ---")
            for addr, old, new in changed[: args.limit]:
                bits = []
                if old["dpt"] != new["dpt"]:
                    bits.append(f"dpt {old['dpt']} -> {new['dpt']}")
                if old["name"] != new["name"]:
                    bits.append(f"name {old['name']!r} -> {new['name']!r}")
                print(f"  {addr:<10} [{old['origin']}] " + "; ".join(bits))
            if len(changed) > args.limit:
                print(f"  ... {len(changed) - args.limit} more")

        if vanished:
            print("\n--- vanished (in DB, not in project) ---")
            for addr in vanished[: args.limit]:
                print(f"  {addr:<10} {existing[addr]['name']}")
            if len(vanished) > args.limit:
                print(f"  ... {len(vanished) - args.limit} more")

        no_dpt = [a for a, g in project.items() if not g["dpt"]]
        if no_dpt:
            print(f"\n--- without DPT in ETS ({len(no_dpt)}) — raw value path ---")
            for addr in no_dpt[: args.limit]:
                print(f"  {addr:<10} {project[addr]['name']}")

        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply.")
            return

        cur.executemany(
            """
            INSERT INTO knx_ga (address, name, dpt, description, origin, updated_at)
            VALUES (%s, %s, %s, %s, 'ets', now())
            ON CONFLICT (address) DO UPDATE SET
                name        = excluded.name,
                dpt         = excluded.dpt,
                description = excluded.description,
                origin      = 'ets',
                updated_at  = now()
            """,
            [(a, g["name"], g["dpt"], g["description"]) for a, g in project.items()],
        )
        cur.execute(
            """INSERT INTO knx_import_log (source_file, added, changed, vanished)
               VALUES (%s, %s, %s, %s)""",
            (os.path.basename(args.knxproj), len(added), len(changed), len(vanished)),
        )
        conn.commit()
        print(f"\napplied: {len(added)} new, {len(changed)} updated.")


if __name__ == "__main__":
    sys.exit(main())
