# Create the lab CA and the server certificate with the OpenSSL command-line tool.
#   powershell -ExecutionPolicy Bypass -File tools\make_certs.ps1 -ServerIP 192.168.56.1
# If OpenSSL is not installed, use the equivalent: python tools\make_certs.py --ip 192.168.56.1
param(
    [string]$ServerIP = "192.168.56.1",
    [string]$Out = "certs"
)
$ErrorActionPreference = "Stop"
if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
    Write-Error "openssl not found on PATH. Install it (e.g. with Git for Windows) or run: python tools\make_certs.py --ip $ServerIP"
}
New-Item -ItemType Directory -Force $Out | Out-Null

$ext = @"
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:$ServerIP, IP:127.0.0.1, DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"@
Set-Content -Path "$Out\server.ext" -Value $ext -Encoding ascii

# 1. Lab CA (10 years)
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 `
    -keyout "$Out\ca.key" -out "$Out\ca.crt" -subj "/O=ZephyQ Lab/CN=ZephyQ Lab CA" `
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
# 2. Server key + certificate signing request
openssl req -newkey rsa:2048 -nodes -sha256 -keyout "$Out\server.key" -out "$Out\server.csr" `
    -subj "/O=ZephyQ Lab/CN=ZephyQ server"
# 3. Sign the server certificate with the CA (825 days)
openssl x509 -req -in "$Out\server.csr" -CA "$Out\ca.crt" -CAkey "$Out\ca.key" -CAcreateserial `
    -days 825 -sha256 -extfile "$Out\server.ext" -out "$Out\server.crt"
# 4. Show what was issued
openssl x509 -in "$Out\server.crt" -noout -subject -issuer -dates -ext subjectAltName
Remove-Item "$Out\server.csr", "$Out\server.ext"
Write-Host "Done. Copy ONLY $Out\ca.crt to the student VMs; keep ca.key and server.key on the host."
