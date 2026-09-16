$ErrorActionPreference = 'Stop'
# No matching Ingress should exist for this diagnostic hostname.
$status = curl.exe --noproxy '*' --max-time 5 -sS -o NUL -w '%{http_code}' -H 'Host: previewmesh-ingress-check.invalid' http://127.0.0.1
if ($LASTEXITCODE -ne 0 -or $status -ne '404') {
    throw "Expected HTTP 404 through Windows localhost; got $status"
}
Write-Output 'PASS: Windows localhost returned expected HTTP 404.'
