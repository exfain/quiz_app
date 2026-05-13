#!/bin/bash
# Stellt den Test-Snapshot der Datenbank wieder her.
# Alle seit dem Snapshot gemachten Änderungen gehen verloren.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$SCRIPT_DIR/db.sqlite3"
SNAPSHOT="$SCRIPT_DIR/db.sqlite3.test_snapshot"

if [ ! -f "$SNAPSHOT" ]; then
    echo "Fehler: Snapshot nicht gefunden ($SNAPSHOT)"
    exit 1
fi

cp "$SNAPSHOT" "$DB"
echo "Datenbank wiederhergestellt aus: db.sqlite3.test_snapshot"
