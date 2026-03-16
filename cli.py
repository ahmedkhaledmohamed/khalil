#!/usr/bin/env python3
"""Minimal CLI for local Khalil testing — bypasses Telegram, calls core logic directly."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


async def _ask(query: str) -> str:
    from knowledge.context import get_relevant_context
    from server import ask_llm

    context = await get_relevant_context(query)
    return await ask_llm(query, context)


def _run_query(query: str):
    response = asyncio.run(_ask(query))
    print(response)


def _repl():
    print("Khalil CLI (interactive mode). Type 'exit' or Ctrl-D to quit.\n")
    while True:
        try:
            query = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query or query.lower() in ("exit", "quit"):
            break
        _run_query(query)
        print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _run_query(" ".join(sys.argv[1:]))
    elif not sys.stdin.isatty():
        _run_query(sys.stdin.read().strip())
    else:
        _repl()
