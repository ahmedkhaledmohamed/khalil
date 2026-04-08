"""Generate the Khalil eval dashboard HTML from report data.

Reads all eval reports from eval/reports/, computes trends, and writes
an interactive dashboard to docs/dashboard.html.

Usage:
    python eval/generate_dashboard.py              # generate from all reports
    python eval/generate_dashboard.py --open       # generate and open in browser
"""

import glob
import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "eval" / "reports"
OUTPUT_PATH = ROOT / "docs" / "dashboard.html"

# ---------------------------------------------------------------------------
# Curated run history (maps to SCORECARD.md run table)
# Reports are noisy — some are partial runs, API key issues, etc.
# This list picks the canonical report per iteration.
# ---------------------------------------------------------------------------

CURATED_RUNS = [
    # (run_number, report_file, label, note)
    (0, "20260329_002626.json", "Baseline", "Initial run, all failures were LLM timeouts"),
    (1, "20260329_125445.json", "Direct dispatch", "Pattern match → handler, skip LLM call"),
    (2, "20260329_134306.json", "Query gen + routing", "Regex → NL generator, routing priority fix"),
    (3, "20260329_152446.json", "Case quality", "Keyword cases → llm_intent, natural templates"),
    (4, "20260329_200302.json", "Eval infra", "Latency threshold, runner timeout, screenshot fix"),
    (5, "20260329_202327.json", "Param extraction", "Search term extraction, weather exclusion"),
    (6, "20260405_030054.json", "Production validation", "Full-scope 2,458 cases"),
    (7, "20260408_162754.json", "Direct-action + generated", "Full generated suite via Taskforce"),
    (8, "20260408_181456.json", "Frozen baseline (Taskforce)", "851 frozen cases, apples-to-apples"),
]


def load_report(filename: str) -> dict | None:
    path = REPORTS_DIR / filename
    if not path.exists():
        return None
    with open(path) as f:
        data = json.load(f)
    # Older reports are lists of TestResult dicts
    if isinstance(data, list):
        passed = sum(1 for r in data if r.get("error") is None)
        return {
            "timestamp": filename.replace(".json", ""),
            "total_cases": len(data),
            "passed": passed,
            "failed": len(data) - passed,
            "pass_rate": passed / len(data) if data else 0,
            "by_skill": {},
            "gaps": [],
        }
    return data


def load_all_reports() -> list[dict]:
    """Load all reports sorted by timestamp."""
    reports = []
    for f in sorted(glob.glob(str(REPORTS_DIR / "2026*.json"))):
        with open(f) as fh:
            data = json.load(fh)
        name = os.path.basename(f)
        if isinstance(data, list):
            passed = sum(1 for r in data if r.get("error") is None)
            reports.append({
                "file": name,
                "timestamp": name.replace(".json", ""),
                "total_cases": len(data),
                "passed": passed,
                "pass_rate": passed / len(data) if data else 0,
            })
        else:
            reports.append({
                "file": name,
                "timestamp": data.get("timestamp", name.replace(".json", "")),
                "total_cases": data.get("total_cases", 0),
                "passed": data.get("passed", 0),
                "pass_rate": data.get("pass_rate", 0),
                "by_skill": data.get("by_skill", {}),
                "gaps": data.get("gaps", []),
            })
    return reports


def format_ts(ts: str) -> str:
    """20260408_181456 → Apr 8"""
    try:
        dt = datetime.strptime(ts[:8], "%Y%m%d")
        return dt.strftime("%b %d")
    except ValueError:
        return ts[:8]


def generate_html() -> str:
    all_reports = load_all_reports()
    curated = []
    for run_num, filename, label, note in CURATED_RUNS:
        report = load_report(filename)
        if report:
            curated.append({
                "run": run_num,
                "label": label,
                "note": note,
                "file": filename,
                **report,
            })

    latest = curated[-1] if curated else None
    prev = curated[-2] if len(curated) >= 2 else None

    # Compute deltas
    deltas = []
    for i, run in enumerate(curated):
        if i == 0:
            deltas.append(run["pass_rate"] * 100)
        else:
            deltas.append((run["pass_rate"] - curated[i - 1]["pass_rate"]) * 100)

    # Latest skill breakdown
    skills = []
    if latest and latest.get("by_skill"):
        for name, info in sorted(latest["by_skill"].items()):
            total = info.get("total", 0)
            passed = info.get("passed", 0)
            if total > 0:
                skills.append((name, passed, total))
        skills.sort(key=lambda x: (-x[1] / x[2] if x[2] else 0, x[0]))

    # Gap categories from latest
    gap_cats = {}
    if latest and latest.get("gaps"):
        for gap in latest["gaps"]:
            cat = gap.get("category", "unknown")
            gap_cats[cat] = gap_cats.get(cat, 0) + 1

    # All reports timeline
    timeline_data = []
    for r in all_reports:
        timeline_data.append({
            "ts": format_ts(r["timestamp"]),
            "rate": round(r["pass_rate"] * 100, 1),
            "cases": r["total_cases"],
            "passed": r["passed"],
            "file": r["file"],
        })

    # --- Build HTML ---

    # Chart points for curated runs
    chart_w, chart_h = 760, 220
    margin_l, margin_r, margin_t, margin_b = 60, 40, 30, 40
    plot_w = chart_w - margin_l - margin_r
    plot_h = chart_h - margin_t - margin_b

    n = len(curated)
    if n < 2:
        spacing = plot_w
    else:
        spacing = plot_w / (n - 1)

    chart_points = []
    for i, run in enumerate(curated):
        x = margin_l + i * spacing
        pct = run["pass_rate"] * 100
        y = margin_t + plot_h - (pct / 100 * plot_h)
        chart_points.append((x, y, pct, run["run"], deltas[i]))

    # SVG elements
    area_path = "M" + " L".join(f"{x:.0f},{y:.0f}" for x, y, *_ in chart_points)
    area_path += f" L{chart_points[-1][0]:.0f},{margin_t + plot_h} L{chart_points[0][0]:.0f},{margin_t + plot_h} Z"

    line_points = " ".join(f"{x:.0f},{y:.0f}" for x, y, *_ in chart_points)

    dots_svg = ""
    for i, (x, y, pct, run_num, delta) in enumerate(chart_points):
        is_last = i == len(chart_points) - 1
        r = 8 if is_last else 6
        fill = "var(--green)" if is_last else ""
        stroke = 'style="fill:var(--green);stroke-width:3"' if is_last else ""
        label_color = ' style="fill:var(--green)"' if is_last else ""
        delta_str = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"
        if i == 0:
            delta_str = ""
        dots_svg += f"""
        <circle cx="{x:.0f}" cy="{y:.0f}" r="{r}" class="chart-dot" {stroke}/>
        <text x="{x:.0f}" y="{y - 12:.0f}" class="chart-label"{label_color}>{pct:.1f}%</text>
        <text x="{x:.0f}" y="{y + 20:.0f}" class="chart-sublabel">#{run_num} {delta_str}</text>"""

    # Waterfall bars
    waterfall_html = ""
    for i, run in enumerate(curated):
        pct = run["pass_rate"] * 100
        delta = deltas[i]
        delta_str = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"
        if pct >= 85:
            color = "var(--green)"
        elif pct >= 60:
            color = "var(--blue)"
        elif pct >= 40:
            color = "var(--yellow)"
        else:
            color = "var(--red)"
        delta_color = "var(--green)" if delta >= 0 else "var(--red)"
        waterfall_html += f"""
      <div class="waterfall-bar">
        <div class="iter">#{run['run']}</div>
        <div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%;background:{color}">{pct:.1f}%</div></div>
        <div class="delta-label" style="color:{delta_color}">{delta_str}</div>
      </div>"""

    # Iteration details
    iter_html = ""
    for i, run in enumerate(curated):
        pct = run["pass_rate"] * 100
        border = ' style="border-left-color:var(--green)"' if i == len(curated) - 1 else ""
        num_style = ' style="background:var(--green)"' if i == len(curated) - 1 else ""
        iter_html += f"""
    <div class="iteration"{border}>
      <div class="head">
        <span class="num"{num_style}>#{run['run']}</span>
        <span class="title">{run['label']}</span>
        <span class="rate-badge">{pct:.1f}%</span>
        <span class="case-count">{run['total_cases']} cases</span>
      </div>
      <ul><li>{run['note']}</li></ul>
    </div>"""

    # Skills JSON for JS
    skills_json = json.dumps(skills)

    # Gap donut data
    total_gaps = sum(gap_cats.values()) if gap_cats else 1
    gap_items = sorted(gap_cats.items(), key=lambda x: -x[1])
    gap_colors = ["var(--yellow)", "var(--purple)", "var(--red)", "var(--blue)", "var(--green)"]

    donut_svg = ""
    donut_legend = ""
    offset = 78.5  # starting offset
    circumference = 314.16  # 2 * pi * 50
    for i, (cat, count) in enumerate(gap_items[:5]):
        frac = count / total_gaps
        arc = frac * circumference
        color = gap_colors[i % len(gap_colors)]
        donut_svg += f'<circle cx="60" cy="60" r="50" fill="none" stroke="{color}" stroke-width="16" stroke-dasharray="{arc:.1f} {circumference - arc:.1f}" stroke-dashoffset="{offset:.1f}" stroke-linecap="round"/>\n'
        offset -= arc
        donut_legend += f'<div class="item"><div class="dot" style="background:{color}"></div> {cat} ({count})</div>\n'

    # All runs timeline table
    timeline_rows = ""
    for r in all_reports:
        rate = r["pass_rate"] * 100
        if rate >= 85:
            badge_color = "var(--green)"
        elif rate >= 60:
            badge_color = "var(--blue)"
        elif rate >= 40:
            badge_color = "var(--yellow)"
        else:
            badge_color = "var(--red)"
        timeline_rows += f"""
        <tr>
          <td class="mono">{format_ts(r['timestamp'])}</td>
          <td class="mono">{r['file']}</td>
          <td>{r['total_cases']}</td>
          <td>{r['passed']}</td>
          <td><span style="color:{badge_color};font-weight:600">{rate:.1f}%</span></td>
        </tr>"""

    # Latest stats
    latest_rate = latest["pass_rate"] * 100 if latest else 0
    latest_passed = latest["passed"] if latest else 0
    latest_total = latest["total_cases"] if latest else 0
    latest_failed = latest["failed"] if latest else 0

    if prev:
        overall_delta = latest_rate - (prev["pass_rate"] * 100)
        delta_display = f"+{overall_delta:.1f}" if overall_delta >= 0 else f"{overall_delta:.1f}"
        delta_color = "var(--green)" if overall_delta >= 0 else "var(--red)"
    else:
        delta_display = ""
        delta_color = "var(--green)"

    first_rate = curated[0]["pass_rate"] * 100 if curated else 0
    total_improvement = latest_rate - first_rate
    total_delta = f"+{total_improvement:.1f}pp" if total_improvement >= 0 else f"{total_improvement:.1f}pp"

    last_updated = format_ts(latest["timestamp"]) if latest else "N/A"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Khalil Eval Dashboard</title>
<style>
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --green: #3fb950;
    --green-dim: #238636;
    --yellow: #d29922;
    --red: #f85149;
    --blue: #58a6ff;
    --purple: #bc8cff;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 2rem;
    max-width: 1200px;
    margin: 0 auto;
  }}
  h1 {{ font-size: 1.8rem; font-weight: 600; margin-bottom: 0.25rem; }}
  .subtitle {{ color: var(--text-muted); font-size: 0.95rem; margin-bottom: 2rem; }}
  .grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.25rem;
    margin-bottom: 1.25rem;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
  }}
  .card.full {{ grid-column: 1 / -1; }}
  .card h2 {{
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    margin-bottom: 1rem;
  }}
  .big-number {{
    font-size: 3.5rem;
    font-weight: 700;
    line-height: 1;
  }}
  .big-number .unit {{ font-size: 1.5rem; color: var(--text-muted); }}
  .big-number .delta {{
    font-size: 1rem;
    vertical-align: super;
    margin-left: 0.5rem;
  }}
  .stat-row {{
    display: flex;
    gap: 2rem;
    margin-top: 1rem;
  }}
  .stat-item .label {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; }}
  .stat-item .value {{ font-size: 1.4rem; font-weight: 600; }}
  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1rem;
    margin-bottom: 1.25rem;
  }}
  .kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    text-align: center;
  }}
  .kpi .kpi-value {{ font-size: 1.8rem; font-weight: 700; }}
  .kpi .kpi-label {{ font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 0.25rem; }}
  .kpi .kpi-delta {{ font-size: 0.8rem; font-weight: 600; margin-top: 0.15rem; }}

  /* Chart */
  .chart-container {{ position: relative; height: 220px; margin-top: 0.5rem; }}
  .chart-svg {{ width: 100%; height: 100%; }}
  .chart-line {{ fill: none; stroke: var(--blue); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }}
  .chart-area {{ fill: url(#areaGrad); }}
  .chart-dot {{ fill: var(--blue); stroke: var(--surface); stroke-width: 3; }}
  .chart-label {{ fill: var(--text); font-size: 11px; font-weight: 600; text-anchor: middle; }}
  .chart-sublabel {{ fill: var(--text-muted); font-size: 9px; text-anchor: middle; }}
  .chart-grid {{ stroke: var(--border); stroke-width: 0.5; stroke-dasharray: 4 4; }}
  .chart-grid-label {{ fill: var(--text-muted); font-size: 10px; text-anchor: end; }}

  /* Waterfall */
  .waterfall-bar {{ display: flex; align-items: center; margin-bottom: 0.5rem; gap: 0.75rem; }}
  .waterfall-bar .iter {{ width: 24px; font-size: 0.8rem; color: var(--text-muted); text-align: right; flex-shrink: 0; }}
  .waterfall-bar .bar-track {{ flex: 1; height: 26px; background: rgba(88,166,255,0.08); border-radius: 6px; overflow: hidden; }}
  .waterfall-bar .bar-fill {{ height: 100%; border-radius: 6px; display: flex; align-items: center; padding-left: 10px; font-size: 0.75rem; font-weight: 600; color: #fff; white-space: nowrap; transition: width 1s ease; }}
  .waterfall-bar .delta-label {{ width: 55px; text-align: right; font-size: 0.85rem; font-weight: 600; flex-shrink: 0; }}

  /* Iterations */
  .iteration {{ border-left: 3px solid var(--border); padding: 0.6rem 0 0.6rem 1rem; margin-bottom: 0.4rem; }}
  .iteration:last-child {{ margin-bottom: 0; }}
  .iteration .head {{ display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}
  .iteration .num {{ background: var(--blue); color: #000; font-size: 0.7rem; font-weight: 700; padding: 2px 7px; border-radius: 10px; }}
  .iteration .title {{ font-weight: 600; font-size: 0.9rem; }}
  .iteration .rate-badge {{ font-size: 0.75rem; padding: 2px 8px; border-radius: 10px; background: rgba(63,185,80,0.15); color: var(--green); font-weight: 600; }}
  .iteration .case-count {{ font-size: 0.7rem; color: var(--text-muted); }}
  .iteration ul {{ list-style: none; padding: 0; }}
  .iteration li {{ font-size: 0.8rem; color: var(--text-muted); padding: 2px 0 2px 1rem; position: relative; }}
  .iteration li::before {{ content: ">"; position: absolute; left: 0; color: var(--border); }}

  /* Skill table */
  .skill-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.3rem 1.5rem; }}
  .skill-row {{ display: flex; align-items: center; gap: 0.5rem; padding: 3px 0; }}
  .skill-row .status {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
  .skill-row .status.perfect {{ background: var(--green); }}
  .skill-row .status.good {{ background: var(--yellow); }}
  .skill-row .status.mid {{ background: #d29922; opacity: 0.6; }}
  .skill-row .status.bad {{ background: var(--red); }}
  .skill-row .name {{ flex: 1; font-size: 0.78rem; font-family: 'SF Mono', 'Fira Code', monospace; }}
  .skill-row .bar-bg {{ width: 80px; height: 6px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; flex-shrink: 0; }}
  .skill-row .bar-fg {{ height: 100%; border-radius: 3px; transition: width 0.8s ease; }}
  .skill-row .pct {{ width: 42px; text-align: right; font-size: 0.78rem; font-weight: 600; flex-shrink: 0; }}
  .skill-row .count {{ width: 40px; text-align: right; font-size: 0.72rem; color: var(--text-muted); flex-shrink: 0; }}

  /* Donut */
  .donut-container {{ display: flex; align-items: center; gap: 2rem; justify-content: center; }}
  .donut-legend .item {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; font-size: 0.82rem; }}
  .donut-legend .dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}

  /* Timeline table */
  .timeline-table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
  .timeline-table th {{ text-align: left; color: var(--text-muted); font-weight: 500; padding: 0.5rem; border-bottom: 1px solid var(--border); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; }}
  .timeline-table td {{ padding: 0.4rem 0.5rem; border-bottom: 1px solid rgba(48,54,61,0.4); }}
  .timeline-table .mono {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.75rem; }}

  .tab-bar {{ display: flex; gap: 0.5rem; margin-bottom: 1rem; }}
  .tab {{ padding: 0.4rem 1rem; border-radius: 8px; font-size: 0.8rem; cursor: pointer; border: 1px solid var(--border); background: transparent; color: var(--text-muted); }}
  .tab.active {{ background: var(--blue); color: #000; border-color: var(--blue); font-weight: 600; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}

  .gen-note {{ color: var(--text-muted); font-size: 0.75rem; text-align: right; margin-top: 1.5rem; }}

  @media (max-width: 700px) {{
    .grid {{ grid-template-columns: 1fr; }}
    .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .skill-grid {{ grid-template-columns: 1fr; }}
    body {{ padding: 1rem; }}
  }}
</style>
</head>
<body>

<h1>Khalil Eval Dashboard</h1>
<p class="subtitle">Performance tracking across {len(curated)} iterations &mdash; {len(all_reports)} eval runs &mdash; Last updated: {last_updated}, 2026</p>

<!-- KPI strip -->
<div class="kpi-grid">
  <div class="kpi">
    <div class="kpi-value" style="color:{"var(--green)" if latest_rate >= 85 else "var(--yellow)" if latest_rate >= 60 else "var(--red)"}">{latest_rate:.1f}%</div>
    <div class="kpi-label">Current Pass Rate</div>
    <div class="kpi-delta" style="color:{delta_color}">{delta_display}pp vs prev</div>
  </div>
  <div class="kpi">
    <div class="kpi-value" style="color:var(--blue)">{total_delta}</div>
    <div class="kpi-label">Total Improvement</div>
    <div class="kpi-delta" style="color:var(--text-muted)">from {first_rate:.0f}% baseline</div>
  </div>
  <div class="kpi">
    <div class="kpi-value">{latest_passed}</div>
    <div class="kpi-label">Cases Passed</div>
    <div class="kpi-delta" style="color:var(--text-muted)">of {latest_total}</div>
  </div>
  <div class="kpi">
    <div class="kpi-value" style="color:var(--red)">{latest_failed}</div>
    <div class="kpi-label">Failures</div>
    <div class="kpi-delta" style="color:var(--text-muted)">{len(gap_cats)} categories</div>
  </div>
</div>

<!-- Chart + Failure donut -->
<div class="grid">
  <div class="card full">
    <h2>Pass Rate Over Iterations</h2>
    <div class="chart-container">
      <svg class="chart-svg" viewBox="0 0 {chart_w} {chart_h}" preserveAspectRatio="xMidYMid meet">
        <defs>
          <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--blue)" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="var(--blue)" stop-opacity="0.02"/>
          </linearGradient>
        </defs>
        <line x1="{margin_l}" y1="{margin_t}" x2="{chart_w - margin_r}" y2="{margin_t}" class="chart-grid"/>
        <line x1="{margin_l}" y1="{margin_t + plot_h * 0.25:.0f}" x2="{chart_w - margin_r}" y2="{margin_t + plot_h * 0.25:.0f}" class="chart-grid"/>
        <line x1="{margin_l}" y1="{margin_t + plot_h * 0.5:.0f}" x2="{chart_w - margin_r}" y2="{margin_t + plot_h * 0.5:.0f}" class="chart-grid"/>
        <line x1="{margin_l}" y1="{margin_t + plot_h * 0.75:.0f}" x2="{chart_w - margin_r}" y2="{margin_t + plot_h * 0.75:.0f}" class="chart-grid"/>
        <text x="{margin_l - 5}" y="{margin_t + 4}" class="chart-grid-label">100%</text>
        <text x="{margin_l - 5}" y="{margin_t + plot_h * 0.25 + 4:.0f}" class="chart-grid-label">75%</text>
        <text x="{margin_l - 5}" y="{margin_t + plot_h * 0.5 + 4:.0f}" class="chart-grid-label">50%</text>
        <text x="{margin_l - 5}" y="{margin_t + plot_h * 0.75 + 4:.0f}" class="chart-grid-label">25%</text>
        <path class="chart-area" d="{area_path}"/>
        <polyline class="chart-line" points="{line_points}"/>
        {dots_svg}
      </svg>
    </div>
  </div>
</div>

<!-- Waterfall + Iterations -->
<div class="grid">
  <div class="card">
    <h2>Cumulative Progress</h2>
    {waterfall_html}
  </div>
  <div class="card">
    <h2>What Changed Per Iteration</h2>
    {iter_html}
  </div>
</div>

<!-- Skills + Failures -->
<div class="grid">
  <div class="card">
    <h2>Skill Pass Rates (Run #{curated[-1]['run'] if curated else '?'})</h2>
    <div class="skill-grid" id="skillGrid"></div>
  </div>
  <div class="card">
    <h2>Failure Categories</h2>
    <div class="donut-container">
      <svg width="120" height="120" viewBox="0 0 120 120">
        <circle cx="60" cy="60" r="50" fill="none" stroke="var(--border)" stroke-width="16"/>
        {donut_svg}
        <text x="60" y="56" text-anchor="middle" fill="var(--text)" font-size="22" font-weight="700">{latest_failed}</text>
        <text x="60" y="72" text-anchor="middle" fill="var(--text-muted)" font-size="10">failures</text>
      </svg>
      <div class="donut-legend">
        {donut_legend}
      </div>
    </div>
  </div>
</div>

<!-- All runs -->
<div class="grid">
  <div class="card full">
    <h2>All Eval Runs ({len(all_reports)} reports)</h2>
    <table class="timeline-table">
      <thead>
        <tr><th>Date</th><th>Report</th><th>Cases</th><th>Passed</th><th>Rate</th></tr>
      </thead>
      <tbody>
        {timeline_rows}
      </tbody>
    </table>
  </div>
</div>

<p class="gen-note">Auto-generated by <code>eval/generate_dashboard.py</code> from {len(all_reports)} reports in eval/reports/</p>

<script>
const skills = {skills_json};
const grid = document.getElementById("skillGrid");
skills.forEach(([name, passed, total]) => {{
  const pct = total > 0 ? (passed / total * 100) : 0;
  let statusClass, color;
  if (pct === 100) {{ statusClass = "perfect"; color = "var(--green)"; }}
  else if (pct >= 75) {{ statusClass = "good"; color = "var(--yellow)"; }}
  else if (pct >= 50) {{ statusClass = "mid"; color = "var(--yellow)"; }}
  else {{ statusClass = "bad"; color = "var(--red)"; }}
  const row = document.createElement("div");
  row.className = "skill-row";
  row.innerHTML = `
    <div class="status ${{statusClass}}"></div>
    <div class="name">${{name}}</div>
    <div class="bar-bg"><div class="bar-fg" style="width:${{pct}}%;background:${{color}}"></div></div>
    <div class="pct" style="color:${{color}}">${{pct.toFixed(0)}}%</div>
    <div class="count">${{passed}}/${{total}}</div>
  `;
  grid.appendChild(row);
}});
</script>

</body>
</html>"""


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate Khalil eval dashboard")
    parser.add_argument("--open", action="store_true", help="Open in browser after generating")
    parser.add_argument("--out", default=str(OUTPUT_PATH), help="Output path")
    args = parser.parse_args()

    html = generate_html()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"Dashboard generated: {out_path}")

    if args.open:
        webbrowser.open(f"file://{out_path.resolve()}")


if __name__ == "__main__":
    main()
