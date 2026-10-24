from __future__ import annotations

import logging
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, Any, Literal

from arango import ArangoClient

if TYPE_CHECKING:
    from arango.database import StandardDatabase

from urllib.parse import urlsplit

from .analyzer import AgenticSchemaAnalyzer
from .csi import to_csi
from .defaults import (
    ALLOWED_HOSTS_ENV_VAR,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_EXPORT_TARGET,
    DEFAULT_REVIEW_THRESHOLD,
    DEFAULT_TIMEOUT_MS,
    FALLBACK_LIBRARY_VERSION,
)
from .diff import diff_analyses
from .docs import generate_schema_docs
from .errors import SchemaAnalyzerError
from .exports import build_cypher_resolution_index, export_mapping
from .owl_export import export_conceptual_model_as_jsonld, export_conceptual_model_as_owl_turtle
from .redaction import RedactionOptions
from .snapshot import fingerprint_physical_schema, snapshot_physical_schema
from .tool_contract_v1 import CONTRACT_VERSION, validate_request_v1, validate_response_v1

logger = logging.getLogger(__name__)


Operation = Literal["analyze", "snapshot", "export", "docs", "owl", "diff", "resolve", "csi"]


def _library_version() -> str:
    try:
        return pkg_version("arangodb-schema-analyzer")
    except PackageNotFoundError:
        return FALLBACK_LIBRARY_VERSION


def _env(name: str) -> str | None:
    return os.environ.get(name)


def _get_password(conn: dict[str, Any]) -> str | None:
    password = conn.get("password")
    if isinstance(password, str):
        return password
    env_var = conn.get("passwordEnvVar")
    if isinstance(env_var, str) and env_var:
        return _env(env_var)
    return None


def _get_api_key(llm: dict[str, Any] | None) -> str | None:
    if not llm:
        return None
    api_key = llm.get("apiKey")
    if isinstance(api_key, str):
        return api_key
    env_var = llm.get("apiKeyEnvVar")
    if isinstance(env_var, str) and env_var:
        return _env(env_var)
    return None


def _allowed_hosts() -> set[str] | None:
    """
    Parse ``SCHEMA_ANALYZER_ALLOWED_HOSTS`` (comma-separated host[:port])
    into a normalised set. Returns ``None`` when unset/empty so callers
    can short-circuit the check (preserving the default trust-the-caller
    behaviour for local CLI use).
    """
    raw = os.environ.get(ALLOWED_HOSTS_ENV_VAR, "").strip()
    if not raw:
        return None
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _check_url_allowed(url: str) -> None:
    allowed = _allowed_hosts()
    if allowed is None:
        return
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    netloc = parts.netloc.lower()
    if not host:
        raise SchemaAnalyzerError(
            "connection.url is missing a host component",
            code="INVALID_ARGUMENT",
        )
    candidates = {host, netloc}
    if parts.port is not None:
        candidates.add(f"{host}:{parts.port}")
    if not candidates & allowed:
        raise SchemaAnalyzerError(
            f"connection.url host {host!r} is not in the {ALLOWED_HOSTS_ENV_VAR} allowlist",
            code="INVALID_ARGUMENT",
        )


def _connect_db(conn: dict[str, Any]) -> StandardDatabase:
    url = conn.get("url")
    db_name = conn.get("database")
    username = conn.get("username") or "root"
    if not isinstance(url, str) or not url:
        raise SchemaAnalyzerError("connection.url is required", code="INVALID_ARGUMENT")
    if not isinstance(db_name, str) or not db_name:
        raise SchemaAnalyzerError("connection.database is required", code="INVALID_ARGUMENT")
    _check_url_allowed(url)
    pw = _get_password(conn)
    if pw is None:
        raise SchemaAnalyzerError("Missing ArangoDB password (password or passwordEnvVar)", code="INVALID_ARGUMENT")
    verify_tls = conn.get("verifyTls", True)
    if not isinstance(verify_tls, bool):
        verify_tls = True
    client = ArangoClient(hosts=url, verify_override=verify_tls)
    return client.db(db_name, username=username, password=pw)


def _tooling_block(
    *,
    analysis: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    include_snapshot_fingerprint: bool = True,
) -> dict[str, Any]:
    tooling: dict[str, Any] = {"contractVersion": CONTRACT_VERSION}
    if analysis and isinstance(analysis, dict):
        md = analysis.get("metadata") if isinstance(analysis.get("metadata"), dict) else {}
        if isinstance(md, dict):
            tooling["usedBaseline"] = bool(md.get("used_baseline"))
            tooling["repairAttempts"] = int(md.get("repair_attempts") or 0)
            if md.get("runId"):
                tooling["runId"] = md["runId"]
            if md.get("physicalSchemaFingerprint"):
                tooling["physicalSchemaFingerprint"] = md["physicalSchemaFingerprint"]
            if "cacheHit" in md:
                tooling["cacheHit"] = bool(md.get("cacheHit"))
    if snapshot and isinstance(snapshot, dict):
        raw_ver = snapshot.get("version")
        tooling["snapshotVersion"] = int(raw_ver or 0) if str(raw_ver or "").isdigit() else raw_ver
        if include_snapshot_fingerprint:
            tooling["snapshotFingerprint"] = fingerprint_physical_schema(snapshot, include_samples=False)
    tooling["libraryVersion"] = _library_version()
    return tooling


def _build_response(
    *,
    op: str,
    req_id: str | None,
    result: dict[str, Any],
    tooling: dict[str, Any],
) -> dict[str, Any]:
    resp: dict[str, Any] = {
        "contractVersion": CONTRACT_VERSION,
        "operation": op,
        "ok": True,
        "tooling": tooling,
        "result": result,
    }
    if req_id:
        resp["requestId"] = req_id
    errors = validate_response_v1(resp)
    if errors:
        raise SchemaAnalyzerError(f"Internal response validation failed: {errors}", code="INTERNAL_ERROR")
    return resp


def run_tool(request: dict[str, Any]) -> dict[str, Any]:
    """
    Stable v1 tool entrypoint. Accepts a request dict, returns a response dict.
    """
    errors = validate_request_v1(request)
    if errors:
        return {
            "contractVersion": CONTRACT_VERSION,
            "operation": request.get("operation"),
            "requestId": request.get("requestId"),
            "ok": False,
            "error": {"code": "INVALID_REQUEST", "message": "; ".join(errors)},
        }

    op: Operation = request["operation"]
    req_id = request.get("requestId")
    req_id = req_id if isinstance(req_id, str) and req_id else None

    try:
        logger.info("Processing operation=%s requestId=%s", op, req_id)

        if op in ("snapshot", "analyze"):
            conn = request["connection"]
            db = _connect_db(conn)

        raw_ao = request.get("analysisOptions")
        analysis_options: dict[str, Any] = raw_ao if isinstance(raw_ao, dict) else {}
        raw_oo = request.get("outputOptions")
        output_options: dict[str, Any] = raw_oo if isinstance(raw_oo, dict) else {}
        include_snapshot_fingerprint = bool(output_options.get("includeSnapshotFingerprint", True))

        if op == "snapshot":
            snap_graph_scope = analysis_options.get("graphScope")
            snapshot = snapshot_physical_schema(
                db,
                sample_limit_per_collection=int(analysis_options.get("sampleLimitPerCollection") or 0),
                include_samples_in_snapshot=bool(analysis_options.get("includeSamplesInSnapshot") or False),
                graph_scope=snap_graph_scope if isinstance(snap_graph_scope, str) and snap_graph_scope else None,
            )
            return _build_response(
                op=op,
                req_id=req_id,
                result={"snapshot": snapshot},
                tooling=_tooling_block(
                    analysis=None,
                    snapshot=snapshot,
                    include_snapshot_fingerprint=include_snapshot_fingerprint,
                ),
            )

        if op == "analyze":
            llm = request.get("llm") if isinstance(request.get("llm"), dict) else None
            raw_max_rep = analysis_options.get("maxRepairAttempts")
            max_repair = int(raw_max_rep) if raw_max_rep is not None else None
            sys_prompt = llm.get("systemPrompt") if llm else None
            if isinstance(sys_prompt, str) and not sys_prompt.strip():
                sys_prompt = None
            pv = llm.get("promptVersion") if llm else None
            prompt_version = pv if isinstance(pv, str) and pv.strip() else None
            redaction = RedactionOptions.from_dict(analysis_options.get("redaction"))
            raw_gold = analysis_options.get("goldReference")
            gold_reference = raw_gold if isinstance(raw_gold, dict) and raw_gold else None
            raw_domain_ctx = request.get("domainContext")
            domain_context = raw_domain_ctx if isinstance(raw_domain_ctx, (dict, str)) and raw_domain_ctx else None
            analyzer = AgenticSchemaAnalyzer(
                llm_provider=(llm.get("provider") if llm else None),
                api_key=_get_api_key(llm),
                model=(llm.get("model") if llm else None),
                cache=(analysis_options.get("cache") if isinstance(analysis_options.get("cache"), dict) else None),
                cache_ttl_seconds=int(analysis_options.get("cacheTtlSeconds") or DEFAULT_CACHE_TTL_SECONDS),
                review_threshold=float(analysis_options.get("reviewThreshold") or DEFAULT_REVIEW_THRESHOLD),
                system_prompt=sys_prompt,
                prompt_version=prompt_version,
                max_repair_attempts=max_repair,
                redaction=redaction if redaction.active else None,
                gold_reference=gold_reference,
                domain_context=domain_context,
            )

            include_samples = bool(analysis_options.get("includeSamplesInSnapshot") or False)
            sample_limit = int(analysis_options.get("sampleLimitPerCollection") or 0)
            raw_graph_scope = analysis_options.get("graphScope")
            graph_scope = raw_graph_scope if isinstance(raw_graph_scope, str) and raw_graph_scope else None

            snapshot = snapshot_physical_schema(
                db,
                sample_limit_per_collection=sample_limit,
                include_samples_in_snapshot=include_samples,
                graph_scope=graph_scope,
            )

            raw_strategy = analysis_options.get("entityStrategy")
            if raw_strategy is not None and raw_strategy not in ("auto", "collection", "labels"):
                return {
                    "ok": False,
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "analysisOptions.entityStrategy must be 'auto', 'collection' or "
                        f"'labels', got {raw_strategy!r}",
                    },
                }
            # Relational-pattern and taxonomy enrichment (PRD §6.2 / §6.3). Both default
            # off: candidate generation is snapshot-only, but the confirming probes are
            # cross-collection database costs and a caller should opt into paying them.
            analyzer.detect_foreign_keys = bool(analysis_options.get("detectForeignKeys") or False)
            analyzer.sample_fk_overlap = bool(analysis_options.get("sampleForeignKeyOverlap") or False)
            analyzer.discover_taxonomy = bool(analysis_options.get("discoverTaxonomy") or False)
            analyzer.measure_key_containment = bool(analysis_options.get("measureKeyContainment") or False)
            # Curator-declared attribute references (issue #27): verified against the live
            # database and merged; a declaration that does not hold fails the request with
            # INVALID_ARGUMENT (DeclaredReferenceError is a SchemaAnalyzerError).
            analyzer.declared_references = analysis_options.get("declaredReferences") or None

            # Prior-run input (PRD §3.13.5): `input.previousAnalysis` (the existing shared
            # contract field, already used by `diff`) routes `analyze` through
            # `analyze_incremental`, so a contract consumer (e.g. CDF) can obtain
            # fingerprint-continuity valid time — unreachable otherwise. Reusing the
            # pre-existing field rather than minting one keeps ASA/RSA/CDF converged.
            raw_analyze_input = request.get("input")
            prior_run = raw_analyze_input.get("previousAnalysis") if isinstance(raw_analyze_input, dict) else None
            prior_run = prior_run if isinstance(prior_run, dict) else None

            analyze_kwargs: dict[str, Any] = {
                "timeout_ms": int(analysis_options.get("timeoutMs") or DEFAULT_TIMEOUT_MS),
                "sample_limit_per_collection": sample_limit,
                "include_samples_in_snapshot": include_samples,
                "use_cache": bool(analysis_options.get("useCache", True)),
                "entity_strategy": raw_strategy or "auto",
                "_snapshot": snapshot,
            }
            if prior_run:
                analysis = analyzer.analyze_incremental(db, prior=prior_run, **analyze_kwargs)
            else:
                analysis = analyzer.analyze_physical_schema(db, **analyze_kwargs)

            analysis_dict = {
                "conceptualSchema": analysis.conceptual_schema,
                "physicalMapping": analysis.physical_mapping,
                "metadata": analysis.metadata.model_dump(by_alias=True),
            }

            result: dict[str, Any] = {"analysis": analysis_dict}
            if bool(output_options.get("includeSnapshot") or False):
                result["snapshot"] = snapshot

            return _build_response(
                op=op,
                req_id=req_id,
                result=result,
                tooling=_tooling_block(
                    analysis=analysis_dict,
                    snapshot=snapshot,
                    include_snapshot_fingerprint=include_snapshot_fingerprint,
                ),
            )

        # Transform operations
        raw_input = request.get("input")
        input_obj = raw_input if isinstance(raw_input, dict) else {}
        analysis_in = input_obj.get("analysis")
        if not isinstance(analysis_in, dict):
            raise SchemaAnalyzerError("input.analysis is required", code="INVALID_ARGUMENT")

        if op == "diff":
            previous = input_obj.get("previousAnalysis")
            if not isinstance(previous, dict):
                raise SchemaAnalyzerError("input.previousAnalysis is required for diff", code="INVALID_ARGUMENT")
            return _build_response(
                op=op,
                req_id=req_id,
                result={"diff": diff_analyses(previous, analysis_in)},
                tooling=_tooling_block(analysis=analysis_in, snapshot=None),
            )

        if op == "resolve":
            return _build_response(
                op=op,
                req_id=req_id,
                result={"resolution": build_cypher_resolution_index(analysis_in)},
                tooling=_tooling_block(analysis=analysis_in, snapshot=None),
            )

        if op == "export":
            raw_target = output_options.get("exportTarget")
            target = raw_target if isinstance(raw_target, str) else DEFAULT_EXPORT_TARGET
            out = export_mapping(analysis_in, target=target)
            return _build_response(
                op=op,
                req_id=req_id,
                result={"export": out},
                tooling=_tooling_block(analysis=analysis_in, snapshot=None),
            )

        if op == "docs":
            md = generate_schema_docs(analysis_in)
            return _build_response(
                op=op,
                req_id=req_id,
                result={"markdown": md},
                tooling=_tooling_block(analysis=analysis_in, snapshot=None),
            )

        if op == "csi":
            return _build_response(
                op=op,
                req_id=req_id,
                result={"csi": to_csi(analysis_in)},
                tooling=_tooling_block(analysis=analysis_in, snapshot=None),
            )

        if op == "owl":
            owl_format = output_options.get("owlFormat")
            if owl_format == "jsonld":
                result_block: dict[str, Any] = {"jsonld": export_conceptual_model_as_jsonld(analysis_in)}
            else:
                result_block = {"turtle": export_conceptual_model_as_owl_turtle(analysis_in)}
            return _build_response(
                op=op,
                req_id=req_id,
                result=result_block,
                tooling=_tooling_block(analysis=analysis_in, snapshot=None),
            )

        raise SchemaAnalyzerError(f"Unsupported operation: {op}", code="INVALID_ARGUMENT")

    except SchemaAnalyzerError as e:
        logger.warning("Operation %s failed: [%s] %s", op, e.code, e)
        resp: dict[str, Any] = {
            "contractVersion": CONTRACT_VERSION,
            "operation": op,
            "ok": False,
            "error": {"code": e.code or "ERROR", "message": str(e)},
        }
        if req_id:
            resp["requestId"] = req_id
        return resp
    except Exception:
        logger.exception("Unexpected error during operation %s", op)
        resp = {
            "contractVersion": CONTRACT_VERSION,
            "operation": op,
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred. Check server logs for details.",
            },
        }
        if req_id:
            resp["requestId"] = req_id
        return resp
