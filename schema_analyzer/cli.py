from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, cast

from .defaults import (
    DEFAULT_ARANGO_URL,
    DEFAULT_ARANGO_USER,
    DEFAULT_EVAL_DATABASE,
    DEFAULT_EVAL_SAMPLE_LIMIT,
    DEFAULT_EVAL_SCALE,
    DEFAULT_TIMEOUT_MS,
    EVAL_MAX_UNHEALTHY_RUN_FRACTION,
    TOOL_ERROR_EXIT_CODE,
)
from .tool import run_tool


def _read_json(path: str | None) -> dict[str, Any]:
    if path:
        p = Path(path)
        return cast("dict[str, Any]", json.loads(p.read_text(encoding="utf-8")))
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("No input provided. Pass --request <file> or pipe JSON to stdin.")
    return cast("dict[str, Any]", json.loads(raw))


def _emit(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")


def _cmd_tool(args: argparse.Namespace) -> int:
    req = _read_json(args.request)
    resp = run_tool(req)
    _emit(json.dumps(resp, indent=2 if args.pretty else None, sort_keys=True), args.out)
    return 0 if resp.get("ok") else TOOL_ERROR_EXIT_CODE


def _url_user_password(args: argparse.Namespace) -> tuple[str, str, str]:
    """``--url`` / ``--user`` / ``--password``, each falling back to its environment variables."""
    url = args.url or os.environ.get("ARANGO_URL", os.environ.get("ARANGO_HOST", DEFAULT_ARANGO_URL))
    user = args.user or os.environ.get("ARANGO_USER", DEFAULT_ARANGO_USER)
    password = args.password or os.environ.get("ARANGO_PASS", os.environ.get("ARANGO_PASSWORD", ""))
    return url, user, password


def _build_connection(args: argparse.Namespace) -> dict[str, Any]:
    url, user, password = _url_user_password(args)
    database = args.database or os.environ.get("ARANGO_DB", "")
    if not database:
        raise SystemExit("A database is required. Pass --database or set ARANGO_DB.")
    conn: dict[str, Any] = {"url": url, "database": database, "username": user}
    # Prefer an env-var indirection if requested; otherwise inline password.
    if args.password_env_var:
        conn["passwordEnvVar"] = args.password_env_var
    else:
        conn["password"] = password
    return conn


def _cmd_connect(args: argparse.Namespace) -> int:
    """Convenience wrapper: point at a DB and emit snapshot/analysis/docs/owl
    directly, without hand-authoring v1 request JSON."""
    conn = _build_connection(args)
    pretty = getattr(args, "pretty", False)

    def _dump(obj: Any) -> str:
        return json.dumps(obj, indent=2 if pretty else None, sort_keys=True)

    if args.command == "snapshot":
        resp = run_tool({"contractVersion": "1", "operation": "snapshot", "connection": conn})
        if not resp.get("ok"):
            _emit(_dump(resp), args.out)
            return TOOL_ERROR_EXIT_CODE
        _emit(_dump(resp["result"]["snapshot"]), args.out)
        return 0

    # analyze / docs / owl all need an analysis first.
    analyze_req: dict[str, Any] = {"contractVersion": "1", "operation": "analyze", "connection": conn}
    prior_run_file = getattr(args, "prior_run", None)
    if prior_run_file:
        prior = json.loads(Path(prior_run_file).read_text(encoding="utf-8"))
        # Accept either a saved tool response ({result: {analysis: …}}) or a bare analysis.
        if (
            isinstance(prior, dict)
            and isinstance(prior.get("result"), dict)
            and isinstance(prior["result"].get("analysis"), dict)
        ):
            prior = prior["result"]["analysis"]
        analyze_req["input"] = {"previousAnalysis": prior}
    declared_file = getattr(args, "declared_references", None)
    if declared_file:
        overlay = json.loads(Path(declared_file).read_text(encoding="utf-8"))
        analyze_req.setdefault("analysisOptions", {})["declaredReferences"] = overlay
    if getattr(args, "provider", None):
        llm: dict[str, Any] = {"provider": args.provider}
        if getattr(args, "model", None):
            llm["model"] = args.model
        if getattr(args, "api_key_env_var", None):
            llm["apiKeyEnvVar"] = args.api_key_env_var
        analyze_req["llm"] = llm
    analyze_resp = run_tool(analyze_req)
    if not analyze_resp.get("ok"):
        _emit(_dump(analyze_resp), args.out)
        return TOOL_ERROR_EXIT_CODE
    analysis = analyze_resp["result"]["analysis"]

    if args.command == "analyze":
        _emit(_dump(analysis), args.out)
        return 0

    if args.command == "docs":
        resp = run_tool({"contractVersion": "1", "operation": "docs", "input": {"analysis": analysis}})
        if not resp.get("ok"):
            _emit(_dump(resp), args.out)
            return TOOL_ERROR_EXIT_CODE
        _emit(resp["result"]["markdown"], args.out)
        return 0

    if args.command == "owl":
        fmt = getattr(args, "format", "turtle")
        resp = run_tool(
            {
                "contractVersion": "1",
                "operation": "owl",
                "input": {"analysis": analysis},
                "outputOptions": {"owlFormat": fmt},
            }
        )
        if not resp.get("ok"):
            _emit(_dump(resp), args.out)
            return TOOL_ERROR_EXIT_CODE
        _emit(_dump(resp["result"]["jsonld"]) if fmt == "jsonld" else resp["result"]["turtle"], args.out)
        return 0

    raise SystemExit(f"Unknown command: {args.command}")


def _cmd_eval(args: argparse.Namespace) -> int:
    from arango import ArangoClient

    from .analyzer import AgenticSchemaAnalyzer
    from .eval import (
        build_eval_report,
        compare_reports,
        format_calibration_report,
        format_eval_summary,
        format_eval_table,
        report_regressions,
        run_eval,
        save_eval_report,
    )

    url, user, password = _url_user_password(args)
    db_name = args.database or os.environ.get("ARANGO_EVAL_DB", DEFAULT_EVAL_DATABASE)

    client = ArangoClient(hosts=url)
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(db_name):
        sys_db.delete_database(db_name, ignore_missing=True)
    sys_db.create_database(db_name)
    db = client.db(db_name, username=user, password=password)

    analyzer = AgenticSchemaAnalyzer(
        llm_provider=args.provider,
        model=args.model,
    )

    domains = args.domains.split(",") if args.domains else None

    try:
        results = run_eval(
            db,
            analyzer=analyzer,
            domains=domains,
            sample_limit=args.sample_limit,
            timeout_ms=args.timeout_ms,
            scale=args.scale,
        )
    finally:
        if args.cleanup:
            with contextlib.suppress(Exception):
                sys_db.delete_database(db_name, ignore_missing=True)

    print(format_eval_table(results))

    report = build_eval_report(results, ignore_health=args.ignore_health)
    print()
    print(format_eval_summary(report))

    print("\n--- Confidence calibration ---")
    print(format_calibration_report(report["calibration"]))

    if args.report:
        save_eval_report(results, args.report, ignore_health=args.ignore_health)
        print(f"\nReport saved to {args.report}")

    if args.baseline and args.report and Path(args.baseline).exists():
        print(f"\n--- Comparison vs {args.baseline} ---")
        print(compare_reports(args.report, args.baseline))

        if args.fail_on_regression:
            regressions = report_regressions(args.report, args.baseline)
            if regressions:
                print("\nREGRESSIONS vs baseline:")
                for r in regressions:
                    print(f"  {r}")
                return 1
            print("\nNo regressions vs baseline.")
    elif args.fail_on_regression:
        print("--fail-on-regression requires both --report and an existing --baseline", file=sys.stderr)
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arangodb-schema-analyzer", add_help=True)
    sub = parser.add_subparsers(dest="command")

    # Default tool mode (backwards compatible: no subcommand)
    parser.add_argument("--request", help="Path to request JSON. If omitted, read from stdin.")
    parser.add_argument("--out", help="Write response JSON to this path (default: stdout).")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output (indent=2).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")

    # Convenience subcommands: connect to a DB and emit a single artifact.
    def _add_connection_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--url", help="ArangoDB URL (or ARANGO_URL / ARANGO_HOST env).")
        p.add_argument("--database", help="Database name (or ARANGO_DB env).")
        p.add_argument("--user", help="Username (default: root).")
        p.add_argument("--password", help="Password (or ARANGO_PASS / ARANGO_PASSWORD env).")
        p.add_argument("--password-env-var", help="Name of env var holding the password (preferred over --password).")
        p.add_argument("--out", help="Write output to this path (default: stdout).")
        p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output (indent=2).")
        p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")

    def _add_llm_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--provider", help="LLM provider (openai|anthropic|openrouter). Omit for baseline inference.")
        p.add_argument("--model", help="LLM model name.")
        p.add_argument("--api-key-env-var", help="Name of env var holding the LLM API key.")

    snapshot_p = sub.add_parser("snapshot", help="Connect to a DB and print the physical schema snapshot JSON.")
    _add_connection_args(snapshot_p)

    analyze_p = sub.add_parser("analyze", help="Connect to a DB and print the analysis JSON.")
    _add_connection_args(analyze_p)
    _add_llm_args(analyze_p)
    analyze_p.add_argument(
        "--prior-run",
        metavar="FILE",
        help="Path to a prior analysis JSON (a saved `analyze` result, or its bare "
        "{conceptualSchema,physicalMapping,metadata}) — routes through incremental "
        "analysis so bitemporal valid time (§3.13.5) is carried back by fingerprint-continuity.",
    )
    analyze_p.add_argument(
        "--declared-references",
        metavar="FILE",
        help='Path to a declared-references overlay ({"references": [{"fromCollection", "attribute", '
        '"toCollection", "toAttribute"}]}). Each declaration is verified against the database and '
        "merged as a declared relationship; one that does not hold fails the run.",
    )

    docs_p = sub.add_parser("docs", help="Connect to a DB, analyze, and print Markdown docs.")
    _add_connection_args(docs_p)
    _add_llm_args(docs_p)

    owl_p = sub.add_parser("owl", help="Connect to a DB, analyze, and print OWL (turtle|jsonld).")
    _add_connection_args(owl_p)
    _add_llm_args(owl_p)
    owl_p.add_argument("--format", choices=["turtle", "jsonld"], default="turtle", help="OWL serialization.")

    # Eval subcommand
    eval_p = sub.add_parser("eval", help="Run evaluation against domain packs.")
    eval_p.add_argument("--url", help="ArangoDB URL (or ARANGO_URL / ARANGO_HOST env).")
    eval_p.add_argument("--user", help="ArangoDB username (default: root).")
    eval_p.add_argument("--password", help="ArangoDB password (or ARANGO_PASS / ARANGO_PASSWORD env).")
    eval_p.add_argument("--database", help="Eval database name (default: schema_analyzer_eval).")
    eval_p.add_argument("--provider", default=None, help="LLM provider name.")
    eval_p.add_argument("--model", default=None, help="LLM model name.")
    eval_p.add_argument("--domains", default=None, help="Comma-separated domain names (default: all).")
    eval_p.add_argument(
        "--sample-limit",
        type=int,
        default=DEFAULT_EVAL_SAMPLE_LIMIT,
        help="Samples per collection.",
    )
    eval_p.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="Timeout per analysis (ms).")
    eval_p.add_argument("--scale", type=int, default=DEFAULT_EVAL_SCALE, help="Scale factor for seeded data.")
    eval_p.add_argument("--report", default=None, help="Save JSON report to this path.")
    eval_p.add_argument("--baseline", default=None, help="Baseline report path for comparison.")
    eval_p.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit 1 when a gated quality metric drops vs --baseline (CI regression gate; requires --report).",
    )
    eval_p.add_argument(
        "--ignore-health",
        action="store_true",
        help=(
            f"Report headline scores even when more than {EVAL_MAX_UNHEALTHY_RUN_FRACTION * 100:g}%% of runs "
            "fell back to the baseline or errored (they are withheld by default)."
        ),
    )
    eval_p.add_argument("--no-cleanup", dest="cleanup", action="store_false", default=True, help="Keep eval database.")
    eval_p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")

    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    if args.command == "eval":
        return _cmd_eval(args)

    if args.command in ("snapshot", "analyze", "docs", "owl"):
        return _cmd_connect(args)

    return _cmd_tool(args)


if __name__ == "__main__":
    raise SystemExit(main())
