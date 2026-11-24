#!/usr/bin/env python3
"""Read failure metadata without printing prompts, responses, or credentials."""
import argparse
import collections
import datetime
import json
import sqlite3
import time
from pathlib import Path


def summarize(connection, since):
    failures = []
    for sid, stamp, raw in connection.execute(
        "SELECT session_id,time_created,data FROM session_message "
        "WHERE type='assistant' AND time_created>=? ORDER BY time_created", (since,)
    ):
        message = json.loads(raw)
        error = message.get('error') or {}
        if error.get('type') != 'provider.free-limit':
            continue
        previous = connection.execute(
            "SELECT data FROM session_message WHERE session_id=? "
            "AND type='assistant' AND time_created<? ORDER BY time_created DESC LIMIT 1",
            (sid, stamp),
        ).fetchone()
        previous = json.loads(previous[0]) if previous else {}
        tokens = previous.get('tokens')
        context = None if tokens is None else (
            tokens.get('input', 0) + (tokens.get('cache') or {}).get('read', 0)
            + (tokens.get('cache') or {}).get('write', 0)
        )
        delay = error.get('retryAfterMs')
        when = datetime.datetime.fromtimestamp(stamp / 1000, datetime.timezone.utc)
        failures.append(dict(
            time_utc=when.isoformat(), model=(message.get('model') or {}).get('id'),
            status=error.get('status'), retry_after_ms=delay,
            retry_at_utc=(when + datetime.timedelta(milliseconds=delay)).isoformat()
            if isinstance(delay, (int, float)) and delay >= 0 else None,
            preceding_context_tokens=context,
        ))
    return dict(
        quota_failure_count=len(failures),
        failures_by_model=dict(collections.Counter(x['model'] or 'unknown' for x in failures)),
        recent_failures=failures[-20:],
        interpretation='A provider.free-limit response is an upstream free-tier rejection. '
        'It can represent quota exhaustion or a client/network eligibility restriction. '
        'Preceding token counts describe an earlier request; they do not establish '
        'the rejected request size or the provider quota accounting rules.',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--database', type=Path,
                        default=Path.home() / '.local/share/opencode/opencode.db')
    args = parser.parse_args()
    if args.days < 1:
        parser.error('--days must be positive')
    try:
        with sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            report = summarize(connection, int((time.time() - args.days * 86400) * 1000))
    except (sqlite3.Error, OSError, ValueError) as error:
        parser.exit(1, 'Unable to read OpenCode failure metadata: ' + str(error) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
