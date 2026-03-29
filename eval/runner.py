"""Instrumented test runner for PharoClaw's eval pipeline.

Runs TestCases through the full message pipeline using an InstrumentedChannel
that captures all output without printing to stdout.

Usage:
    python -m eval.runner                           # run all from fixtures/cases.json
    python -m eval.runner --cases path/to/cases.json
    python -m eval.runner --limit 50                # run first 50 only
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# eval/ is a subdirectory — add parent to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import ActionButton, Channel, ChannelType, IncomingMessage, SentMessage
from channels.message_context import MessageContext
from eval.cases import TestCase, load_cases

log = logging.getLogger("pharoclaw.eval.runner")

# Suppress noisy loggers during eval runs
for _noisy in ("httpx", "httpcore", "googleapiclient", "urllib3", "pharoclaw.state", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# InstrumentedChannel — captures messages instead of printing
# ---------------------------------------------------------------------------

class InstrumentedChannel(Channel):
    """Channel that silently captures all sent messages for eval."""

    channel_type = ChannelType.TELEGRAM

    def __init__(self):
        self.messages: list[str] = []
        self._msg_counter: int = 0

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: list[list[ActionButton]] | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        self._msg_counter += 1
        # Skip "Thinking..." placeholder messages
        if text.strip() != "Thinking...":
            self.messages.append(text)
        return SentMessage(chat_id=chat_id, message_id=self._msg_counter, channel=self)

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        if text.strip() != "Thinking...":
            self.messages.append(text)

    async def delete_message(self, chat_id: int | str, message_id: int | str) -> None:
        pass

    async def send_typing(self, chat_id: int | str) -> None:
        pass


# ---------------------------------------------------------------------------
# TestResult
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    case_id: str
    response: str                   # concatenated messages
    latency_s: float
    error: str | None
    pipeline_path: str              # "direct_action" | "llm_intent" | "conversational" | "error"
    signals: list[dict] = field(default_factory=list)
    actual_action: str | None = None    # from trace: the action_type that was dispatched
    handler_name: str | None = None     # from trace: handler function that was called


# ---------------------------------------------------------------------------
# Server init (headless, no bot startup)
# ---------------------------------------------------------------------------

async def init_server():
    """Initialize PharoClaw server without starting any bots."""
    import server

    server.db_conn = server.init_db()
    server.autonomy = server.AutonomyController(server.db_conn)

    from learning import set_conn as set_learning_conn
    set_learning_conn(server.db_conn)

    row = server.db_conn.execute(
        "SELECT value FROM settings WHERE key = 'owner_chat_id'"
    ).fetchone()
    if row:
        server.OWNER_CHAT_ID = int(row[0])

    from config import LLM_BACKEND
    if LLM_BACKEND == "claude":
        import anthropic
        api_key = server.get_secret("anthropic-api-key")
        if api_key:
            server.claude = anthropic.AsyncAnthropic(api_key=api_key)

    from skills import get_registry
    get_registry()

    return server


# ---------------------------------------------------------------------------
# Case execution
# ---------------------------------------------------------------------------

async def run_case(server_mod, case: TestCase) -> TestResult:
    """Run a single test case through the message pipeline."""
    channel = InstrumentedChannel()
    ctx = MessageContext(
        channel=channel,
        chat_id="eval",
        user_id="eval",
        channel_type=ChannelType.TELEGRAM,
        incoming=IncomingMessage(
            text=case.query,
            chat_id="eval",
            user_id="eval",
            channel_type=ChannelType.TELEGRAM,
        ),
    )

    # Timeout: fast for direct_action, generous for conversational/LLM
    timeout = 15.0 if case.expected_path == "direct_action" else 60.0

    from eval.trace import capture_trace

    start = time.monotonic()
    error = None
    pipeline_path = "error"
    trace = None

    try:
        with capture_trace() as trace:
            await asyncio.wait_for(
                server_mod.handle_message_generic(ctx),
                timeout=timeout,
            )
        elapsed = time.monotonic() - start
        # Use trace for path inference instead of latency
        if trace and trace.matched_path:
            pipeline_path = trace.matched_path
        elif elapsed < 1.0 and channel.messages:
            pipeline_path = "direct_action"  # fallback
        elif elapsed < 2.0:
            pipeline_path = "llm_intent"
        else:
            pipeline_path = "conversational"
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        error = f"Timeout after {timeout:.0f}s"
    except Exception as e:
        elapsed = time.monotonic() - start
        error = f"{type(e).__name__}: {e}"

    response = "\n".join(channel.messages)

    return TestResult(
        case_id=case.id,
        response=response,
        latency_s=round(elapsed, 3),
        error=error,
        pipeline_path=pipeline_path,
        signals=[],
        actual_action=trace.matched_action if trace else None,
        handler_name=trace.handler_name if trace else None,
    )


async def run_suite(
    cases: list[TestCase],
    server_mod=None,
) -> list[TestResult]:
    """Run all cases sequentially (Ollama is single-threaded).

    Prints progress to stderr so stdout stays clean for piping.
    """
    if server_mod is None:
        print("Initializing server...", file=sys.stderr)
        server_mod = await init_server()
        print("Server ready.", file=sys.stderr)

    results: list[TestResult] = []
    total = len(cases)

    for i, case in enumerate(cases):
        result = await run_case(server_mod, case)
        results.append(result)

        status = "OK" if result.error is None else "ERR"
        print(
            f"  [{i + 1}/{total}] {status} {case.id:20s} "
            f"{result.latency_s:5.1f}s {result.pipeline_path}",
            file=sys.stderr,
        )

    return results


async def run_suite_parallel(
    cases: list[TestCase],
    server_mod=None,
    max_concurrent: int = 8,
) -> list[TestResult]:
    """Run cases with parallel execution for direct_action, sequential for LLM.

    Direct action cases (expected_path='direct_action') never hit the LLM
    and can safely run concurrently. LLM cases must be sequential because
    Ollama is single-threaded.
    """
    if server_mod is None:
        print("Initializing server...", file=sys.stderr)
        server_mod = await init_server()
        print("Server ready.", file=sys.stderr)

    # Partition cases with their original indices for re-ordering
    direct_indexed = [(i, c) for i, c in enumerate(cases) if c.expected_path == "direct_action"]
    llm_indexed = [(i, c) for i, c in enumerate(cases) if c.expected_path != "direct_action"]

    results: dict[int, TestResult] = {}
    total = len(cases)

    # Phase A: parallel direct_action cases
    if direct_indexed:
        print(f"\n  Phase A: {len(direct_indexed)} direct_action cases (parallel, max {max_concurrent})...", file=sys.stderr)
        sem = asyncio.Semaphore(max_concurrent)
        completed_a = 0

        async def run_bounded(idx: int, case: TestCase):
            nonlocal completed_a
            async with sem:
                result = await run_case(server_mod, case)
                results[idx] = result
                completed_a += 1
                status = "OK" if result.error is None else "ERR"
                print(
                    f"  [A {completed_a}/{len(direct_indexed)}] {status} {case.id:20s} "
                    f"{result.latency_s:5.1f}s {result.pipeline_path}",
                    file=sys.stderr,
                )

        await asyncio.gather(*[run_bounded(i, c) for i, c in direct_indexed])

    # Phase B: sequential LLM cases
    if llm_indexed:
        print(f"\n  Phase B: {len(llm_indexed)} LLM cases (sequential)...", file=sys.stderr)
        completed_b = 0
        for idx, case in llm_indexed:
            result = await run_case(server_mod, case)
            results[idx] = result
            completed_b += 1
            status = "OK" if result.error is None else "ERR"
            print(
                f"  [B {completed_b}/{len(llm_indexed)}] {status} {case.id:20s} "
                f"{result.latency_s:5.1f}s {result.pipeline_path}",
                file=sys.stderr,
            )

    # Merge back in original order
    return [results[i] for i in range(total)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _save_results(results: list[TestResult], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2, ensure_ascii=False)


async def _main():
    import argparse

    parser = argparse.ArgumentParser(description="PharoClaw eval runner")
    parser.add_argument("--cases", default=str(Path(__file__).parent / "fixtures" / "cases.json"))
    parser.add_argument("--limit", type=int, default=0, help="Run only first N cases")
    parser.add_argument("--out", default=str(Path(__file__).parent / "reports" / "results.json"))
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.limit > 0:
        cases = cases[: args.limit]

    print(f"Running {len(cases)} cases...", file=sys.stderr)
    results = await run_suite(cases)

    _save_results(results, args.out)
    print(f"Saved {len(results)} results to {args.out}", file=sys.stderr)

    # Summary
    errors = sum(1 for r in results if r.error)
    avg_latency = sum(r.latency_s for r in results) / len(results) if results else 0
    print(f"\nSummary: {len(results)} run, {errors} errors, avg latency {avg_latency:.2f}s",
          file=sys.stderr)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_main())
