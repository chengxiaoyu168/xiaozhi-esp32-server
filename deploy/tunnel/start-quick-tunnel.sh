#!/bin/sh
# Free cloudflared quick tunnel to local Caddy. Does not fix box DNS or edge TLS.
# Does not set HTTP(S)_PROXY, ALL_PROXY, or a resolver. Does not print the OTA secret.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
# shellcheck source=quick-tunnel-lib.sh
. "$ROOT/deploy/tunnel/quick-tunnel-lib.sh"

OUT="$ROOT/main/xiaozhi-server/data/.public-wss"
UNVERIFIED="$ROOT/main/xiaozhi-server/data/.public-wss.unverified"
mkdir -p "$(dirname "$OUT")"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Use the official free binary." >&2
  exit 1
fi

set -- tunnel --url http://127.0.0.1:18766 --no-autoupdate --loglevel "$QUICK_TUNNEL_LOGLEVEL"
# shellcheck disable=SC2046
extra=$(cloudflared_protocol_arg) || exit $?
if [ -n "$extra" ]; then
  # shellcheck disable=SC2086
  set -- "$@" $extra
fi

echo "Starting quick tunnel to http://127.0.0.1:18766 at log level ${QUICK_TUNNEL_LOGLEVEL}." >&2
echo "A hostname in the log is not published until cloudflared logs Registered tunnel connection." >&2
echo "This script does not change protocol, DNS, or proxy settings unless CLOUDFLARED_PROTOCOL is already set." >&2

state=$(mktemp -d)
trap 'rm -rf "$state"' EXIT INT TERM

set +e
cloudflared "$@" 2>&1 | consume_cloudflared_log "$state" "$OUT" "$UNVERIFIED"
pipe_status=$?
set -e

if [ -f "$state/published" ]; then
  echo "Wrote data/.public-wss after a registered tunnel connection." >&2
  exit "$pipe_status"
fi

if [ -s "$UNVERIFIED" ] && [ -s "$state/host" ]; then
  echo "Hostname was logged but there was no Registered tunnel connection." >&2
  echo "Left data/.public-wss.unverified. OTA does not read that file." >&2
else
  echo "No quick-tunnel hostname was observed." >&2
  echo "cloudflared logs that URL at info. This script keeps that level. An error log level hides the hostname." >&2
fi
echo "Not a network fix. Observed box failures (DNS 198.18.0.1, QUIC timeout, http2 TLS EOF, HTTP 530) are outside this script." >&2
echo "CLOUDFLARED_PROTOCOL is not applied unless you set it. Setting it does not bypass a blocked Cloudflare path." >&2
exit 1
