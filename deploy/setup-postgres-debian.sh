#!/usr/bin/env bash
# One-time PostgreSQL setup for wxparser on a Debian/Ubuntu host — the apt
# counterpart of setup-postgres.sh (Fedora). Installs the server + the BSD
# pg8000 driver and creates the wxparser role/databases with local-trust auth.
# Idempotent.
set -euo pipefail

echo "==> install postgresql + pg8000 (BSD driver)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install postgresql python3-pip
# Debian's python3-pg8000 package is ancient (1.10, no pg8000.native) — the app
# needs >=1.31, so it comes from pip. --break-system-packages is deliberate:
# the services run the system python3, so that's where the module must live.
sudo python3 -m pip install --break-system-packages --quiet 'pg8000>=1.31'

echo "==> enable/start (apt initializes the cluster itself)"
sudo systemctl enable --now postgresql

echo "==> role + databases"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='wxparser'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE ROLE wxparser LOGIN"
for db in wxparser wxparser_test; do
    sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
        || sudo -u postgres createdb -O wxparser "$db"
done

echo "==> pg_hba: local TCP trust for the wxparser role"
HBA=$(sudo -u postgres psql -tAc "SHOW hba_file")
if ! sudo grep -q "wxparser-local-trust" "$HBA"; then
    sudo bash -c "printf '# wxparser-local-trust\nhost wxparser,wxparser_test wxparser 127.0.0.1/32 trust\nhost wxparser,wxparser_test wxparser ::1/128 trust\n' | cat - '$HBA' > /tmp/hba.new \
        && cp /tmp/hba.new '$HBA' && chown postgres:postgres '$HBA' && chmod 640 '$HBA'"
    sudo systemctl reload postgresql
fi

echo "==> done. Connection: wxparser@127.0.0.1:5432/wxparser (trust)."
echo "    For password auth instead, set WX_PG_PASSWORD and switch pg_hba to scram-sha-256."
