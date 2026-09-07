#!/usr/bin/env bash
# NOT FOR PRODUCTION: generate ephemeral lab-only certificate material.
set -euo pipefail

target=${1:?certificate directory required}
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
openssl_config="$script_dir/openssl.cnf"
mkdir -p "$target"
chmod 700 "$target"
umask 077

openssl genrsa -out "$target/ca.key" 2048 >/dev/null 2>&1
openssl req -x509 -new -nodes -key "$target/ca.key" -sha256 -days 2 \
    -subj "/CN=DomoAI Lab v2 CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -config "$openssl_config" -extensions v3_ca \
    -out "$target/ca.pem" >/dev/null 2>&1

make_cert() {
    local name=$1 subject=$2 serial=$3
    local -a request_extensions=()
    if [[ "$name" == server ]]; then
        request_extensions=(-addext "subjectAltName=DNS:domoai-lab-v2-server")
    fi
    openssl genrsa -out "$target/$name.key" 2048 >/dev/null 2>&1
    openssl req -new -key "$target/$name.key" -subj "$subject" \
        "${request_extensions[@]}" -out "$target/$name.csr" >/dev/null 2>&1
    openssl x509 -req -in "$target/$name.csr" -CA "$target/ca.pem" -CAkey "$target/ca.key" \
        -set_serial "$serial" -days 1 -sha256 -copy_extensions copy \
        -out "$target/$name.pem" >/dev/null 2>&1
    rm -f -- "$target/$name.csr"
}

make_cert server "/CN=domoai-lab-v2-server" 11
make_cert client "/CN=domoai-lab-v2-client" 12
make_cert rotated-client "/CN=domoai-lab-v2-client-rotated" 13

openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
    -subj "/CN=domoai-lab-v2-untrusted" \
    -keyout "$target/untrusted-client.key" -out "$target/untrusted-client.pem" >/dev/null 2>&1

# Issue one genuinely expired credential so the probe exercises certificate
# validity enforcement, rather than merely checking that a file exists.
openssl genrsa -out "$target/expired-client.key" 2048 >/dev/null 2>&1
openssl req -new -key "$target/expired-client.key" \
    -subj "/CN=domoai-lab-v2-expired-client" \
    -out "$target/expired-client.csr" >/dev/null 2>&1
touch "$target/ca-index.txt"
printf '01\n' >"$target/ca-serial"
mkdir -p "$target/ca-newcerts"
openssl ca -batch -config <(
    printf '%s\n' \
        '[ca]' \
        'default_ca=ca_default' \
        '[ca_default]' \
        "database=$target/ca-index.txt" \
        "new_certs_dir=$target/ca-newcerts" \
        "serial=$target/ca-serial" \
        'default_md=sha256' \
        'policy=policy' \
        '[policy]' \
        'commonName=supplied'
) -cert "$target/ca.pem" -keyfile "$target/ca.key" \
    -in "$target/expired-client.csr" -startdate 20000101000000Z -enddate 20010101000000Z \
    -out "$target/expired-client.pem" >/dev/null 2>&1

rm -f -- "$target/ca.key" "$target"/*.csr "$target/ca-index.txt" "$target/ca-serial"
rm -rf -- "$target/ca-newcerts"
chmod 600 "$target"/*.key "$target"/*.pem
