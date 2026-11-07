#!/bin/sh
# Parser and failure handling only. No cloudflared, no network, no real hostname.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
# shellcheck source=quick-tunnel-lib.sh
. "$ROOT/deploy/tunnel/quick-tunnel-lib.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

host=$(extract_quick_tunnel_host "INF https://fixture-name.trycloudflare.com")
[ "$host" = "fixture-name.trycloudflare.com" ] || fail "info line did not yield fixture host"

host=$(extract_quick_tunnel_host "INF Requesting new quick Tunnel on trycloudflare.com...")
[ -z "$host" ] || fail "request line was treated as a hostname"

host=$(extract_quick_tunnel_host "ERR failed to dial quic: timeout")
[ -z "$host" ] || fail "error line without a URL yielded a host"

host=$(extract_quick_tunnel_host "INF https://fixture-name.trycloudflare.com/xiaozhi/ota/local-credential/")
[ -z "$host" ] || fail "OTA path line was parsed"
redacted=$(printf '%s\n' "INF https://fixture-name.trycloudflare.com/xiaozhi/ota/local-credential/" \
  | consume_cloudflared_log "$work/redact-state" "$work/redact-live" "$work/redact-unverified" 2>&1)
printf '%s\n' "$redacted" | grep -q 'cloudflared line redacted' || fail "OTA line was not redacted"
printf '%s\n' "$redacted" | grep -q 'local-credential' && fail "redacted line still contains the path credential"
[ ! -e "$work/redact-live" ] || fail "OTA line wrote the live endpoint"

line_is_registered_tunnel "INF Registered tunnel connection connIndex=0" || fail "registration line missed"
if line_is_registered_tunnel "INF https://fixture-name.trycloudflare.com"; then
  fail "hostname line counted as registration"
fi

# Hostname without registration must not create the live endpoint.
: > "$work/old-live"
printf '%s\n' "INF https://fixture-name.trycloudflare.com" \
  | consume_cloudflared_log "$work/unreg" "$work/old-live" "$work/unverified" >/dev/null 2>&1
[ ! -s "$work/old-live" ] || fail "unregistered run replaced the live file"
grep -q 'wss://fixture-name.trycloudflare.com/xiaozhi/v1/' "$work/unverified" \
  || fail "unregistered run did not quarantine"

# Registration after the hostname publishes the live file and clears quarantine.
printf '%s\n' \
  "INF https://fixture-name.trycloudflare.com" \
  "INF Registered tunnel connection connIndex=0 protocol=quic" \
  | consume_cloudflared_log "$work/ok" "$work/live" "$work/unverified" >/dev/null 2>&1
grep -q 'wss://fixture-name.trycloudflare.com/xiaozhi/v1/' "$work/live" \
  || fail "registered run did not write public-wss"
[ ! -e "$work/unverified" ] || fail "registered run left unverified file"

# Error-level sample has no URL and no registration.
printf '%s\n' "ERR Connection terminated error=\"tls: handshake failure\"" \
  | consume_cloudflared_log "$work/err" "$work/err-live" "$work/err-unverified" >/dev/null 2>&1
[ ! -e "$work/err-live" ] || fail "error log wrote public-wss"
[ ! -e "$work/err-unverified" ] || fail "error log wrote unverified"
[ ! -s "$work/err/host" ] || fail "error log stored a host"

# Protocol flag is opt-in and limited to cloudflared's names.
saved=${CLOUDFLARED_PROTOCOL-}
unset CLOUDFLARED_PROTOCOL
args=$(cloudflared_protocol_arg)
[ -z "$args" ] || fail "unset protocol added a flag"
CLOUDFLARED_PROTOCOL=http2
args=$(cloudflared_protocol_arg)
[ "$args" = "--protocol
http2" ] || fail "http2 flag mismatch"
CLOUDFLARED_PROTOCOL=h2mux
if cloudflared_protocol_arg >/dev/null 2>&1; then
  fail "rejected protocol was accepted"
fi
if [ -n "$saved" ]; then
  CLOUDFLARED_PROTOCOL=$saved
else
  unset CLOUDFLARED_PROTOCOL
fi

# Starter keeps info logs and does not inject proxy or DNS settings.
starter="$ROOT/deploy/tunnel/start-quick-tunnel.sh"
grep -q 'QUICK_TUNNEL_LOGLEVEL' "$starter" || fail "starter does not force info log level"
grep -q -- '--loglevel "$QUICK_TUNNEL_LOGLEVEL"' "$starter" || fail "starter log flag"
if grep -q -- '--loglevel error' "$starter"; then
  fail "starter still requests error logs"
fi
if grep -Eq '^(export )?(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|DNS)=' "$starter" "$ROOT/deploy/tunnel/quick-tunnel-lib.sh"; then
  fail "script assigns a proxy or resolver"
fi

caddy="$ROOT/deploy/caddy/Caddyfile"
grep -q 'bind 127.0.0.1' "$caddy" || fail "Caddyfile has no bind 127.0.0.1"
if grep -q '0.0.0.0' "$caddy"; then
  fail "Caddyfile binds all interfaces"
fi

echo "quick tunnel parser tests passed"
