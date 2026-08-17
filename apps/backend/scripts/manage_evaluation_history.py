"""Manage the canonical local and PostgreSQL evaluation history."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.history import artifact_checksum
from super_ai.evaluation.history_import import import_history, reconcile_history
from super_ai.evaluation.persistence import EvaluationRepository
from super_ai.evaluation.summary import build_history_summary, write_history_summary
from super_ai.memory.database import create_memory_engine, create_memory_session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage durable evaluation history.")
    commands = parser.add_subparsers(dest="command", required=True)
    imported = commands.add_parser("import-history")
    imported.add_argument("--source", type=Path, action="append", required=True)
    for name in ("reconcile", "summarize", "audit"):
        commands.add_parser(name)
    for command in commands.choices.values():
        command.add_argument("--config", type=Path)
    return parser


async def run_command(arguments: argparse.Namespace) -> tuple[dict[str, object], int]:
    archive = EvaluationArchive.from_config(config_path=arguments.config)
    if arguments.command == "audit":
        envelopes = list(archive.iter_envelopes())
        checksums = {item.run_id: artifact_checksum(item) for item in envelopes}
        return {"artifacts": len(envelopes), "checksums": len(checksums)}, 0

    config_path = str(arguments.config) if arguments.config is not None else None
    engine = create_memory_engine(config_path=config_path)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    try:
        if arguments.command == "import-history":
            report = await import_history(
                sources=arguments.source,
                archive=archive,
                repository=repository,
            )
            payload = _report_payload(report)
            return payload, 1 if report.rejected or report.conflicts else 0
        if arguments.command == "reconcile":
            report = await reconcile_history(archive=archive, repository=repository)
            payload = _report_payload(report)
            return payload, 1 if report.conflicts else 0
        rows = await repository.list_runs_with_results()
        database_checksums = {run.run_id: run.artifact_checksum for run, _result in rows}
        summary = build_history_summary(
            list(archive.iter_envelopes()),
            database_checksums=database_checksums,
        )
        write_history_summary(archive.root, summary)
        return {
            "total": summary.counts.total,
            "databasePending": summary.counts.database_pending,
            "conflicts": summary.reconciliation.conflicts,
        }, 1 if summary.reconciliation.conflicts else 0
    finally:
        await engine.dispose()


def _report_payload(report: object) -> dict[str, object]:
    return {
        key: getattr(report, key)
        for key in (
            "imported", "duplicates", "reconstructed", "rejected", "conflicts",
            "database_pending",
        )
    }


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        payload, exit_code = asyncio.run(run_command(arguments))
    except Exception:
        print(json.dumps({"error": "evaluation_history_infrastructure_failure"}))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
