#!/usr/bin/env bash
# Drive the NAI gateway with N concurrent chat completions carrying V distinct
# X-RATELIMIT header values.
#
#   ./run.sh 100              # one wave: 100 concurrent requests, 100 distinct values
#   ./run.sh 5000             # one wave: 5000 concurrent requests, 5000 distinct values
#   ./run.sh 50 1             # 50 concurrent requests sharing 1 value -> expect 429s
#   ./run.sh 500 500 10m      # hold 500 in flight for 10 minutes, cycling 500 values
#   ./run.sh 500 500 1h       # same, for an hour
#
# Without a duration the run sends exactly <concurrency> requests, so it finishes
# in roughly one request latency. Pass a duration (Ns, Nm, Nh, or bare seconds) to
# keep the concurrency saturated for that long instead.
#
# Credentials and target come from .env.local (NAI_TARGET, NAI_MODEL, NAI_API_KEY).
#
# A single worker process is used by default. guidellm feeds its worker processes
# from a shared queue, so with several processes the per-process request counts are
# uneven and a bounded pool of values can hand out a repeat. One process owns the
# whole counter, so the pool is consumed exactly once: ratelimit-0 .. ratelimit-V-1.
#
# One process is also a throughput ceiling: a single asyncio loop doing TLS, JSON and
# tokenization saturates one core at roughly 60 req/s, which shows up as inflated
# client latency and connect timeouts well before the gateway is under strain. Set
# WORKERS=5 for duration runs, where values cycle and per-request uniqueness is moot
# anyway. Do not raise it for request-count runs that need each value used once.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

CONCURRENCY="${1:-100}"
VALUES="${2:-$CONCURRENCY}"
DURATION="${3:-}"
PREFIX="${RATELIMIT_PREFIX:-ratelimit-}"
WORKERS="${WORKERS:-1}"

# Every rendered header value is logged at DEBUG so a run can be audited, which
# costs about 1KB per request. Off by default for duration runs, where hundreds of
# thousands of requests would otherwise fill the disk. Set HEADER_AUDIT=1 to force.
HEADER_AUDIT="${HEADER_AUDIT:-$([[ -n "$DURATION" ]] && echo 0 || echo 1)}"

to_seconds() {
    local raw="$1" num unit
    num="${raw%[smhSMH]}"
    unit="${raw#"$num"}"
    if [[ ! "$num" =~ ^[0-9]+$ ]] || (( num == 0 )); then
        echo "bad duration '$raw'; use 30s, 10m, 1h or a number of seconds" >&2
        exit 1
    fi
    case "$unit" in
        "" | s | S) echo "$num" ;;
        m | M) echo $((num * 60)) ;;
        h | H) echo $((num * 3600)) ;;
        *) echo "bad duration unit in '$raw'; use s, m or h" >&2; exit 1 ;;
    esac
}

if [[ -n "$DURATION" ]]; then
    SECONDS_LIMIT="$(to_seconds "$DURATION")"
    CONSTRAINT="kind=max_duration,seconds=${SECONDS_LIMIT}"
    # Cycle the dataset, otherwise the run stops when prompts.jsonl is exhausted
    # instead of when the time limit is reached.
    DATA_LOADER="kind=pytorch,cycle=true"
    RUN_DIR="runs/$(date +%Y%m%d-%H%M%S)-c${CONCURRENCY}-v${VALUES}-${DURATION}"
else
    CONSTRAINT="kind=max_requests,count=${CONCURRENCY}"
    DATA_LOADER="kind=pytorch"
    RUN_DIR="runs/$(date +%Y%m%d-%H%M%S)-n${CONCURRENCY}-v${VALUES}"
    if (( CONCURRENCY > 5000 )); then
        echo "prompts.jsonl holds 5000 rows; regenerate it for $CONCURRENCY requests" >&2
        exit 1
    fi
fi

if [[ ! -f .env.local ]]; then
    echo "missing .env.local (see README.md)" >&2
    exit 1
fi

# Every in-flight request holds a socket, and macOS starts processes at a soft limit
# of 256 descriptors ("launchctl limit maxfiles"), which the worker processes inherit.
# Past that, sockets cannot be opened at all and httpx reports
# ConnectError('All connection attempts failed') wrapping OSError(24, 'Too many open
# files') -- which reads like a gateway failure but never leaves the laptop. The limit
# is per process and the pool is shared, so concurrency plus slack is ample headroom.
FD_CEILING="$(sysctl -n kern.maxfilesperproc 2>/dev/null || echo 61440)"
FD_TARGET=$((CONCURRENCY + 4096))
if (( FD_TARGET > FD_CEILING )); then
    FD_TARGET="$FD_CEILING"
fi
FD_CURRENT="$(ulimit -S -n)"
if [[ "$FD_CURRENT" != unlimited ]] && (( FD_CURRENT < FD_TARGET )); then
    # -S so a bare "ulimit -n" does not also clamp the hard limit down to the target.
    if ! ulimit -S -n "$FD_TARGET" 2>/dev/null; then
        echo "warning: could not raise the descriptor limit past $FD_CURRENT; expect" \
             "EMFILE-driven connect failures above roughly that many in flight" >&2
    fi
fi
set -a
# shellcheck disable=SC1091
. ./.env.local
set +a

mkdir -p "$RUN_DIR"
LOG="$RUN_DIR/guidellm-debug.log"

# fork() after torch is imported crashes the workers on macOS.
export GUIDELLM__MP_CONTEXT_TYPE=spawn
export GUIDELLM__MAX_WORKER_PROCESSES="$WORKERS"
export GUIDELLM__LOGGING__LOG_FILE="$LOG"
if [[ "$HEADER_AUDIT" == 1 ]]; then
    export GUIDELLM__LOGGING__LOG_FILE_LEVEL=DEBUG
else
    export GUIDELLM__LOGGING__LOG_FILE_LEVEL=INFO
fi

BACKEND="kind=openai_http"
BACKEND+=",target=${NAI_TARGET}"
BACKEND+=",model=${NAI_MODEL}"
BACKEND+=",api_key=${NAI_API_KEY}"
BACKEND+=",stream=false"
BACKEND+=",http2=false"
BACKEND+=",verify=false"
BACKEND+=",validate_backend=false"
BACKEND+=",text_content_as_string=true"
BACKEND+=",unique_headers.X-RATELIMIT=${PREFIX}{index}"
BACKEND+=",unique_headers_count=${VALUES}"
BACKEND+=",unique_headers_workers=${WORKERS}"

if [[ -n "$DURATION" ]]; then
    echo "==> $CONCURRENCY in flight for $DURATION, cycling $VALUES unique X-RATELIMIT values, $WORKERS worker process(es)"
else
    echo "==> $CONCURRENCY concurrent requests, $VALUES unique X-RATELIMIT values"
fi
echo "==> results in $RUN_DIR"
echo "==> descriptor limit $(ulimit -n) per worker process"

../.venv/bin/guidellm run \
    --backend "$BACKEND" \
    --profile "kind=concurrent,streams=${CONCURRENCY}" \
    --constraint "$CONSTRAINT" \
    --data "kind=json_file,path=prompts.jsonl,load_kwargs.split=train" \
    --data-loader "$DATA_LOADER" \
    --output "kind=console" \
    --output "kind=json,path=$RUN_DIR/benchmarks.json" \
    --disable-console-interactive \
    2>&1 | tee "$RUN_DIR/console.txt"

if [[ "$HEADER_AUDIT" == 1 ]]; then
    ./verify_headers.py "$LOG" "$VALUES" | tee "$RUN_DIR/header-audit.txt"
else
    echo "==> header audit skipped (HEADER_AUDIT=1 to enable)"
fi
