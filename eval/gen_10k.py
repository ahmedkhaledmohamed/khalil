"""One-shot script to generate 10K test cases using Claude API.

Usage:
    python -m eval.gen_10k              # generate and save
    python -m eval.gen_10k --count      # just show counts
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import keyring
import anthropic

from eval.case_gen import generate_all_cases, _FIXTURES_DIR
from eval.cases import save_cases


def make_ask_llm_fn():
    """Create a synchronous ask_llm function using Anthropic API."""
    api_key = keyring.get_password("khalil-assistant", "anthropic-api-key")
    if not api_key:
        raise RuntimeError("No Anthropic API key found in keyring")

    client = anthropic.Anthropic(api_key=api_key)

    def ask_llm(prompt: str) -> str:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    return ask_llm


if __name__ == "__main__":
    force = "--force" in sys.argv
    count_only = "--count" in sys.argv

    ask_llm_fn = make_ask_llm_fn()
    cases = generate_all_cases(ask_llm_fn=ask_llm_fn, force_llm=force)

    if count_only:
        print(f"Total: {len(cases)} test cases")
        source_count: dict[str, int] = {}
        for c in cases:
            source_count[c.source] = source_count.get(c.source, 0) + 1
        for source, count in sorted(source_count.items(), key=lambda x: -x[1]):
            print(f"  {source}: {count}")

        # Path distribution
        path_count: dict[str, int] = {}
        for c in cases:
            path_count[c.expected_path] = path_count.get(c.expected_path, 0) + 1
        print("\nBy expected_path:")
        for path, count in sorted(path_count.items(), key=lambda x: -x[1]):
            print(f"  {path}: {count}")
    else:
        out_path = str(_FIXTURES_DIR / "cases_10k.json")
        save_cases(cases, out_path)
        print(f"Saved {len(cases)} cases to {out_path}")
