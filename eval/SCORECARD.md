# Eval Scorecard

Tracking pass rates across improvement iterations. Run `python -m eval --trend` for automated trend view.

## Baseline & History

| # | Date | Commit | Cases | Passed | Rate | Delta | Change |
|---|------|--------|-------|--------|------|-------|--------|
| 0 | 2026-03-28 | pre-fix | 50 (mixed) | 13 | 26% | — | Initial run, all failures were LLM timeouts |
| 1 | **2026-03-29** | `ebf5d3b` | **309 (direct_action)** | **116** | **37.5%** | **+11.5pp** | Baseline: direct dispatch fix, handler-aware case classification |

## Current Breakdown (Run #1 — Baseline)

### By Skill (sorted by pass rate)

| Skill | Passed/Total | Rate | Failure Mode |
|-------|-------------|------|-------------|
| readwise_review | 3/3 | 100% | — |
| cursor_extensions | 2/2 | 100% | — |
| web_search | 7/8 | 88% | 1 timeout |
| github_notifications | 7/8 | 88% | 1 timeout |
| weather | 3/4 | 75% | 1 timeout |
| readwise_highlights | 6/8 | 75% | 2 timeout |
| linkedin_profile | 5/7 | 71% | 2 timeout |
| weather_forecast | 2/3 | 67% | 1 timeout |
| macos_browser_tabs | 6/9 | 67% | 2 timeout |
| digitalocean_spend | 4/6 | 67% | 2 timeout |
| linkedin_jobs | 4/6 | 67% | 2 timeout |
| linkedin_messages | 5/8 | 62% | 3 timeout |
| imessage_recent | 8/13 | 62% | 5 timeout |
| youtube_liked | 3/5 | 60% | 2 timeout |
| appstore_ratings | 7/12 | 58% | 5 timeout |
| digitalocean_status | 4/8 | 50% | 4 timeout |
| youtube_search | 2/5 | 40% | 2 timeout |
| appstore_downloads | 4/10 | 40% | 6 timeout |
| github_prs | 4/11 | 36% | 7 timeout |
| macos_apps | 6/17 | 35% | 11 timeout |
| terminal_status | 3/9 | 33% | 6 timeout |
| cursor_terminal_status | 3/10 | 30% | 7 timeout |
| screenshot | 3/12 | 25% | 3 timeout |
| notion_search | 2/8 | 25% | handler error + timeout |
| imessage_search | 1/4 | 25% | timeout |
| browser_screenshot | 1/4 | 25% | handler error + timeout |
| apple_reminders_sync | 1/4 | 25% | timeout |
| icloud_reminder | 3/14 | 21% | timeout |
| cursor_status | 2/10 | 20% | timeout |
| macos_system_info | 3/16 | 19% | timeout |
| github_create_issue | 1/11 | 9% | timeout |
| macos_frontmost | 1/12 | 8% | timeout |
| cursor_diff | 0/3 | 0% | timeout |
| notion_create | 0/4 | 0% | timeout |
| spotlight | 0/14 | 0% | timeout |
| imessage_read | 0/9 | 0% | timeout |
| browser_extract | 0/7 | 0% | handler error |
| browser_navigate | 0/5 | 0% | handler error |

### Failure Categories

| Category | Count | % of failures |
|----------|-------|---------------|
| Timeout (handler returned False → LLM fallback > 15s) | ~155 | 80% |
| Handler error (0.0s, missing deps/keys) | ~23 | 12% |
| Routing conflict (wrong skill matched) | ~15 | 8% |

## Process

1. Pick highest-impact gap from the plan
2. Fix on a branch
3. Re-run eval: `python -m eval --cases eval/fixtures/cases_direct.json --parallel`
4. Record results in this table
5. Only merge if pass rate improved and no regressions
