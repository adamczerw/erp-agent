#!/usr/bin/env bash
# Restore the ERP to the baseline. Run this BEFORE every test.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

[[ -f "$BASELINE_SQL"  ]] || { echo "!! No baseline DB found. Run checkpoint.sh first."   >&2; exit 1; }
[[ -f "$BASELINE_DOCS" ]] || { echo "!! No baseline docs found. Run checkpoint.sh first." >&2; exit 1; }

echo ">> Restoring database '$DB_NAME' ..."
# Drop + recreate so tables the agent CREATED are also removed, then reload.
docker exec "$DB_CONTAINER" mariadb -u"$DB_ROOT_USER" -p"$DB_ROOT_PASS" \
    -e "DROP DATABASE IF EXISTS \`$DB_NAME\`; CREATE DATABASE \`$DB_NAME\`;"
docker exec -i "$DB_CONTAINER" mariadb -u"$DB_ROOT_USER" -p"$DB_ROOT_PASS" \
    "$DB_NAME" < "$BASELINE_SQL"

echo ">> Restoring documents ..."
# Wipe current contents (incl. dotfiles) then unpack baseline.
find "$DOCS_DIR" -mindepth 1 -delete
tar -xzf "$BASELINE_DOCS" -C "$DOCS_DIR"

echo ">> Reset complete — ERP is back at baseline."
