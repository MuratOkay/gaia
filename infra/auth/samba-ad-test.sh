#!/usr/bin/env bash
# A throwaway Active Directory for verifying Phase 5A (OWNER_VERIFICATION §22d; P5-10).
#
# Samba 4 as an AD domain controller in a Debian container, with the same people and
# groups as the offline test directory (tests/unit/_fake_directory.py), so the host result
# can be read against the sandbox's:
#
#   ACME.TEST  (dc=acme,dc=test), LDAPS on 127.0.0.1:6360
#   OU=Service     svc-gaia (Gaia's read-only service account)
#   OU=People/Finance  akaya  — GAIA-Finance (department Finance)
#   OU=People/HR       bdemir — Team-HR, which is a member of GAIA-HR (nested)
#                      cpasif — disabled, GAIA-Finance
#   OU=People/IT       dyilmaz — GAIA-Admins and GAIA-Finance
#                      enorole — in no Gaia group (must not get in)
#   OU=Groups/Gaia     GAIA-Finance, GAIA-HR, GAIA-Admins      OU=Groups/Teams  Team-HR
#
# NOT run in the assistant's sandbox (no Docker there): written from samba-tool's
# documented commands. If a step fails, send the output; older Samba versions call
# `samba-tool ou add` `ou create`.
#
#   infra/auth/samba-ad-test.sh up        # create and provision (~2–4 min, downloads Samba)
#   infra/auth/samba-ad-test.sh ca        # copy the domain's CA certificate to infra/auth/acme-ca.pem
#   infra/auth/samba-ad-test.sh down      # remove the container
#
# Passwords below are for this throwaway test domain only.
set -euo pipefail
NAME=gaia-ad
ADMIN_PW='Adm1n-Passw0rd!'
USER_PW='Test-Passw0rd-1'
SVC_PW='Svc-Gaia-Passw0rd-1'
BASE='DC=acme,DC=test'

t() { docker exec "$NAME" samba-tool "$@"; }

up() {
  docker run -d --name "$NAME" --hostname dc1 --privileged \
    -p 127.0.0.1:6360:636 debian:12 sleep infinity
  docker exec "$NAME" bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive \
      apt-get install -y -qq samba winbind ldb-tools >/dev/null && rm -f /etc/samba/smb.conf && \
      samba-tool domain provision --realm=ACME.TEST --domain=ACME --server-role=dc \
        --dns-backend=SAMBA_INTERNAL --adminpass='$ADMIN_PW' >/dev/null && samba"
  sleep 5
  for ou in "OU=Service" "OU=People" "OU=Finance,OU=People" "OU=HR,OU=People" \
            "OU=IT,OU=People" "OU=Groups" "OU=Gaia,OU=Groups" "OU=Teams,OU=Groups"; do
    t ou add "$ou,$BASE" 2>/dev/null || t ou create "$ou,$BASE"
  done
  t user create svc-gaia "$SVC_PW" --userou="OU=Service" --description="Gaia read-only"
  t user create akaya   "$USER_PW" --userou="OU=Finance,OU=People" --given-name=Ayse \
      --surname=Kaya --mail-address=akaya@acme.test --department=Finance
  t user create bdemir  "$USER_PW" --userou="OU=HR,OU=People" --given-name=Berk \
      --surname=Demir --mail-address=bdemir@acme.test --department=HR
  t user create cpasif  "$USER_PW" --userou="OU=HR,OU=People" --given-name=Cem \
      --surname=Pasif --department=HR
  t user create dyilmaz "$USER_PW" --userou="OU=IT,OU=People" --given-name=Deniz \
      --surname=Yilmaz --mail-address=dyilmaz@acme.test --department=IT
  t user create enorole "$USER_PW" --userou="OU=IT,OU=People" --given-name=Ece \
      --surname=Norole --department=IT
  t user disable cpasif
  t group add GAIA-Finance --groupou="OU=Gaia,OU=Groups" --description="Finance analysts"
  t group add GAIA-HR      --groupou="OU=Gaia,OU=Groups" --description="HR"
  t group add GAIA-Admins  --groupou="OU=Gaia,OU=Groups" --description="Gaia administrators"
  t group add Team-HR      --groupou="OU=Teams,OU=Groups"
  t group addmembers GAIA-Finance akaya,cpasif,dyilmaz
  t group addmembers Team-HR bdemir
  t group addmembers GAIA-HR Team-HR
  t group addmembers GAIA-Admins dyilmaz
  echo "ready: ldaps://dc1.acme.test:6360 (add '127.0.0.1 dc1.acme.test' to /etc/hosts), \
service account CN=svc-gaia,OU=Service,$BASE; users' password $USER_PW"
}

ca() {
  docker cp "$NAME":/var/lib/samba/private/tls/ca.pem "$(dirname "$0")/acme-ca.pem"
  echo "wrote $(dirname "$0")/acme-ca.pem — the ca_file of infra/auth/acme-samba.json"
}

down() { docker rm -f "$NAME"; }

case "${1:-}" in
  up) up ;;
  ca) ca ;;
  down) down ;;
  *) sed -n '2,28p' "$0"; exit 1 ;;
esac
