#!/usr/bin/env bash
# Capture the post-test state and show what the agent changed vs baseline.
# Run this AFTER a test, before the next reset.
#   ./snapshot.sh <test-name>
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

NAME="${1:-}"
[[ -n "$NAME" ]] || { echo "usage: $0 <test-name>" >&2; exit 1; }
[[ -f "$BASELINE_DIR/counts.tsv" ]] || { echo "!! No baseline. Run checkpoint.sh first." >&2; exit 1; }

OUT="$RESULTS_DIR/$NAME"
mkdir -p "$OUT"

echo ">> Saving full state for '$NAME' ..."
docker exec "$DB_CONTAINER" mariadb-dump -u"$DB_ROOT_USER" -p"$DB_ROOT_PASS" \
    $DUMP_FLAGS "$DB_NAME" > "$OUT/db.sql"
tar -czf "$OUT/documents.tar.gz" -C "$DOCS_DIR" .

echo ">> Computing diff vs baseline ..."
db_table_counts > "$OUT/counts.tsv"
( cd "$DOCS_DIR" && find . -type f | sort ) > "$OUT/docs.list"

REPORT="$OUT/diff.txt"
{
    echo "# Diff for test '$NAME' (vs baseline)"
    echo

    echo "## Table row-count changes"
    join -t$'\t' -a1 -a2 -e '-' -o '0,1.2,2.2' \
        "$BASELINE_DIR/counts.tsv" "$OUT/counts.tsv" \
    | awk -F'\t' '$2!=$3 {
        d = ($2=="-"?0:$2); n = ($3=="-"?0:$3);
        printf "  %-40s %s -> %s  (%+d)\n", $1, $2, $3, n-d }'
    echo

    echo "## Document files added"
    comm -13 "$BASELINE_DIR/docs.list" "$OUT/docs.list" | sed 's/^/  + /'
    echo "## Document files removed"
    comm -23 "$BASELINE_DIR/docs.list" "$OUT/docs.list" | sed 's/^/  - /'
} > "$REPORT"

echo ">> Saved to $OUT/"
echo "-----------------------------------------------------------"
cat "$REPORT"
