"""Terminal chat with approval prompts.

    python -m scripts.chat <user_id>
"""
import json
import sys
import uuid

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.agent import make_agent


def main(user_id: str):
    graph = make_agent(user_id)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    print("Connected-apps agent. Ctrl+C to quit.")
    while True:
        text = input("\nyou> ").strip()
        if not text:
            continue
        result = graph.invoke({"messages": [HumanMessage(text)]}, config)
        while result.get("__interrupt__"):
            actions = result["__interrupt__"][0].value["actions"]
            print("\nApproval needed:")
            for a in actions:
                print(f"  - {a['tool']}\n    {json.dumps(a['arguments'], indent=2, ensure_ascii=False)[:2000]}")
            ok = input("Approve? [y/N] ").strip().lower() == "y"
            feedback = "" if ok else input("What should change? (optional) ").strip()
            result = graph.invoke(Command(resume={"approve": ok, "feedback": feedback}), config)
        print(f"\nagent> {result['messages'][-1].content}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
