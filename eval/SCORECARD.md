# Eval Scorecard

Tracking pass rates across improvement iterations. Run `python -m eval --trend` for automated trend view.

## Baseline & History

| # | Date | Commit | Cases | Passed | Rate | Delta | Change |
|---|------|--------|-------|--------|------|-------|--------|
| 0 | 2026-03-28 | pre-fix | 50 (mixed) | 13 | 26% | — | Initial run, all failures were LLM timeouts |
| 1 | 2026-03-29 | `ebf5d3b` | 309 (direct_action) | 116 | 37.5% | +11.5pp | Baseline: direct dispatch fix, handler-aware case classification |
| 2 | **2026-03-29** | pending | **323 (direct_action)** | **214** | **66.3%** | **+28.8pp** | Query generator fix, NameError fix, routing priority fix |

## What Changed (Run #2)

Three fixes:
1. **Query generator** (`eval/cases.py`): Fixed `_positive_query_from_regex` — was producing junk like `":appsprocessesprograms"`, now produces `"what apps are running"`. Recursive alternation expansion + `.*` → space.
2. **NameError** (`server.py`): Removed dead `update` param from `_execute_with_retry` — was crashing 35 cases.
3. **Routing priority** (`server.py`): Skill-pattern handlers now run BEFORE `_try_direct_shell_intent`. Prevents shell from stealing queries meant for richer skill handlers (e.g., `ps` vs AppleScript for "what apps are running").
4. **LLM-param classification** (`eval/cases.py`): browser_*, cursor_diff reclassified as `llm_intent` (need URL/file params).
5. **Spotlight fallback** (`actions/macos.py`): Extract search term from user_query when LLM `query` param missing.

## Current Breakdown (Run #2)

### Failure Categories

| Category | Count | % of failures |
|----------|-------|---------------|
| Timeout (handler slow or falls to LLM) | 89 | 82% |
| Routing wrong (latency/content check) | 20 | 18% |
| Handler error | 0 | 0% |

### By Skill (sorted by pass rate)

| Skill | Passed/Total | Rate | Notes |
|-------|-------------|------|-------|
| digitalocean_status | 10/10 | 100% | |
| digitalocean_spend | 8/8 | 100% | |
| linkedin_profile | 7/7 | 100% | |
| readwise_review | 2/2 | 100% | |
| cursor_extensions | 2/2 | 100% | |
| appstore_ratings | 11/12 | 92% | |
| imessage_recent | 12/13 | 92% | |
| github_notifications | 7/8 | 88% | |
| linkedin_messages | 7/8 | 88% | |
| macos_frontmost | 12/14 | 86% | |
| macos_browser_tabs | 11/13 | 85% | |
| readwise_highlights | 5/6 | 83% | |
| appstore_downloads | 8/10 | 80% | |
| youtube_search | 4/5 | 80% | |
| youtube_liked | 4/5 | 80% | |
| imessage_read | 10/13 | 77% | |
| macos_apps | 16/21 | 76% | was 52% |
| apple_reminders_sync | 3/4 | 75% | |
| terminal_status | 8/11 | 73% | |
| cursor_terminal_status | 7/10 | 70% | |
| macos_system_info | 11/16 | 69% | was 31% |
| linkedin_jobs | 4/6 | 67% | |
| github_create_issue | 7/11 | 64% | |
| notion_search | 5/8 | 62% | |
| cursor_status | 7/12 | 58% | |
| github_prs | 8/15 | 53% | |
| screenshot | 6/12 | 50% | was 0% |
| icloud_reminder | 7/18 | 39% | |
| weather_forecast | 1/3 | 33% | no API keys |
| notion_create | 1/4 | 25% | |
| web_search | 2/10 | 20% | generic queries |
| spotlight | 1/14 | 7% | needs search term |
| weather | 0/4 | 0% | no KHALIL_WEATHER_LAT |
| imessage_search | 0/8 | 0% | timeout |

## Next Targets

1. **icloud_reminder** (7/18, 39%): 11 failures — likely routing conflict with apple_reminders
2. **Timeout cases** (89 total): Most are LLM fallback. Could increase timeout or fix handler to not return False
3. **web_search**: Improve generated queries to include actual search terms

## Process

1. Pick highest-impact gap from the plan
2. Fix on a branch
3. Re-run eval: `python -m eval --cases eval/fixtures/cases_direct.json --parallel`
4. Record results in this table
5. Only merge if pass rate improved and no regressions
