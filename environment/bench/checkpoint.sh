#!/usr/bin/env bash
# Save the CURRENT Dolibarr state as the baseline that every test starts from.
# Run this ONCE, when the ERP is in the exact starting state you want.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

echo ">> Dumping database '$DB_NAME' from $DB_CONTAINER ..."
docker exec "$DB_CONTAINER" mariadb-dump -u"$DB_ROOT_USER" -p"$DB_ROOT_PASS" \
    $DUMP_FLAGS "$DB_NAME" > "$BASELINE_SQL"

echo ">> Archiving documents from $DOCS_DIR ..."
tar -czf "$BASELINE_DOCS" -C "$DOCS_DIR" .

echo ">> Recording baseline table row-counts ..."
db_table_counts > "$BASELINE_DIR/counts.tsv"

echo ">> Recording baseline document manifest ..."
( cd "$DOCS_DIR" && find . -type f | sort ) > "$BASELINE_DIR/docs.list"

echo ">> Baseline saved:"
echo "     $BASELINE_SQL   ($(du -h "$BASELINE_SQL"  | cut -f1))"
echo "     $BASELINE_DOCS  ($(du -h "$BASELINE_DOCS" | cut -f1))"
echo "     $BASELINE_DIR/counts.tsv ($(wc -l < "$BASELINE_DIR/counts.tsv") tables)"
