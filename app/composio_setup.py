"""Composio client and per-user session."""
import os
from functools import lru_cache

from composio import Composio
from composio_langchain import LangchainProvider
from dotenv import load_dotenv

load_dotenv()

TOOLKITS = [t.strip() for t in os.getenv("COMPOSIO_TOOLKITS", "gmail,googledrive").split(",") if t.strip()]


@lru_cache(maxsize=1)
def client() -> Composio:
    # Reads COMPOSIO_API_KEY from the environment.
    return Composio(provider=LangchainProvider())


def get_session(user_id: str):
    """One Composio session per end user. user_id is YOUR id for the user
    (e.g. their email or DB id); Composio stores their Google tokens under it."""
    return client().create(
        user_id=user_id,
        toolkits=TOOLKITS,
        manage_connections=False,   # sign-in happens via connect.py / your API, not the LLM
        sandbox={"enable": False},  # no remote code-execution / bash tools
    )
