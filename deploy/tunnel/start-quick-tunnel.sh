#!/bin/sh
# Free temporary Cloudflare quick tunnel to the local Caddy listener.
# Does not print the OTA bootstrap credential. Does not open Docker.
# The trycloudflare hostname changes every start; OTA reads data/.public-wss live.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
OUT="$ROOT/main/xiaozhi-server/data/.public-wss"
mkdir -p "$(dirname "$OUT")"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Use the official free binary; this script does not buy a tunnel." >&2
  exit 1
fi

echo "Starting quick tunnel to http://127.0.0.1:18766. Leave this process running." >&2

cloudflared tunnel --url http://127.0.0.1:18766 --no-autoupdate --loglevel error 2>&1 |
  while IFS= read -r line; do
    case "$line" in
      *"/xiaozhi/ota/"*)
        echo "cloudflared line redacted" >&2
        ;;
      *)
        printf '%s\n' "$line" >&2
        ;;
    esac
    host=$(printf '%s\n' "$line" | sed -n 's/.*https:\/\/\([A-Za-z0-9.-]*trycloudflare.com\).*/\1/p')
    if [ -n "$host" ]; then
      tmp="$OUT.tmp"
      printf 'wss://%s/xiaozhi/v1/\n' "$host" > "$tmp"
      chmod 600 "$tmp"
      mv "$tmp" "$OUT"
      echo "Updated data/.public-wss. Set the device OTA URL on the device to https://$host/xiaozhi/ota/<contents of data/.ota-bootstrap>/" >&2
    fi
  done
