# Einmalige Migration, September 2026

Diese Skripte liefen genau einmal und sind hier zur Nachvollziehbarkeit
abgelegt — sie gehören nicht zum laufenden Betrieb.

| Datei | Zweck |
|---|---|
| `migrate_knx.py` | 512'440'493 Telegramme aus 54 MariaDB-Monatstabellen (`db_knx_LTS`) nach `knx_data.knx_measurements` |
| `migrate_power.py` | 35'337'122 Zeilen aus `db_power_LTS` nach `power_data.bkw_measurements` |
| `setup_power.py` | Anlage von `power_data`, Hypertable und `bkw_hourly` |
| `power_epochs.sql` | Zählerepochen und die kumulierten Sichten |
| `dpt_units.py` | DPT-zu-Einheit-Abbildung, aus dem Zielbestand rekonstruiert |
| `*.log` | Protokolle der beiden Läufe |

Festgehaltene Entscheidungen: Zeitstempel als UTC gelesen (DST-sicher),
Quelle am Minimum des Ziels abgeschnitten, `ID` verworfen, `knxunit` aus dem
DPT abgeleitet, NUL-Bytes aus `knxvalue` entfernt (104'674 Zeilen, DPT 16
füllt mit NUL auf), PV-Daten bewusst nicht migriert.
