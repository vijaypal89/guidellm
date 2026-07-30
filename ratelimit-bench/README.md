# Rate-limit load testing NAI with guidellm

Drive the NAI inference gateway with N concurrent chat completions where **every
request carries a different `X-RATELIMIT` header value**, so the gateway's global
rate limiter sees N distinct rate-limit keys instead of one.

The reference request this reproduces:

```bash
curl -k -X POST 'https://10.117.63.78/enterpriseai/gateway/v1/chat/completions' -i \
  -H "Authorization: Bearer $API_KEY" -H "X-RATELIMIT: ${UNIQUE_5000_VALUES}" \
  -H 'accept: application/json' -H 'Content-Type: application/json' \
  -d '{"model":"mock-uep","messages":[{"role":"user","content":"just say hi"}],"stream":false}'
```

Stock guidellm cannot do this: it has no way to vary a header per request, it
always encodes chat content as a list of content parts (which this endpoint
rejects), and it reports failures without their HTTP status code so a 429 is
indistinguishable from a 500. All three gaps are closed by the patch described
below.

## Contents

| Path | Purpose |
| --- | --- |
| `guidellm-unique-headers.patch` | The guidellm patch, for reapplying on a clean tree |
| `run.sh` | Runs the benchmark for a given request count or duration |
| `verify_headers.py` | Audits the header values a run actually sent |
| `socket-sample.sh` | Samples the client socket table during a run |
| `prompts.jsonl` | 5000 rows of `{"prompt": "just say hi"}` |
| `.env.local` | Target, model, and API key (git-ignored) |
| `runs/` | Per-run console output, JSON report, debug log, audit (git-ignored) |

## 1. Set up the virtualenv and build guidellm

From the repository root (`/Users/vijay.pal/github.com/vllm-project/guidellm`):

```bash
cd /Users/vijay.pal/github.com/vllm-project/guidellm
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
```

Apply the patch if the working tree is clean (skip if the changes are already
present — `git diff --stat` should show `common.py`, `http.py`, and
`request_handlers.py`):

```bash
git apply ratelimit-bench/guidellm-unique-headers.patch
```

Install in editable mode so edits to `src/` take effect without reinstalling:

```bash
.venv/bin/pip install -e .
.venv/bin/guidellm --version    # guidellm version: 0.7.2
```

## 2. Configure the target

Create `ratelimit-bench/.env.local` (git-ignored, keeps the token off the command
line):

```bash
NAI_TARGET=https://10.117.63.78/enterpriseai/gateway
NAI_MODEL=mock-uep
NAI_API_KEY=<your gateway api key>
```

`NAI_TARGET` is the base URL **without** `/v1`; guidellm appends
`/v1/chat/completions` itself.

Sanity check the endpoint before benchmarking:

```bash
cd ratelimit-bench && set -a && . ./.env.local && set +a
curl -sk -o /dev/null -w '%{http_code}\n' -X POST "$NAI_TARGET/v1/chat/completions" \
  -H "Authorization: Bearer $NAI_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"'"$NAI_MODEL"'","messages":[{"role":"user","content":"just say hi"}]}'
```

`200` means you are ready. `401` is a bad key; `404` with
`"No matching cassette found"` means the request body does not match what the
mock endpoint recorded (see [Troubleshooting](#troubleshooting)).

## 3. Prepare the prompt dataset

`prompts.jsonl` already holds 5000 identical prompts. Regenerate or resize it
with:

```bash
python3 -c "
import json; from pathlib import Path
row = json.dumps({'prompt': 'just say hi'})
Path('prompts.jsonl').write_text('\n'.join([row] * 5000) + '\n')
"
```

For request-count runs the file needs at least as many rows as the request count,
because guidellm stops generating requests once the dataset is exhausted.
Duration runs cycle the file instead, so 5000 rows are enough for any length.

## 4. Run

```bash
cd ratelimit-bench
./run.sh 100      # 100 concurrent requests, 100 distinct header values
./run.sh 5000     # 5000 concurrent requests, 5000 distinct header values
./run.sh 50 1     # 50 concurrent requests sharing 1 value -> drives 429s
```

The second argument is how many distinct values to spread over the requests. It
defaults to the request count (one value per request); a smaller number makes
requests share values, which is how you push a single rate-limit bucket over its
budget.

Each run writes to `runs/<timestamp>-n<count>/`:

- `console.txt` — the guidellm report
- `benchmarks.json` — machine-readable results
- `guidellm-debug.log` — DEBUG log including every rendered header value
- `header-audit.txt` — the uniqueness verdict

Override the value prefix with `RATELIMIT_PREFIX=tenant- ./run.sh 100`, which
sends `tenant-0 … tenant-99`.

### Sample run result (100 requests)

```
| concurrent | Comp 900.0 input tok | Inc 0.0 | Err 0.0 | Comp 1900.0 output tok |
| concurrent | Request Latency Mdn 3.0s p95 3.6s | Concurrency Mdn 100.0 | 26.4 req/s |

requests with rendered headers : 100
X-RATELIMIT: 100 sent, 100 distinct
  first: ratelimit-0   last: ratelimit-99
PASS
```

All 100 requests succeeded (9 input / 19 output tokens each, no errors) and the
gateway received `ratelimit-0` through `ratelimit-99`, each exactly once.

### Running for a fixed duration

With two arguments the run sends exactly one wave: `./run.sh 500` fires 500
requests, waits for them, and stops. That takes about as long as a single
request, so the measured window is only 8 seconds (~25 s of wall clock once
process startup and tokenizer loading are counted):

```
| concurrent | 02:05:59 | 02:06:07 | Dur 8.1 s | 4491 input tok | 9481 output tok |
| concurrent | Request Latency Mdn 3.9s p95 7.8s | Concurrency Mdn 236.0 | 61.5 req/s |
```

Pass a third argument to hold that concurrency for a set time instead. Accepts
`30s`, `10m`, `1h`, or a bare number of seconds:

```bash
./run.sh 500 500 10m    # 500 in flight for 10 minutes, cycling 500 values
./run.sh 500 500 1h     # same, for an hour
./run.sh 50 1 5m        # hammer one bucket for 5 minutes
```

In duration mode guidellm keeps `<concurrency>` requests in flight and replaces
each one as it completes until the time limit, so total requests are
`concurrency / latency * duration` rather than a fixed count. The value pool
wraps around: with 500 values the 501st request reuses `ratelimit-0`.

Two things change automatically in duration mode:

- The dataset is cycled (`--data-loader kind=pytorch,cycle=true`), otherwise the
  run would stop after 5000 requests instead of at the time limit.
- The per-request header DEBUG log is switched off, because it costs roughly 1 KB
  per request and a 1-hour run at a few hundred req/s would write tens of GB.
  Force it back on with `HEADER_AUDIT=1 ./run.sh 500 500 10m`, and keep an eye on
  the log size.

### Worker processes

One worker process does TLS, JSON and tokenization on a single asyncio loop and
saturates a core at a few hundred req/s, whatever the concurrency setting. Past
that the client, not the gateway, becomes the bottleneck and the run reports
inflated latency and connect timeouts. Spreading the load also multiplies the
descriptor budget, since the limit is per process. For concurrency above ~200:

```bash
WORKERS=5 ./run.sh 1000 1000 10m
```

`WORKERS` sets both `GUIDELLM__MAX_WORKER_PROCESSES` and the backend's
`unique_headers_workers`, so header indices stay strided and never collide across
processes. Keep it at 1 for request-count runs that must use each value exactly
once; in duration runs values cycle regardless, so there is nothing to lose.

Note that a long run against a bounded value pool spends most of its time being
rate limited. Each value gets 500 tokens per hour and a request costs ~28 tokens,
so a value is exhausted after ~17 requests; everything after that returns 429
until the hour rolls over. A 30-second run at 20 concurrency over 20 values
already ended with 5.2% 429s. To measure sustained success instead of throttling,
give the run far more values than it can exhaust, or expect the error summary to
dominate.

## 5. Reading failures out of the report

When any request fails, the console report ends with a breakdown by HTTP status
code, so you can tell rate limiting from a server error at a glance:

```
ℹ Request Error Summary (Errored Requests)
| Benchmark  | Status | Count | Pct Reqs | Example Message                                     |
| concurrent | 429    | 50    | 100.0    | OpenAIResponseError('HTTP 429 Too Many Requests      |
|            |        |       |          | [x-ratelimit-limit=500, 500;w=3600 …                 |
```

`Status` is `no response` for failures that never got one (connection refused,
timeouts). The table is skipped entirely when every request succeeds.

The same information is in `benchmarks.json` per request, with the status code as
a number so you can aggregate it yourself:

```bash
python3 -c "
import json, collections
b = json.load(open('runs/<run>/benchmarks.json'))['benchmarks'][0]
print(collections.Counter(r['info']['error_status'] for r in b['requests']['errored']))
print(b['requests']['errored'][0]['info']['error'])
"
```

### Triaging a large run without parsing the JSON

`json.load` is only practical for short runs. A 10-minute run at 5000 concurrency
writes a **1.5 GB** `benchmarks.json`, and it is all on **one line**, which has two
consequences: loading it costs several GB of RAM, and `rg -c` reports `1` no matter
how many matches there are. Use `rg -o` to count occurrences instead of lines.

Group every distinct error message with its count. This is the first command to run
on a failed run, and usually the only one needed:

```bash
cd runs/<run>
rg -o '"error": "[^"]*"' benchmarks.json | sort | uniq -c | sort -rn | head
```

`ConnectTimeout` and `WriteTimeout` messages embed the memory address of the anyio
cancel scope, which splits one failure mode into thousands of unique groups.
Normalize it first:

```bash
rg -o '"error": "[^"]*"' benchmarks.json \
  | sed 's/cancel scope [^;]*;/cancel scope <addr>;/' \
  | sort | uniq -c | sort -rn | head
```

For totals per failure class, bucket by signature. `[^"]*` stops at the first
escaped quote, so a few messages truncate mid-string; matching on a substring keeps
them in the right bucket anyway:

```bash
rg -o '"error": "[^"]*"' benchmarks.json | awk '
  /Too many open files/   {emfile++;    next}
  /ConnectTimeout/        {ctimeout++;  next}
  /WriteTimeout/          {wtimeout++;  next}
  /Request was cancelled/ {cancelled++; next}
  /HTTP 429/              {http429++;   next}
  /HTTP 500/              {http500++;   next}
  {other++}
  END {
    printf "EMFILE          %d\nConnectTimeout  %d\nWriteTimeout    %d\n", emfile, ctimeout, wtimeout
    printf "cancelled       %d\nHTTP 429        %d\nHTTP 500        %d\nother           %d\n", cancelled, http429, http500, other
  }'
```

Reconcile against the console table with the status counts. The three statuses are
disjoint and cover every request:

```bash
rg -o '"status": "[a-z_]*"' benchmarks.json | sort | uniq -c | sort -rn
# 110855 completed   7698 errored   4318 cancelled
```

`errored` must equal the console's error table total, and **`cancelled` is not in
that table** — those are requests still in flight when `max_duration` fired, so
their count tracks the concurrency at cutoff rather than any fault.

To read one full record, stream the file in chunks rather than loading it:

```bash
python3 - <<'PY'
needle = b"Too many open files"          # or any error substring
with open("benchmarks.json", "rb") as f:
    buf = b""
    while chunk := f.read(1 << 20):
        buf += chunk
        if (i := buf.find(needle)) != -1:
            print(buf[max(0, i - 1800):i + 400].decode("utf-8", "replace"))
            break
        buf = buf[-len(needle):]
PY
```

To compare runs, the presence of a signature is often the whole answer:

```bash
cd runs
for d in */; do
  f="$d/benchmarks.json"; [ -f "$f" ] || continue
  printf '%-42s EMFILE=%-8s ConnectTimeout=%s\n' "${d%/}" \
    "$(rg -c -o 'Too many open files' "$f" || echo 0)" \
    "$(rg -c -o 'ConnectTimeout\(' "$f" || echo 0)"
done
```

The full message is preserved there even though the console truncates it:

```
OpenAIResponseError('HTTP 429 Too Many Requests [x-ratelimit-limit=500, 500;w=3600
x-ratelimit-remaining=0 x-ratelimit-reset=3146] for
https://10.117.63.78/enterpriseai/gateway/v1/chat/completions')
```

Rate limit rejections from this gateway have an **empty body** and explain
themselves in headers, so `retry-after` and any `x-ratelimit-*` headers are
captured into the message. `x-ratelimit-reset` is the seconds until the bucket
refills.

Note that a non-default `--metrics sample_size=N` makes the errored list a sample
of N per status group, which would make the counts a sample rather than a total.
The default keeps every request.

## 6. How the NAI rate limit actually works

Worth understanding before interpreting a run. The policy for this model
(`kubectl -n nai-admin get backendtrafficpolicy uep-mock-uep -o yaml`) is:

```yaml
rateLimit:
  global:
    rules:
    - clientSelectors:
      - headers:
        - { name: x-ai-eg-model, type: Exact, value: mock-uep }
        - { name: X-RATELIMIT,   type: Distinct }        # one bucket per value
      cost:
        request:  { from: Number, number: 0 }            # a request costs nothing
        response: { from: Metadata, key: llm_total_token }  # a response costs its tokens
      limit: { requests: 500, unit: Hour }
```

Two consequences:

1. **The bucket is per distinct `X-RATELIMIT` value.** This is what the patch
   exercises — 5000 distinct values means 5000 independent buckets.
2. **The budget is spent by responses, not requests.** The limit is 500 *tokens*
   per hour per bucket, and each `just say hi` exchange costs 28
   (`llm_total_token`), so a bucket allows roughly 18 exchanges per hour.

Because cost is charged when the response arrives, a burst of concurrent requests
on one bucket is **not** rejected: they are all in flight before any cost
registers. The first `./run.sh 50 1` in this environment returned 50× HTTP 200
and charged 1400 tokens to `ratelimit-0`; the *next* run on that same value
returned 50× HTTP 429. To provoke 429s deliberately, either run twice against the
same value or run sequentially (`--profile kind=synchronous`) so each response
registers its cost before the next request goes out.

### Observed so far in this environment

| Run | Result |
| --- | --- |
| `./run.sh 100` (first, cold buckets) | 100× 200, no errors |
| `./run.sh 50 1` (first) | 50× 200; charged 1400 tokens to `ratelimit-0` |
| `./run.sh 50 1` (repeat) | 50× 429, `x-ratelimit-remaining=0`, reset in ~3150s |
| `./run.sh 100` (later) | 61× 200, **39× 500** with an empty body |
| `./run.sh 1000 1000 10m`, 1 worker | 46.5% `ConnectError`, 11× 500, 53 req/s |
| `./run.sh 1000 1000 60s`, 1 worker | 2.2% `ConnectTimeout`, 56 req/s, latency mdn 10.0s p95 31.3s |
| `WORKERS=5 ./run.sh 1000 1000 60s` | **no connect errors**, 258 req/s, latency mdn 3.0s p95 6.5s, 0.3% 500s |
| `WORKERS=5 ./run.sh 5000 5000 90s` | 220 req/s, latency mdn 12.7s p95 41.0s, 1.7% connect timeouts |
| `WORKERS=5 ./run.sh 5000 5000 10m` | 230 req/s, latency mdn 14.8s p95 50.1s, 1.4% connect timeouts, **4.3% 500s** |
| `WORKERS=8 ./run.sh 5000 5000 10m`, `ulimit -n 256` | 504 req/s, **41.6% `ConnectError` (all EMFILE)**, 0.16% 500s |

Past ~1000 concurrency the client stops gaining anything: 5000 in flight produced
*lower* throughput than 1000 (230 vs 258 req/s) with five times the latency, since
the extra 4000 requests only queue inside the worker processes. Concurrency is
worth raising only until throughput stops improving.

### The connect errors are the client's descriptor limit

A run reporting `ConnectError('All connection attempts failed')` for 40%+ of
requests looks like the gateway falling over. It is not. Every one of those
requests failed before a packet left the laptop, because macOS starts processes at
a **soft limit of 256 file descriptors** (`launchctl limit maxfiles`) and each
in-flight request needs one for its socket. `run.sh` now raises the limit itself;
these runs predate that.

The signature is identical across runs with identical arguments, and splits purely
on the descriptor limit of the shell that launched them:

| Run | `Too many open files` | `ConnectTimeout` | Shell `ulimit -n` |
| --- | --- | --- | --- |
| `5000 5000 10m` | 103,970 | 4,822 | 256 (default) |
| `5000 5000 10m` | 119,072 | 8,790 | 256 (default) |
| `5000 5000 90s` | **0** | 816 | ~1M (raised) |
| `5000 5000 10m` | **0** | 3,638 | ~1M (raised) |
| `1000 1000 90s` | **0** | 0 | ~1M (raised) |

Same cluster, same concurrency, same rate-limit config — only the client's
descriptor budget differs. The full chain names it outright:

```
ConnectError('All connection attempts failed')
  <- ConnectError(OSError('All connection attempts failed'))
  <- OSError('All connection attempts failed')
  <- OSError(24, 'Too many open files')
```

The error count follows the arithmetic exactly: 8 worker processes × ~256
descriptors caps the client near 2,000 concurrent sockets, so of 5000 requested in
flight the surplus fails instantly. Measured in-flight successes were ~2,378
(290 req/s × 8.2 s median latency), and 119,072 + 8,790 = 127,862, the reported
"no response" total to the request.

Envoy's counters agree that it never refused anything:

```
listener.0.0.0.0_10443.downstream_cx_overflow: 0     # never hit a connection cap
listener.0.0.0.0_10443.downstream_pre_cx_timeout: 0  # no stalled handshakes
http.https-10443.downstream_cx_protocol_error: 0
```

Two details make this easy to misdiagnose. The failure is *immediate*, not a
timeout, so it cannot be a network or accept-queue problem — a saturated server
drops SYNs and yields `ConnectTimeout` instead. And `ulimit -n` measured in one
shell says nothing about the shell that ran the benchmark; the ~1M this document
previously cited was a raised session, not what the workers inherited.

The `ConnectTimeout` column is the residue that is genuinely about load: it
survives at 1.4–1.7% at 5000 concurrency even with descriptors to spare, because
the gateway is past its knee. Compare the client's latency against Envoy's
`duration` field before blaming the server, and check the innermost errno before
blaming either.

### Attributing an error to a layer

The error class tells you which **phase** failed, and the phase bounds who can be
responsible. Only then does one specific counter settle it.

The phase boundary is sharper than it looks, because of how the timeouts are built:
`httpx.Timeout(5.0, read=timeout, connect=timeout_connect)`. The first argument is
the default for every category not named, so `write` and `pool` are **5 s** while
`read` is `timeout`, which `run.sh` leaves at `None`. Read is therefore unlimited:
a slow gateway or a slow model can never produce an error, only latency. Every
timeout you see happened *before* the request was fully handed over.

| Client error | Phase | Who it can be | Counter that decides |
| --- | --- | --- | --- |
| `OSError(24, ...)` EMFILE | socket allocation | client only | never reaches Envoy at all |
| `ConnectTimeout` | TCP + TLS, 5 s | client loop or Envoy's listener | `downstream_pre_cx_timeout`, `downstream_cx_overflow`, `watchdog_miss` |
| `WriteTimeout` | sending body, 5 s | client loop or Envoy's read side | `downstream_cx_protocol_error`, `watchdog_miss` |
| `HTTP 500` | server response | Envoy, ratelimit svc, or Valkey | `ratelimit.error`, `ratelimit_cluster.upstream_rq_504`, then Valkey `latency history` |
| `HTTP 429` | server response | rate limit working as designed | `ratelimit.over_limit` |
| `Request was cancelled` | none | the harness | status is `cancelled`, not `errored` |

The single most useful check is a reconciliation. Envoy's `downstream_rq_total` for
the run window against the client's `completed + 5xx`: **anything the client
reports that Envoy did not count never left the client.**

Snapshot Envoy either side of a run and diff every counter that moved:

```bash
POD=$(kubectl get pod -n envoy-gateway-system \
        -l gateway.envoyproxy.io/owning-gateway-name=nai-ingress-gateway \
        -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n envoy-gateway-system "$POD" 19000:19000 &
curl -s localhost:19000/stats > /tmp/envoy-before.txt    # then run the benchmark
curl -s localhost:19000/stats > /tmp/envoy-after.txt

python3 - <<'PY'
def load(p):
    d = {}
    for line in open(p):
        k, _, v = line.partition(":")
        if v.strip().isdigit():
            d[k.strip()] = int(v)
    return d
before, after = load("/tmp/envoy-before.txt"), load("/tmp/envoy-after.txt")
for k in sorted(after):
    if (delta := after[k] - before.get(k, 0)):
        print(f"{delta:>10}  {k}")
PY
```

Bracket Valkey the same way. An empty `latency history` for the window exonerates
it, which moves any rate-limit timeouts onto the rate limit service itself:

```bash
H=nai-valkey-0.nai-valkey-headless.nai-system.svc.cluster.local
V="kubectl exec -n nai-system valkey-cli -- valkey-cli -h $H"
$V latency reset                                   # before the run
$V latency history aof-write                       # after
$V info persistence | rg 'aof_delayed_fsync|aof_rewrite_in_progress'
```

### Reading Envoy's counter names

Envoy stat names are `<scope>.<resource>.<metric>`, and the scope tells you which
side of the proxy you are looking at. Once you can decode the name, the counter
list reads as a narrative of the request path.

| Scope prefix | What it covers |
| --- | --- |
| `listener.0.0.0.0_10443.` | the L4 socket: TCP accepts and TLS handshakes. `10443` is Envoy's in-pod port; the LB maps `443` → nodePort `30964` → `10443` |
| `http.https-10443.` | the HTTP connection manager on that listener: requests and status codes |
| `cluster.httproute/<ns>/<route>/rule/<n>.` | the per-route upstream, i.e. your model backend and its rate limit rule |
| `cluster.ratelimit_cluster.` | Envoy's gRPC connection to the `envoy-ratelimit` pod |
| `server.worker_<n>.` | one event loop thread. The count equals Envoy's CPU limit, 3 here |

Two suffix conventions carry most of the meaning. `downstream_` is the client→Envoy
side and `upstream_` is the Envoy→backend side, so a `downstream` failure is about
your load arriving and an `upstream` failure is about a backend Envoy called. And
`_cx_` is a connection while `_rq_` is a request, which is what lets you compute
keepalive reuse as `rq_total / cx_total`.

That decodes the block as follows:

| Counter | Reads as | Non-zero means |
| --- | --- | --- |
| `http.*.downstream_rq_total` | requests Envoy accepted | your load that actually arrived |
| `http.*.downstream_rq_5xx` | 5xx Envoy returned | compare against the client's count |
| `cluster.httproute/…/ratelimit.ok` | checks allowed | normal traffic |
| `cluster.httproute/…/ratelimit.over_limit` | checks that hit the limit | 429s, the limiter working |
| `cluster.httproute/…/ratelimit.error` | checks that *failed* | 500s under `failClosed: true` |
| `cluster.ratelimit_cluster.…upstream_rq_504` | gRPC calls that timed out | the 20 ms budget being blown |
| `cluster.ratelimit_cluster.…upstream_rq_503` | ratelimit svc unreachable | no healthy endpoint, or a connection failure |
| `cluster.ratelimit_cluster.…upstream_rq_200` | checks completed | divide by `rq_total` for checks per request |
| `listener.*.downstream_cx_total` | TCP connections accepted | denominator for keepalive reuse |
| `listener.*.downstream_pre_cx_timeout` | accepted, handshake never finished | Envoy's own TLS handshakes are stalling |
| `listener.*.downstream_cx_overflow` | connections refused by a listener cap | you hit a configured connection limit |
| `http.*.downstream_cx_protocol_error` | malformed HTTP framing | a client or intermediary is corrupting the stream |
| `server.worker_<n>.watchdog_miss` | an event loop failed to tick | **Envoy itself** stalled, not the client |

The three readings that do the diagnostic work:

- **`downstream_rq_total` vs the client's `completed + 5xx`.** Requests the client
  counted that Envoy did not never left the client. This is the client-vs-server
  test, and it is the one that identified the descriptor exhaustion.
- **`downstream_rq_5xx` vs `ratelimit.error`.** Equal means every server error is a
  rate-limit failure and none came from the model, since a rejected check is never
  forwarded upstream.
- **`upstream_rq_200 / downstream_rq_total`.** The checks-per-request ratio. It is
  **2** here — one check for the request and one to charge `llm_total_token` — which
  is why Valkey sees twice the request rate as writes.

Counts across scopes will not match exactly, and the mismatches are informative
rather than errors. `upstream_rq_504` is global across every route while
`ratelimit.error` is per route, so 333 timeouts producing 274 errors on one route
means the rest landed elsewhere. And `downstream_rq_total` running slightly ahead of
the client's completed count is the tail of requests Envoy served whose responses the
client had already abandoned.

Worked example, the 10-minute 5000-concurrency run once descriptors were fixed.
Client: 110,855 completed, 274 × HTTP 500, 7,424 no-response, 4,318 cancelled.
Envoy deltas for the same window:

```
    111849  http.https-10443.downstream_rq_total     # ~= client's 111,129 done
       274  http.https-10443.downstream_rq_5xx       # exactly the client's 500s
       274  ...uep-nanolama-uep/rule/0.ratelimit.error
       333  cluster.ratelimit_cluster.internal.upstream_rq_504
    222052  cluster.ratelimit_cluster.internal.upstream_rq_200   # 2 checks/request
     73655  listener.0.0.0.0_10443.downstream_cx_total           # 1.52 rq/connection
         0  listener.0.0.0.0_10443.downstream_pre_cx_timeout
         0  listener.0.0.0.0_10443.downstream_cx_overflow
         0  http.https-10443.downstream_cx_protocol_error
         0  server.worker_{0,1,2}.watchdog_miss
```

Conclusions that follow, each from one counter. The 274 500s are the rate limit
service, because `ratelimit.error` matches them exactly — and this time Valkey
recorded **no** latency events and `aof_delayed_fsync` never moved off 2, so unlike
the earlier run these are not disk stalls but the service missing its own 20 ms
budget at 370 checks/s on one replica. The 7,424 connect and write timeouts are the
client and the TLS front door, because Envoy hit no connection cap, had no hanging
handshake, and never stalled a worker; 5000 simultaneous handshakes against three
Envoy worker threads simply do not all finish inside 5 s. The 4,318 cancellations
are the harness cutting off the in-flight tail at the duration limit, and their
count tracks the median concurrency of 4,094 rather than any fault.

### The 500s are rate-limit service timeouts

Every 500 the client sees maps exactly to a failed rate-limit check. Envoy labels
each one itself, and the request never reaches the model:

```
response_code: 500
response_code_details: rate_limiter_error
response_flags: RLSE          # Rate Limit Service Error
upstream_host: null           # never forwarded upstream
duration: 20-171 ms           # the 20 ms filter timeout being blown
```

A 90-second run at 1000 concurrency reported 72 HTTP 500s client-side against 71
`rate_limiter_error` entries in Envoy's access log for the same window — the same
individual requests from both ends. Across all runs the counters line up too:

```
cluster.ratelimit_cluster.upstream_rq_timeout: 174   # gRPC calls that timed out
cluster.ratelimit_cluster.internal.upstream_rq_504: 174
cluster.ratelimit_cluster.internal.upstream_rq_503: 8
http.https-10443.downstream_rq_5xx: 182              # what clients saw as HTTP 500
```

The rate limit filter runs with `failure_mode_deny: true` and no explicit
`timeout`, so it uses Envoy's 20 ms default: any check slower than 20 ms becomes a
500 rather than being allowed through. That budget has to cover a gRPC round trip
to `envoy-ratelimit`, which runs as a **single replica requesting 100m CPU with no
limit**, plus a Valkey write — every check is an `INCR`, so the whole request rate
lands on Valkey as writes.

Valkey is where the 20 ms goes. It persists with `appendonly yes` /
`appendfsync everysec` **and** `save 60 10000` onto a 2Gi `nutanix-volume` PVC, so
under load it fsyncs every second and forks for an RDB snapshot every minute on
network-attached storage. During a sustained run it says so directly:

```
1:M 29 Jul 2026 09:48:27.553 * Asynchronous AOF fsync is taking too long
   (disk is busy?). Writing the AOF buffer without waiting for fsync to
   complete, this may slow down the server.
```

Fifteen of those, and they land in the same two minutes as the 500s. Extracting all
6,207 failures of the 10-minute run by timestamp shows a burst, not a steady rate:

```
15:16    377
15:17   4752     <- Valkey logged 8 AOF-slow warnings in this minute
15:18    685     <- and 7 in this one
15:19    297
15:20     52
15:21+    44
```

So the 500s are not a fixed tax on throughput; they are what a Valkey disk stall
looks like from the client. Neither `envoy-ratelimit` nor Valkey crashed or
restarted during any run and the sentinels logged nothing — this is latency, not
failure.

Scaling either service fixes nothing, because neither is short of resources.
Measured under load at 254 req/s:

| Component | Measured | Budget |
| --- | --- | --- |
| `envoy-ratelimit` CPU | 0.14 cores | no limit set, free to burst |
| `nai-valkey-0` CPU | 0.02 cores | no limit set |
| Valkey `INCRBY` | 0.8 µs per call | 20 ms |
| Valkey `EXPIRE` | 0.9 µs per call | 20 ms |
| Valkey `SLOWLOG` entries (>10 ms) | 0 | — |
| Valkey `aof-write` latency event | 47 ms latest, **118 ms peak** | 20 ms |
| Valkey `aof_delayed_fsync` | 19 | 0 |

The command path is four orders of magnitude inside the budget, and the AOF write
stall is the only measured thing that exceeds it.

The counters do not need persisting: `db0` holds 6,086 keys of which 6,006 carry a
TTL, all shaped `..._ratelimit-<value>_<hour-epoch>`, totalling 13 MB. Losing them
on restart resets budgets, which is what an hour boundary does anyway. Dropping
`appendonly` and the `save` rules removes the stall source; raising
`rateLimit.timeout` (in the `envoy-gateway-config` ConfigMap, **not** the
BackendTrafficPolicy) only trades the 500s for that much added tail latency.

To pull the individual failures out of a report:

```bash
rg -o '"error_status": 500' runs/<run>/benchmarks.json | wc -l
```

Envoy's own per-request lines rotate out fast (its access log holds only ~18
seconds of traffic at 250 req/s), so capture them during the run if you need them:

```bash
kubectl logs -n envoy-gateway-system -f \
  envoy-nai-system-nai-ingress-gateway-ff52ba1f-5798979b94-9xg25 -c envoy \
  | rg '"response_code":500' > envoy-500s.jsonl
```

Note also that `envoy-ratelimit` has restarted 204 times (exit code 2), though the
most recent restart predates these runs and its log shows only xDS reconnects.

Check a bucket's state directly at any time:

```bash
curl -sk -D - -o /dev/null -X POST "$NAI_TARGET/v1/chat/completions" \
  -H "Authorization: Bearer $NAI_API_KEY" -H 'X-RATELIMIT: ratelimit-0' \
  -H 'Content-Type: application/json' \
  --data-binary '{"model":"mock-uep","messages":[{"role":"user","content":"just say hi"}]}' \
  | grep -iE 'HTTP/|ratelimit|retry-after'
```

## 7. Verify header uniqueness

### Client side

The patched backend logs every rendered value at DEBUG level, so a run started
with `GUIDELLM__LOGGING__LOG_FILE` (as `run.sh` does) leaves a full record.
`run.sh` audits it automatically, but you can re-check any run:

```bash
./verify_headers.py runs/<timestamp>-n100/guidellm-debug.log 100
```

It exits non-zero if the number of distinct values is not what you asked for, or
if any value was sent twice.

### Server side

`X-RATELIMIT` is consumed by the Envoy global rate limit service, so the cluster
is where you confirm the gateway actually treated the values as distinct keys.
With `KUBECONFIG=/Users/vijay.pal/.kube/vijay-dev-dandelion2-kubeconfig.conf`:

```bash
# The gateway behind 10.117.63.78
kubectl -n envoy-gateway-system get svc envoy-nai-system-nai-ingress-gateway-ff52ba1f

# Rate limit service logs and restarts during the run
kubectl -n envoy-gateway-system logs deploy/envoy-ratelimit --tail=200
kubectl -n envoy-gateway-system get pods -l app.kubernetes.io/name=envoy-ratelimit

# Envoy access logs, to see per-request 200 vs 429
kubectl -n envoy-gateway-system logs \
  deploy/envoy-nai-system-nai-ingress-gateway-ff52ba1f -c envoy --tail=200
```

Rate-limit counters are kept in the Valkey instance in `nai-system`
(`nai-valkey-0`/`nai-valkey-1` behind `nai-valkey-headless`), so counting keys
there after a run is another way to confirm the distinct-key count.

## Patch reference

### `backends/openai/common.py`

`UniqueHeaderGenerator` renders header values from a template. Worker processes
share no state, so a plain counter would repeat the same values in each process;
indices are strided by the worker count instead (worker `w` on its `n`-th request
yields `n * workers + w`), which no other worker can produce.

`OpenAIResponseError` and `raise_for_status` replace
`httpx.Response.raise_for_status`. The status code is kept as a field rather than
buried in a message, and the response body plus any `retry-after` /
`x-ratelimit-*` headers are captured. Streaming responses are read before raising,
because their body is not yet consumed at the point of failure.

### `backends/openai/http.py` — new `openai_http` backend options

| Option | Meaning |
| --- | --- |
| `unique_headers` | Header name to value template, re-rendered per request. Placeholders: `{index}`, `{seq}`, `{worker}`, `{uuid}`, `{value}`. Overrides any static header of the same name. |
| `unique_headers_count` | Confine `{index}` to `[0, count)` to draw from a fixed pool. Defaults to the length of `unique_headers_values`, otherwise indices are unbounded and never repeat. |
| `unique_headers_values` | Literal values selected by index for `{value}`, for pools you cannot generate (real API keys, tenant ids). |
| `unique_headers_workers` | Stride keeping indices from colliding across worker processes. Must be at least the number of worker processes; defaults to the `max_worker_processes` setting. |
| `text_content_as_string` | Send text-only chat content as `"content": "..."` instead of `[{"type": "text", ...}]`. |

Bad templates are rejected at startup rather than mid-run:
`unique_headers.X-RATELIMIT=rl-{bogus}` fails validation listing the supported
placeholders. Literal braces must be escaped as `{{` and `}}`.

### `backends/openai/request_handlers.py` — plain-string chat content

`text_content_as_string` collapses a text-only content-part list into a plain
string, for both the current turn and replayed history. Messages carrying an
image, video, or audio part keep the list form, because they cannot be expressed
as a string.

### `schemas/info.py` and `scheduler/worker.py` — status code on the request

`RequestInfo.error_status` holds the HTTP status code of a failed request, or
`None` when there was no response. The worker fills it from the raised exception,
accepting either a `status_code` attribute or a nested `response.status_code`, so
it works for any backend rather than just this one.

`_error_repr` records the causes an exception wraps, not just the exception. httpx
reports `ConnectError('All connection attempts failed')`, which says nothing about
which resource ran out; the errno that does sits in the wrapped `OSError`. Without
the chain, a 40%-failure run is indistinguishable from any other transport fault —
`OSError(24, 'Too many open files')` at the end of the chain is what identified the
client's descriptor limit rather than a refused or timed-out connection.

### `benchmark/outputs/console.py` — error summary table

`print_error_summary_table` groups errored requests by status code with counts,
share of the run, and a sample message. It prints only when a run had failures.

`_abbreviate_error` keeps both ends of a cause chain when it exceeds the column
budget. Chains are outermost-first, so plain truncation drops the errno and prints
the one part that is the same for every transport fault. This cost a full
investigation cycle: the console showed only
`ConnectError('All connection attempts failed') <- ConnectError(OSError(...)) <-
OSError('All connection attempts failed')` while the `Too many open files` that
named the fault sat just past the cutoff in `benchmarks.json`.

### `data/loaders/torch.py` — `cycle` for duration runs

The scheduler iterates the data loader once and stops queueing when it ends, so a
`max_duration` run would finish when the dataset ran out rather than at the time
limit. `cycle=true` restarts each exhausted dataset. An empty dataset still ends
iteration instead of spinning forever, since the restarted iterator raises
`StopIteration` immediately.

## Why a single worker process

`run.sh` sets `GUIDELLM__MAX_WORKER_PROCESSES=1`. guidellm normally spreads
concurrency over up to 10 worker processes that pull from a **shared queue**, so
per-process request counts are uneven — in a 10-request test across 10 workers,
5 workers took 2 requests each and the rest took none. With a bounded pool
(`unique_headers_count`) an unlucky worker then wraps around and repeats a value.

One worker process owns the whole counter, so a pool of N is consumed exactly
once and in order: `ratelimit-0 … ratelimit-(N-1)`. One asyncio event loop
handles thousands of in-flight requests comfortably.

If you do want multiple worker processes, drop `unique_headers_count`. Values are
then guaranteed distinct for the whole run regardless of how requests are
distributed; they are just not contiguous.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `404` with `{"error": "No matching cassette found"}` and all requests errored | `mock-uep` is a cassette-replay mock that matches the exact request body. Set `text_content_as_string=true`, and do not add body fields the recording lacks. |
| `Worker process ... died unexpectedly (signal 11)` | `fork()` after torch is imported segfaults on macOS. Set `GUIDELLM__MP_CONTEXT_TYPE=spawn`. |
| `'DatasetDict' object has no attribute 'info'` | The JSON loader returns a split dict. Pass `load_kwargs.split=train` on `--data`. |
| Prompts arrive with a trailing newline | `kind=text_file` keeps the `\n` from each line, which breaks exact-body matching. Use `kind=json_file` as `run.sh` does. |
| `Requested N samples, but only 1 available` | `--data-loader samples=N` needs a dataset with at least N rows. Grow `prompts.jsonl`. |
| All latency and token stats are `0.0` | Every request errored. Read the Request Error Summary table for the status code, or `guidellm-debug.log` for full tracebacks. |
| Expected 429s but everything returned 200 | The bucket is charged from the *response*, so a concurrent burst all passes before any cost registers. Run again on the same value, or use `--profile kind=synchronous`. |
| Run reports fewer distinct values than requests | A bounded pool with multiple worker processes. Use one worker process, or drop `unique_headers_count`. |

## Environment notes

- `GUIDELLM__MP_CONTEXT_TYPE=spawn` is required on macOS (see above).
- `validate_backend=false` skips the startup `/health` probe, which this gateway
  does not expose.
- `verify=false` is the `-k` in the reference curl; the gateway serves a
  self-signed certificate.
- `http2=false` keeps requests on HTTP/1.1 so concurrency maps to real
  connections rather than being multiplexed into a few HTTP/2 streams.
- The tokenizer resolves to the model name (`mock-uep`), which is not a
  Hugging Face model. That is fine — nothing loads it, because token counts come
  from the server's `usage` field.
