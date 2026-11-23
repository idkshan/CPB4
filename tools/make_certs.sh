#!/bin/sh
# Makes the lab CA and server certificate with OpenSSL (usage: sh tools/make_certs.sh 192.168.56.1).
set -e
IP="${1:-192.168.56.1}"
OUT="${2:-certs}"
mkdir -p "$OUT"
cat > "$OUT/server.ext" <<EOF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:$IP, IP:127.0.0.1, DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
    -keyout "$OUT/ca.key" -out "$OUT/ca.crt" -subj "/O=ZephyQ Lab/CN=ZephyQ Lab CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
openssl req -newkey rsa:2048 -nodes -sha256 -keyout "$OUT/server.key" -out "$OUT/server.csr" \
    -subj "/O=ZephyQ Lab/CN=ZephyQ server"
openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
    -days 825 -sha256 -extfile "$OUT/server.ext" -out "$OUT/server.crt"
openssl x509 -in "$OUT/server.crt" -noout -subject -issuer -dates -ext subjectAltName
rm -f "$OUT/server.csr" "$OUT/server.ext"
chmod 600 "$OUT/ca.key" "$OUT/server.key"
echo "Done. Copy ONLY $OUT/ca.crt to the student VMs."
