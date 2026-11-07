# Shared config for the Dolibarr test-bench scripts.
# Sourced by checkpoint.sh / reset.sh / snapshot.sh.

DB_CONTAINER="dolibarr-mariadb-1"
WEB_CONTAINER="dolibarr-web-1"

DB_NAME="dolidb"
DB_ROOT_USER="root"
DB_ROOT_PASS="root"

# Host path bind-mounted to the web container's /var/www/documents
DOCS_DIR="/home/documents"

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASELINE_DIR="$BENCH_DIR/baseline"
RESULTS_DIR="$BENCH_DIR/results"

BASELINE_SQL="$BASELINE_DIR/db.sql"
BASELINE_DOCS="$BASELINE_DIR/documents.tar.gz"

# mariadb-dump flags: consistent hot dump, include routines/triggers/events.
DUMP_FLAGS="--single-transaction --routines --triggers --events --skip-dump-date"

# Emit "table_name<TAB>row_count" for every base table, sorted. Exact COUNT(*),
# not the approximate information_schema.table_rows.
db_table_counts() {
    local q
    q=$(docker exec "$DB_CONTAINER" mariadb -u"$DB_ROOT_USER" -p"$DB_ROOT_PASS" -N -B "$DB_NAME" -e "
        SET SESSION group_concat_max_len=10000000;
        SELECT GROUP_CONCAT(
                 CONCAT('SELECT ''', table_name, ''' AS t, COUNT(*) AS c FROM \`', table_name, '\`')
                 SEPARATOR ' UNION ALL ')
        FROM information_schema.tables
        WHERE table_schema='$DB_NAME' AND table_type='BASE TABLE';")
    docker exec "$DB_CONTAINER" mariadb -u"$DB_ROOT_USER" -p"$DB_ROOT_PASS" -N -B "$DB_NAME" -e "$q" | sort
}
