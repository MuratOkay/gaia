#!/usr/bin/env bash
# A throwaway Active Directory for verifying Phase 5 sign-in (OWNER_VERIFICATION §22e, §26;
# P5-10).
#
# Samba 4 as an AD domain controller in a Debian container, with the same people and
# groups as the offline test directory (tests/unit/_fake_directory.py), so the host result
# can be read against the sandbox's:
#
#   ACME.TEST  (dc=acme,dc=test), LDAPS on 127.0.0.1:6360
#   OU=Service     svc-gaia (Gaia's read-only service account)
#   OU=People/Finance  akaya  — GAIA-Finance (department Finance)
#                      finn   — GAIA-Finance: the person who only chats (§25, §26)
#   OU=People/HR       bdemir — Team-HR, which is a member of GAIA-HR (nested)
#                      cpasif — disabled, GAIA-Finance
#   OU=People/IT       dyilmaz — GAIA-Admins and GAIA-Finance
#                      enorole — in no Gaia group (must not get in)
#   OU=Groups/Gaia     GAIA-Finance, GAIA-HR, GAIA-Admins      OU=Groups/Teams  Team-HR
#
#   infra/auth/samba-ad-test.sh up       # create and provision (~2–4 min), then write the CA
#   infra/auth/samba-ad-test.sh start    # after a reboot or a Docker restart: Samba again
#   infra/auth/samba-ad-test.sh check    # does infra/auth/acme-ca.pem match the domain's CA?
#   infra/auth/samba-ad-test.sh ca       # copy the domain's CA to infra/auth/acme-ca.pem
#   infra/auth/samba-ad-test.sh status
#   infra/auth/samba-ad-test.sh down     # remove the container
#
# Every `up` provisions a NEW domain, and Samba makes a NEW self-signed CA for it under the
# SAME name (O=Samba Administration, CN=DC1.acme.test). A CA file kept from an earlier domain
# then fails with "certificate verify failed: certificate signature failure" — what the owner
# met on build D75 (D76). `up` therefore writes the CA file itself, and `check` compares.
# Samba is not the container's main process: after a reboot the container comes back
# without it — `start` starts it again (the domain and its CA are kept).
#
# Passwords below are for this throwaway test domain only.
set -euo pipefail
NAME=gaia-ad
DIR="$(cd "$(dirname "$0")" && pwd)"
CA_FILE="$DIR/acme-ca.pem"
ADMIN_PW='Adm1n-Passw0rd!'
USER_PW='Test-Passw0rd-1'
SVC_PW='Svc-Gaia-Passw0rd-1'
BASE='DC=acme,DC=test'

t() { docker exec "$NAME" samba-tool "$@"; }
exists() { docker container inspect "$NAME" >/dev/null 2>&1; }
running() { [ "$(docker container inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = true ]; }
samba_running() { docker exec "$NAME" sh -c 'grep -qx samba /proc/[0-9]*/comm 2>/dev/null'; }

wait_ldaps() {
  for _ in $(seq 1 60); do
    if docker exec "$NAME" bash -c 'exec 3<>/dev/tcp/127.0.0.1/636' 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "LDAPS (636) did not open within 2 minutes: docker logs $NAME" >&2
  return 1
}

fingerprint() {  # the SHA-256 of a PEM certificate, when openssl is at hand
  if command -v openssl >/dev/null; then
    openssl x509 -in "$1" -noout -fingerprint -sha256 | sed 's/^.*=//'
  else
    sha256sum "$1" | cut -d' ' -f1
  fi
}

up() {
  if exists; then
    echo "$NAME exists already. After a reboot: '$0 start'. For a new domain: '$0 down'," \
         "then '$0 up' (it writes the new CA to $CA_FILE)." >&2
    exit 1
  fi
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
  t user create finn    "$USER_PW" --userou="OU=Finance,OU=People" --given-name=Finn \
      --surname=Aksoy --mail-address=finn@acme.test --department=Finance
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
  t group addmembers GAIA-Finance akaya,finn,cpasif,dyilmaz
  t group addmembers Team-HR bdemir
  t group addmembers GAIA-HR Team-HR
  t group addmembers GAIA-Admins dyilmaz
  wait_ldaps
  ca
  echo "ready: ldaps://dc1.acme.test:6360 (add '127.0.0.1 dc1.acme.test' to /etc/hosts), \
service account CN=svc-gaia,OU=Service,$BASE; users' password $USER_PW"
  echo "a new domain: if Gaia knew an earlier one, test it again — make auth ARGS=\"directory probe acme\""
}

start() {
  if ! exists; then
    echo "no container $NAME: '$0 up' creates the domain" >&2
    exit 1
  fi
  running || docker start "$NAME" >/dev/null
  if samba_running; then
    echo "Samba is running"
  else
    docker exec "$NAME" samba
    echo "Samba started"
  fi
  wait_ldaps
  check || true
}

ca() {
  docker cp "$NAME":/var/lib/samba/private/tls/ca.pem "$CA_FILE"
  echo "wrote $CA_FILE (SHA-256 $(fingerprint "$CA_FILE")) — the ca_file of" \
       "infra/auth/acme-samba.json; Gaia reads it at every connection"
}

check() {
  if [ ! -f "$CA_FILE" ]; then
    echo "no $CA_FILE: '$0 ca' writes it" >&2
    return 1
  fi
  local live
  live="$(mktemp)"
  docker exec "$NAME" cat /var/lib/samba/private/tls/ca.pem > "$live"
  if cmp -s "$live" "$CA_FILE"; then
    echo "CA file matches the domain's CA (SHA-256 $(fingerprint "$CA_FILE"))"
  else
    echo "CA file does NOT match the domain's CA: the domain was provisioned again." >&2
    echo "  file:   $(fingerprint "$CA_FILE")" >&2
    echo "  domain: $(fingerprint "$live")" >&2
    echo "  fix: '$0 ca', then make auth ARGS=\"directory probe acme\"" >&2
    rm -f "$live"
    return 1
  fi
  rm -f "$live"
  if command -v openssl >/dev/null; then
    local verdict
    verdict="$(openssl s_client -connect 127.0.0.1:6360 -servername dc1.acme.test \
                 -CAfile "$CA_FILE" </dev/null 2>/dev/null | grep -m1 'Verify return code' || true)"
    echo "LDAPS handshake with that CA: ${verdict:-no answer on 127.0.0.1:6360}"
  fi
}

status() {
  if ! exists; then echo "$NAME: no container"; return 0; fi
  echo "$NAME: container $(docker container inspect -f '{{.State.Status}}' "$NAME")"
  if running; then
    if samba_running; then echo "Samba: running"; else echo "Samba: NOT running — '$0 start'"; fi
    check || true
  fi
}

down() { docker rm -f "$NAME"; }

case "${1:-}" in
  up) up ;;
  start) start ;;
  ca) ca ;;
  check) check ;;
  status) status ;;
  down) down ;;
  *) sed -n '2,33p' "$0"; exit 1 ;;
esac
