#!/usr/bin/env bash
# One-time PostgreSQL setup for wxparser on the deployment host (Fedora).
# Installs the server + the BSD pg8000 driver, initializes the cluster, and
# creates the wxparser role/databases with local-trust auth. Idempotent.
set -euo pipefail

echo "==> install postgresql-server + pg8000 (BSD driver)"
sudo dnf -y install postgresql-server postgresql python3-pg8000

echo "==> init cluster (if needed) + enable/start"
if [ ! -f /var/lib/pgsql/data/PG_VERSION ]; then
    sudo postgresql-setup --initdb
fi
sudo systemctl enable --now postgresql

echo "==> role + databases"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='wxparser'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE ROLE wxparser LOGIN"
for db in wxparser wxparser_test; do
    sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
        || sudo -u postgres createdb -O wxparser "$db"
done

echo "==> pg_hba: local TCP trust for the wxparser role"
HBA=/var/lib/pgsql/data/pg_hba.conf
if ! sudo grep -q "wxparser-local-trust" "$HBA"; then
    sudo bash -c "printf '# wxparser-local-trust\nhost wxparser,wxparser_test wxparser 127.0.0.1/32 trust\nhost wxparser,wxparser_test wxparser ::1/128 trust\n' | cat - '$HBA' > /tmp/hba.new \
        && cp /tmp/hba.new '$HBA' && chown postgres:postgres '$HBA' && chmod 600 '$HBA'"
    sudo systemctl reload postgresql
fi

echo "==> done. Connection: wxparser@127.0.0.1:5432/wxparser (trust)."
echo "    For password auth instead, set WX_PG_PASSWORD and switch pg_hba to scram-sha-256."
