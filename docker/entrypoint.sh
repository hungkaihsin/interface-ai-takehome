#!/usr/bin/env bash
# Starts the target app, waits for it to answer, then runs the requested command.
#
# The wait is not cosmetic: every demo begins by driving a UI that has to exist
# first, and a fixed sleep would be a guess about the host machine -- the same
# reason the replay engine has no sleeps in it.
set -euo pipefail

start_target_app() {
  # --host 0.0.0.0 so the console is reachable from your browser on the host.
  # The automation inside the container still reaches it on 127.0.0.1, which is
  # what the committed artifacts' origin allowlist permits.
  python -m flask --app target_app.app run --host 0.0.0.0 --port 5001 \
    >/tmp/target_app.log 2>&1 &

  python - <<'PY'
import sys, time, urllib.error, urllib.request

deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        urllib.request.urlopen("http://127.0.0.1:5001/login", timeout=1)
        print("  target app ready on :5001")
        sys.exit(0)
    except (urllib.error.URLError, OSError):
        time.sleep(0.3)
print("  target app did not start; see /tmp/target_app.log", file=sys.stderr)
sys.exit(1)
PY
}

command="${1:-demo}"
shift || true

case "$command" in
  demo)
    start_target_app
    echo
    echo "=================== REPLAY (no model, no API key) ==================="
    env -u GOOGLE_API_KEY python -m scripts.demo_replay --all
    echo
    echo "=================== ESCALATION (human takeover) ====================="
    python -m scripts.demo_escalation
    echo
    echo "=================== TESTS ==========================================="
    python -m pytest tests/ -q
    echo
    echo "Discovery needs a key and is not part of the default run:"
    echo "  docker compose run --rm -e GOOGLE_API_KEY=... app discovery"
    ;;

  replay)
    start_target_app
    env -u GOOGLE_API_KEY python -m scripts.demo_replay "${@:---all}"
    ;;

  escalation)
    start_target_app
    python -m scripts.demo_escalation "$@"
    ;;

  discovery)
    start_target_app
    if [ -z "${GOOGLE_API_KEY:-}" ]; then
      echo "GOOGLE_API_KEY is not set. Discovery needs a model; replay does not." >&2
      exit 2
    fi
    python -m scripts.run_discovery \
      --goal "Look up member 10001 and read their current savings balance" \
      --param member_id=10001 \
      --capability member_savings_lookup_discovered \
      --verify-with member_id=10003 "$@"
    ;;

  test)
    python -m pytest tests/ -q "$@"
    ;;

  serve)
    # Just the legacy app, in the foreground, for poking at by hand.
    echo "MeridianCU back-office on http://localhost:5001  (operator / letmein)"
    exec python -m flask --app target_app.app run --host 0.0.0.0 --port 5001
    ;;

  shell)
    start_target_app
    exec /bin/bash
    ;;

  *)
    # Anything else runs verbatim, with the target app already up.
    start_target_app
    exec "$command" "$@"
    ;;
esac
