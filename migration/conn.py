"""Shared read-only connection helpers. Credentials come from ~/.my.cnf and ~/.pgpass."""
import configparser
import os
import pymysql
import psycopg


def mysql_conn(db="db_knx_lts"):
    cp = configparser.ConfigParser()
    cp.read(os.path.expanduser("~/.my.cnf"))
    c = cp["client"]
    return pymysql.connect(
        host=c["host"],
        user=c["user"],
        password=c["password"],
        database=db,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def pg_conn(db="knx_data", host="<db-host>", user="postgres"):
    # libpq reads ~/.pgpass automatically when no password is given
    return psycopg.connect(host=host, dbname=db, user=user, port=5432)
