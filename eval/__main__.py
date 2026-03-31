import sys

if "--shell-safety" in sys.argv:
    from eval.shell_safety_runner import run_shell_safety_tests
    run_shell_safety_tests()
elif "--commands" in sys.argv:
    import asyncio, logging
    logging.basicConfig(level=logging.WARNING)
    from eval.command_runner import run_command_tests
    asyncio.run(run_command_tests())
elif "--conversational" in sys.argv:
    # Filter cases.json to conversational only, then run pipeline
    import json
    from pathlib import Path
    fixtures = Path(__file__).parent / "fixtures" / "cases.json"
    if not fixtures.exists():
        print(f"No cases.json found. Run: python -m eval --generate")
        sys.exit(1)
    cases = json.load(open(fixtures))
    conv = [c for c in cases if c.get("expected_path") == "conversational"]
    tmp = Path(__file__).parent / "fixtures" / "cases_conversational.json"
    tmp.write_text(json.dumps(conv, indent=2))
    print(f"Filtered {len(conv)} conversational cases -> {tmp}")
    # Inject into argv for the main pipeline
    sys.argv = [sys.argv[0], "--cases", str(tmp)]
    from eval import main
    main()
elif "--coverage" in sys.argv:
    from eval import _print_coverage
    _print_coverage()
else:
    from eval import main
    main()
