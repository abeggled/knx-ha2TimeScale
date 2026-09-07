"""Create the read-mostly database role used by the web UI.

The UI never touches measurement tables — only configuration and the
collector heartbeat. Run once, prints nothing secret to the terminal;
the generated password lands in the file given as argument.
"""
import secrets
import string
import sys

from psycopg import sql

from db import ha_conn, knx_conn

KNX_GRANTS = """
GRANT CONNECT ON DATABASE knx_data TO webui;
GRANT USAGE ON SCHEMA public TO webui;
GRANT SELECT ON collector_status, knx_dpt_unit TO webui;
-- Read-only on the measurements: the group address detail page shows the
-- last values so a DPT correction can be verified. No INSERT/UPDATE/DELETE.
GRANT SELECT ON knx_measurements TO webui;
GRANT SELECT, UPDATE ON settings TO webui;
GRANT SELECT, INSERT, UPDATE ON knx_ga TO webui;
GRANT SELECT, INSERT ON knx_import_log TO webui;
GRANT SELECT, INSERT, DELETE ON archive_exclude_pattern TO webui;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO webui;
"""

HA_GRANTS = """
GRANT CONNECT ON DATABASE ha_data TO webui;
GRANT USAGE ON SCHEMA public TO webui;
GRANT SELECT, UPDATE ON ha_entity TO webui;
"""


def main(out_path: str, host: str) -> None:
    alphabet = string.ascii_letters + string.digits
    pw = "".join(secrets.choice(alphabet) for _ in range(32))

    with knx_conn(autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'webui'")
        stmt = ("ALTER ROLE webui WITH LOGIN PASSWORD {}" if cur.fetchone()
                else "CREATE ROLE webui LOGIN PASSWORD {}")
        cur.execute(sql.SQL(stmt).format(sql.Literal(pw)))
        cur.execute(KNX_GRANTS)
    print("knx_data: role webui ready")

    with ha_conn(autocommit=True) as conn:
        conn.cursor().execute(HA_GRANTS)
    print("ha_data: grants applied")

    with open(out_path, "w") as fh:
        fh.write(
            f'KNX_DSN="host={host} dbname=knx_data user=webui password={pw}"\n'
            f'HA_DSN="host={host} dbname=ha_data user=webui password={pw}"\n'
            'UI_USER="admin"\n'
            'UI_PASSWORD_HASH=""\n'
            'UI_BIND="0.0.0.0"\n'
            'UI_PORT="8080"\n'
        )
    print(f"environment written to {out_path} — fill in UI_PASSWORD_HASH")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ui.env",
         sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1")
