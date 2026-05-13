#!/bin/bash
# Speichert den aktuellen Stand der Datenbank als neuen Test-Snapshot.
# Überschreibt den bisherigen Snapshot!

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$SCRIPT_DIR/db.sqlite3"
SNAPSHOT="$SCRIPT_DIR/db.sqlite3.test_snapshot"

if [ ! -f "$DB" ]; then
    echo "Fehler: Datenbank nicht gefunden ($DB)"
    exit 1
fi

cp "$DB" "$SNAPSHOT"
echo "Snapshot gespeichert: db.sqlite3.test_snapshot"
