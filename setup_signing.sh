#!/bin/bash
# One-time, optional: creates a local code-signing certificate so macOS keeps
# its "this app may read your Desktop / external drives" permission across
# rebuilds. build_macos.sh finds the certificate by name and signs with it
# automatically; without it the build still works (ad-hoc signature with a
# pinned requirement), this is just the sturdier option.
#
# What it does, in plain terms:
#   1. makes a self-signed certificate called "Notorious BPM Local Signing"
#      (valid 10 years, usable for code signing only),
#   2. imports it into your login keychain,
#   3. marks it trusted for code signing -- macOS will ask for your login
#      password / Touch ID for this step, which is why this is a script you
#      run yourself rather than something the build does silently.
# Nothing leaves your Mac. To undo: open Keychain Access, delete the
# "Notorious BPM Local Signing" certificate and its private key.
#
# Usage:  ./setup_signing.sh
set -euo pipefail

NAME="Notorious BPM Local Signing"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -p codesigning 2>/dev/null | grep -q "$NAME"; then
  echo "\"$NAME\" is already set up. Nothing to do."
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS="notorious-bpm-import"

cat > "$WORK/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF

echo "== Creating certificate =="
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" -config "$WORK/openssl.cnf"
openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
  -name "$NAME" -out "$WORK/identity.p12" -passout "pass:$PASS"

echo "== Importing into your login keychain =="
security import "$WORK/identity.p12" -k "$KEYCHAIN" -P "$PASS" -T /usr/bin/codesign

echo "== Trusting it for code signing (macOS will ask for your password) =="
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$WORK/cert.pem"

echo
security find-identity -p codesigning | grep "$NAME" || true
echo "Done. Run ./build_macos.sh -- it will sign with this certificate from now on."
echo "After the first build you need to allow Desktop/drive access once more; after that it sticks."
