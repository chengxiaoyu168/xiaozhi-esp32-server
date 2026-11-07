# Shared quick-tunnel helpers. Sourced by the starter and by tests.
# No network calls. Does not set proxy variables or change DNS.

# cloudflared LogTable logs the quick-tunnel URL with zerolog Info
# (cmd/cloudflared/cliutil/logger.go). --loglevel error drops Info,
# so a script that only reads error lines never sees the hostname.
QUICK_TUNNEL_LOGLEVEL=info

# Print the hostname from one cloudflared log line, or nothing.
# Requires https://<label>.trycloudflare.com. The bare word
# trycloudflare.com in "Requesting new quick Tunnel" is not a hostname.
# Lines that contain the OTA path are ignored so a request log cannot
# publish a credential.
extract_quick_tunnel_host() {
  line=$1
  case "$line" in
    *"/xiaozhi/ota/"*)
      return 0
      ;;
  esac
  printf '%s\n' "$line" | sed -n \
    's/.*https:\/\/\([A-Za-z0-9-]*\.trycloudflare\.com\).*/\1/p' | head -n 1
}

# cloudflared prints this at Info after the edge accepts the connection.
# A hostname line alone is not registration.
line_is_registered_tunnel() {
  printf '%s\n' "$1" | grep -q 'Registered tunnel connection'
}

# Append --protocol only when the operator set CLOUDFLARED_PROTOCOL.
# Allowed values are cloudflared's own: auto, quic, http2.
# Empty means omit the flag. This function does not change transport by itself.
cloudflared_protocol_arg() {
  proto=${CLOUDFLARED_PROTOCOL:-}
  if [ -z "$proto" ]; then
    return 0
  fi
  case "$proto" in
    auto|quic|http2)
      printf '%s\n' "--protocol" "$proto"
      ;;
    *)
      echo "CLOUDFLARED_PROTOCOL must be auto, quic, or http2." >&2
      return 2
      ;;
  esac
}

# Read cloudflared stdout/stderr from stdin.
# $1 state directory, $2 live wss file, $3 unverified wss file.
# Live file is written only after a hostname and "Registered tunnel connection".
# Hostname without registration is written to the unverified file when the
# stream ends, and the live file is left unchanged.
# Echoes redacted lines to stderr. Does not print a bootstrap secret.
consume_cloudflared_log() {
  state=$1
  live=$2
  unverified=$3
  mkdir -p "$state"
  while IFS= read -r line; do
    case "$line" in
      *"/xiaozhi/ota/"*)
        echo "cloudflared line redacted" >&2
        continue
        ;;
      *)
        printf '%s\n' "$line" >&2
        ;;
    esac
    host=$(extract_quick_tunnel_host "$line")
    if [ -n "$host" ]; then
      printf '%s\n' "$host" > "$state/host"
    fi
    if line_is_registered_tunnel "$line"; then
      : > "$state/registered"
    fi
    if [ -s "$state/host" ] && [ -f "$state/registered" ]; then
      host=$(head -n 1 "$state/host")
      tmp="$live.tmp"
      printf 'wss://%s/xiaozhi/v1/\n' "$host" > "$tmp"
      chmod 600 "$tmp"
      mv "$tmp" "$live"
      rm -f "$unverified"
      : > "$state/published"
    fi
  done
  if [ -s "$state/host" ] && [ ! -f "$state/registered" ]; then
    host=$(head -n 1 "$state/host")
    tmp="$unverified.tmp"
    printf 'wss://%s/xiaozhi/v1/\n' "$host" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$unverified"
  fi
}
