#!/usr/bin/env python3
"""
Audit the X-RATELIMIT values a guidellm run actually sent.

The patched OpenAI HTTP backend logs every rendered value at DEBUG level, so a run
started with GUIDELLM__LOGGING__LOG_FILE and LOG_FILE_LEVEL=DEBUG leaves a record
of what the server was sent.

With an expected count, the run passes when it sent exactly that many distinct
values -- so deliberately sharing one value across many requests is a pass.
Without one, any repeated value is a failure.

Usage:
    ./verify_headers.py <debug-log> [expected-count]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter

MESSAGE = re.compile(r"^unique_headers (\S+) (\{.*\})$")


def collect(log_path: str) -> list[tuple[str, dict[str, str]]]:
    """
    :param log_path: Path to a guidellm JSON-serialized debug log
    :return: (request_id, headers) for every unique-header render in the log
    """
    found = []
    with open(log_path) as log:
        for line in log:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)["record"]["message"]
            except (ValueError, KeyError):
                continue
            if (match := MESSAGE.match(message)) is None:
                continue
            found.append((match.group(1), ast.literal_eval(match.group(2))))
    return found


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    log_path = sys.argv[1]
    expected = int(sys.argv[2]) if len(sys.argv) > 2 else None

    rendered = collect(log_path)
    if not rendered:
        print(f"no unique_headers records found in {log_path}")
        return 1

    names = sorted({name for _, headers in rendered for name in headers})
    status = 0

    print(f"requests with rendered headers : {len(rendered)}")
    for name in names:
        values = [headers[name] for _, headers in rendered if name in headers]
        counts = Counter(values)
        repeats = {value: n for value, n in counts.items() if n > 1}
        print(f"{name}: {len(values)} sent, {len(counts)} distinct")

        if expected is None:
            if repeats:
                status = 1
                worst = sorted(repeats.items(), key=lambda kv: -kv[1])[:5]
                print(f"  repeated values: {worst}")
        elif len(counts) != expected:
            status = 1
            print(f"  expected {expected} distinct values, got {len(counts)}")
        elif repeats:
            reused = max(repeats.values())
            print(f"  as configured, values are shared (up to {reused} requests each)")

        ordered = sorted(values)
        print(f"  first: {ordered[0]}   last: {ordered[-1]}")

    print("PASS" if status == 0 else "FAIL")
    return status


if __name__ == "__main__":
    sys.exit(main())
