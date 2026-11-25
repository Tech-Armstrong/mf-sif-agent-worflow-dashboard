"""One-time sign-in: links a user's Google account to Composio.

    python -m scripts.connect <user_id> [toolkit ...]
    python -m scripts.connect techops gmail googledrive
"""
import sys

from app.composio_setup import TOOLKITS, get_session


def connect(user_id: str, toolkit: str, timeout: float = 300):
    session = get_session(user_id)
    req = session.authorize(toolkit)  # add callback_url="https://your.app/callback" in a web app
    print(f"\n[{toolkit}] Open this link and sign in:\n{req.redirect_url}\n")
    account = req.wait_for_connection(timeout=timeout)
    print(f"[{toolkit}] connected: id={getattr(account, 'id', '?')} status={getattr(account, 'status', '?')}")
    return account


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    user = sys.argv[1]
    for tk in sys.argv[2:] or TOOLKITS:
        connect(user, tk)
