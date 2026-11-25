"""Sends rubric scores to LangSmith as feedback on the current trace.

Separate from the evaluators so they stay free of LangSmith. Tracing is an observer: a LangSmith failure is printed
and never stops an extraction. With LANGSMITH_TRACING off, traceable does nothing and no score is sent.

Where traces go is set in .env: LANGSMITH_AGENT_ID with LANGSMITH_AGENT_ENVIRONMENT, or LANGSMITH_PROJECT. Set only one
of the two: with both, LangSmith refuses to trace.
"""
from dotenv import load_dotenv

load_dotenv()  # searches upward, so the repo-root .env is found

from langsmith import traceable  # noqa: E402,F401  (re-exported for the pipeline)
from langsmith.run_helpers import get_current_run_tree  # noqa: E402
from langsmith.run_trees import get_cached_client  # noqa: E402

MAX_COMMENT = 2000


def post_scores(scores):
    """Attach {rubric: {"score", "comment"}} to the run this is called inside. Returns how many were sent."""
    run = get_current_run_tree()
    if run is None:
        return 0
    sent = 0
    try:
        client = get_cached_client()
        for key, value in scores.items():
            comment = value.get("comment")
            client.create_feedback(run_id=run.id, trace_id=run.trace_id, key=key, score=value["score"],
                                   comment=comment[:MAX_COMMENT] if comment else None)
            sent += 1
    except Exception as e:
        print(f"  ! LangSmith feedback not sent: {type(e).__name__}: {str(e)[:200]}", flush=True)
    return sent


def flush_traces():
    """Send what is still queued: a short script can otherwise exit before its traces are uploaded."""
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers
        wait_for_all_tracers()
        get_cached_client().flush()
    except Exception as e:
        print(f"  ! LangSmith flush failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
