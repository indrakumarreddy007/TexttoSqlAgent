"""Interactive CLI.

    python -m sqlagent.cli --role regional_mgr --regions SE "top customers last quarter"
"""
from __future__ import annotations

import argparse
import json

from sqlagent.bootstrap import build_agent
from sqlagent.core.schemas import Status, UserContext


def main() -> None:
    ap = argparse.ArgumentParser(description="Ask the Halcyon SQL Copilot a question")
    ap.add_argument("question", nargs="?")
    ap.add_argument("--role", default="analyst")
    ap.add_argument("--regions", nargs="*", default=[])
    ap.add_argument("--user", default="cli-user")
    ap.add_argument("--json", action="store_true", help="print the full response as JSON")
    args = ap.parse_args()

    agent = build_agent()
    user = UserContext(user_id=args.user, role=args.role, regions=args.regions)
    question = args.question or input("question> ")
    conv = None
    while True:
        r = agent.ask(question, user, conv)
        conv = r.conversation_id
        if args.json:
            print(json.dumps(r.model_dump(mode="json"), indent=2, default=str))
        if r.status == Status.needs_clarification:
            for q in r.clarification.questions:
                print(f"? {q.question}" + (f"  options: {', '.join(q.options)}" if q.options else ""))
            question = input("answer> ")
            continue
        if not args.json:
            print(f"[{r.status.value}] {r.message}")
            for a in r.assumptions:
                print(f"  assumption: {a}")
            if r.sql:
                print(f"\nSQL (as executed):\n{r.sql}\n")
            if r.rows:
                print(" | ".join(r.columns))
                for row in r.rows[:20]:
                    print(" | ".join(str(v) for v in row))
        break


if __name__ == "__main__":
    main()
