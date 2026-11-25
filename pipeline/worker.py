"""Entry point of the parse Container Apps Jobs.

    python -m pipeline.worker portfolio     # job "parse-portfolio", queue PORTFOLIO_QUEUE (default parse-portfolio)
    python -m pipeline.worker factsheet     # job "parse-factsheet", queue FACTSHEET_QUEUE (default parse-factsheet)

The app puts {"source": "files/..."} on the queue when a file is uploaded (PARSE_RUNNER=queue). The job's
KEDA azure-queue rule starts an execution when a message is waiting. Each execution takes messages until
the queue is empty (or MAX_MESSAGES), parses each with parse_jobs.run (which stores the result and status),
and deletes the message. A message that keeps crashing the job is dropped after MAX_TRIES and marked failed.
"""
import json
import os
import sys

from pipeline import parse_jobs

MAX_TRIES = int(os.getenv("MAX_TRIES", "3"))
MAX_MESSAGES = int(os.getenv("MAX_MESSAGES", "20"))


def main(kind: str) -> int:
    if kind not in parse_jobs.QUEUES:
        print(f"usage: python -m pipeline.worker {'|'.join(parse_jobs.QUEUES)}")
        return 2
    from pipeline import blob_store

    q = blob_store.queue_client(parse_jobs.QUEUES[kind])
    # keep the message hidden for the whole parse, so no other execution takes it meanwhile
    hide_s = parse_jobs.TIMEOUT_S[kind] + 900
    done = 0
    while done < MAX_MESSAGES:
        msgs = list(q.receive_messages(max_messages=1, visibility_timeout=hide_s))
        if not msgs:
            break
        m = msgs[0]
        try:
            body = json.loads(m.content)
            source, scope, force = body["source"], body.get("scope", "uploaded"), bool(body.get("force"))
            only = body.get("only") or None  # factsheets: re-parse just these categories
        except (ValueError, KeyError, TypeError):
            print(f"Dropping a message that is not {{\"source\": ...}}: {m.content[:200]!r}", flush=True)
            q.delete_message(m)
            continue
        if m.dequeue_count > MAX_TRIES:
            print(f"{source}: tried {m.dequeue_count - 1} times, giving up", flush=True)
            res = {"source": source, "kind": kind, "status": "failed", "finished_at": parse_jobs._now(),
                   "error": f"The parse job stopped {MAX_TRIES} times on this file. Parse it again to retry."}
            blob_store.put_json(blob_store.parsed_path(source), res)
            parse_jobs._update_index(source, parse_jobs._summary(res))
            q.delete_message(m)
            continue
        print(f"{source}: parsing", flush=True)
        res = parse_jobs.run(source, scope, force, only)
        print(f"{source}: {res.get('status')} {res.get('error') or ''} ({res.get('seconds', '?')} s)", flush=True)
        q.delete_message(m)
        done += 1
    print(f"{done} file(s) parsed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
