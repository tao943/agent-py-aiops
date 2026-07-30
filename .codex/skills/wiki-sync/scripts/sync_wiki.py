#!/usr/bin/env python3
"""Synchronize OpenSpec changes into deterministic VitePress WIKI pages."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[4]
OPENSPEC_CHANGES = REPO_ROOT / "openspec" / "changes"
MAIN_SPECS = REPO_ROOT / "openspec" / "specs"
DOCS_ROOT = REPO_ROOT / "docs"
CHANGES_DOCS = DOCS_ROOT / "changes"
ACTIVE_DOCS = CHANGES_DOCS / "active"
ARCHIVE_DOCS = CHANGES_DOCS / "archive"
CONFIG_PATH = DOCS_ROOT / ".vitepress" / "config.mts"
INDEX_PATH = CHANGES_DOCS / "index.md"

INCLUDE_RE = re.compile(r"<!--@include:\s*(.+?)-->")
REQUIREMENT_RE = re.compile(r"^### Requirement:\s*(.+?)\s*$", re.MULTILINE)
OPERATION_RE = re.compile(
    r"^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements\s*$", re.MULTILINE
)
INDEX_LINK_RE = re.compile(r"^- \[[^]]+\]\((/changes/(?:active|archive)/[^)]+/)\)$")
CONFIG_LINK_RE = re.compile(
    r'^\s*\{ text: .+?, link: "(/changes/(?:active|archive)/.+?/)" \},?$', re.MULTILINE
)

# Historical requirements occasionally changed names or moved to a replacement
# requirement without an explicit OpenSpec RENAMED section. Keep those evolutions
# auditable and require their current targets to exist instead of silently skipping
# synchronization validation.
REQUIREMENT_EVOLUTIONS: dict[tuple[str, str], tuple[str, str]] = {
    (
        "project-foundation",
        "Developer documentation and environment examples",
    ): (
        "project-foundation",
        "Developer documentation and project configuration",
    ),
    ("docker-compose-startup", "Application image"): (
        "docker-compose-startup",
        "Unified compose startup",
    ),
    ("docker-compose-startup", "Compose documentation and environment"): (
        "local-development-operations-guide",
        "Local-first developer startup guide",
    ),
    (
        "local-development-operations-guide",
        "Tracked configuration and operations reference",
    ): (
        "local-development-operations-guide",
        "Local configuration and operations reference",
    ),
    ("diagnosis-case-knowledge", "AIOps report retention action"): (
        "diagnosis-case-knowledge",
        "Save completed diagnostic as user-scoped knowledge case",
    ),
}


class WikiSyncError(RuntimeError):
    """Actionable synchronization failure."""


@dataclass(frozen=True, slots=True)
class ChangePage:
    directory_name: str
    title: str
    status: Literal["active", "archived"]
    created_date: str
    archived_date: str | None
    source_dir: Path
    output_dir: Path

    @property
    def link(self) -> str:
        return f"/changes/{'active' if self.status == 'active' else 'archive'}/{self.directory_name}/"


@dataclass(frozen=True, slots=True)
class SpecOperation:
    archive_name: str
    capability: str
    operation: Literal["ADDED", "MODIFIED", "REMOVED"]
    requirement: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("active", "archive", "all"))
    parser.add_argument("name", nargs="?")
    parser.add_argument(
        "--allow-unsynced",
        action="store_true",
        help="Skip archive delta-spec synchronization failures only after explicit user approval.",
    )
    args = parser.parse_args()
    if args.mode != "all" and not args.name:
        parser.error("active and archive modes require a change name")
    return args


def main() -> int:
    args = parse_args()
    try:
        validate_layout()
        selected = validate_selected_change(args.mode, args.name)
        active_pages = collect_active_pages()
        archive_pages = collect_archive_pages()
        validate_archive_specs(archive_pages, allow_unsynced=args.allow_unsynced)
        synchronize_pages(active_pages, archive_pages)
        validate_outputs(active_pages, archive_pages)
    except WikiSyncError as exc:
        print(f"wiki-sync failed: {exc}", file=sys.stderr)
        return 1

    print(
        "wiki-sync complete: "
        f"{len(active_pages)} active, {len(archive_pages)} archived, "
        f"{sum(include_count(page) for page in [*active_pages, *archive_pages])} includes verified"
    )
    if selected:
        print(f"selected change: {selected}")
    print("navigation verified: docs/changes/index.md == docs/.vitepress/config.mts")
    return 0


def validate_layout() -> None:
    link = DOCS_ROOT / "openspec"
    if not link.is_symlink() or link.resolve() != (REPO_ROOT / "openspec").resolve():
        raise WikiSyncError("docs/openspec must be a symlink to ../openspec")
    if not OPENSPEC_CHANGES.is_dir():
        raise WikiSyncError("openspec/changes does not exist")


def validate_selected_change(mode: str, name: str | None) -> str | None:
    if mode == "all":
        return None
    if mode == "active":
        source = OPENSPEC_CHANGES / str(name)
        if not source.is_dir() or source.name == "archive":
            raise WikiSyncError(f"active change not found: {name}")
        return source.name
    return resolve_archive_name(str(name))


def resolve_archive_name(name: str) -> str:
    archive_root = OPENSPEC_CHANGES / "archive"
    exact = archive_root / name
    if exact.is_dir():
        return exact.name
    matches = [path.name for path in archive_root.iterdir() if path.is_dir() and path.name.endswith(f"-{name}")]
    if len(matches) != 1:
        detail = "none" if not matches else ", ".join(sorted(matches))
        raise WikiSyncError(f"archive name '{name}' matched {detail}")
    return matches[0]


def collect_active_pages() -> list[ChangePage]:
    pages: list[ChangePage] = []
    for source in sorted(OPENSPEC_CHANGES.iterdir(), key=lambda item: item.name):
        if not source.is_dir() or source.name == "archive":
            continue
        created = existing_created_date(ACTIVE_DOCS / source.name / "index.md") or date.today().isoformat()
        pages.append(
            ChangePage(
                directory_name=source.name,
                title=source.name,
                status="active",
                created_date=created,
                archived_date=None,
                source_dir=source,
                output_dir=ACTIVE_DOCS / source.name,
            )
        )
    return pages


def collect_archive_pages() -> list[ChangePage]:
    pages: list[ChangePage] = []
    archive_root = OPENSPEC_CHANGES / "archive"
    for source in sorted(archive_root.iterdir(), key=lambda item: item.name, reverse=True):
        if not source.is_dir():
            continue
        archived, title = split_archive_name(source.name)
        existing_archive = ARCHIVE_DOCS / source.name / "index.md"
        existing_active = ACTIVE_DOCS / title / "index.md"
        created = (
            existing_created_date(existing_archive)
            or existing_created_date(existing_active)
            or archived
        )
        pages.append(
            ChangePage(
                directory_name=source.name,
                title=title,
                status="archived",
                created_date=created,
                archived_date=archived,
                source_dir=source,
                output_dir=ARCHIVE_DOCS / source.name,
            )
        )
    return pages


def split_archive_name(name: str) -> tuple[str, str]:
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-(.+)", name)
    if not match:
        raise WikiSyncError(f"archive directory lacks date prefix: {name}")
    return match.group(1), match.group(2)


def existing_created_date(page: Path) -> str | None:
    if not page.is_file():
        return None
    match = re.search(r"^createdDate:\s*(\d{4}-\d{2}-\d{2})\s*$", page.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def validate_archive_specs(pages: list[ChangePage], *, allow_unsynced: bool) -> None:
    errors: list[str] = []
    operations: list[SpecOperation] = []
    for page in reversed(pages):
        specs = sorted((page.source_dir / "specs").glob("*/spec.md"))
        if not specs:
            errors.append(f"{page.directory_name}: delta specs are missing")
            continue
        for spec in specs:
            operations.extend(parse_spec_operations(page.directory_name, spec.parent.name, spec))

    latest: dict[tuple[str, str], SpecOperation] = {}
    for operation in operations:
        latest[(operation.capability, operation.requirement)] = operation

    verified_evolutions = 0
    for (capability, requirement), operation in latest.items():
        main_spec = MAIN_SPECS / capability / "spec.md"
        if not main_spec.is_file():
            errors.append(f"{operation.archive_name}: main spec missing for {capability}")
            continue
        main_requirements = current_requirements(capability)
        exists = requirement in main_requirements
        if operation.operation in {"ADDED", "MODIFIED"} and not exists:
            target = REQUIREMENT_EVOLUTIONS.get((capability, requirement))
            if target is not None and target[1] in current_requirements(target[0]):
                verified_evolutions += 1
            else:
                errors.append(
                    f"{operation.archive_name}: requirement '{requirement}' is not synchronized to {capability}"
                )
        if operation.operation == "REMOVED" and exists:
            errors.append(
                f"{operation.archive_name}: removed requirement '{requirement}' still exists in {capability}"
            )

    if errors and not allow_unsynced:
        preview = "\n  - ".join(errors[:20])
        suffix = f"\n  ... {len(errors) - 20} more" if len(errors) > 20 else ""
        raise WikiSyncError(
            "archive delta-spec validation failed; synchronize specs or rerun only after explicit approval "
            f"with --allow-unsynced:\n  - {preview}{suffix}"
        )
    if errors:
        print(f"warning: explicitly skipped {len(errors)} archive spec synchronization issue(s)")
    print(
        "archive delta specs verified: "
        f"{len(pages)} archives, {len(latest)} latest requirements, "
        f"{verified_evolutions} explicit evolutions"
    )


def current_requirements(capability: str) -> set[str]:
    path = MAIN_SPECS / capability / "spec.md"
    if not path.is_file():
        return set()
    return {
        normalize_requirement_name(requirement)
        for requirement in REQUIREMENT_RE.findall(path.read_text(encoding="utf-8"))
    }


def normalize_requirement_name(requirement: str) -> str:
    normalized = requirement.strip().strip("`").strip()
    normalized = re.sub(r"^###\s*(?:Requirement|需求)\s*[:：]\s*", "", normalized)
    return normalized.strip()


def parse_spec_operations(archive_name: str, capability: str, path: Path) -> list[SpecOperation]:
    text = path.read_text(encoding="utf-8")
    headings = list(OPERATION_RE.finditer(text))
    operations: list[SpecOperation] = []
    for index, heading in enumerate(headings):
        operation = heading.group(1)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.end() : end]
        if operation == "RENAMED":
            for source, target in re.findall(r"FROM:\s*`?### Requirement:\s*(.+?)`?\s*\n-?\s*TO:\s*`?### Requirement:\s*(.+?)`?\s*(?:\n|$)", section):
                operations.append(
                    SpecOperation(
                        archive_name,
                        capability,
                        "REMOVED",
                        normalize_requirement_name(source),
                    )
                )
                operations.append(
                    SpecOperation(
                        archive_name,
                        capability,
                        "ADDED",
                        normalize_requirement_name(target),
                    )
                )
            continue
        for requirement in REQUIREMENT_RE.findall(section):
            operations.append(
                SpecOperation(
                    archive_name,
                    capability,
                    operation,  # type: ignore[arg-type]
                    normalize_requirement_name(requirement),
                )
            )
    return operations


def synchronize_pages(active: list[ChangePage], archived: list[ChangePage]) -> None:
    ACTIVE_DOCS.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DOCS.mkdir(parents=True, exist_ok=True)
    remove_stale_directories(ACTIVE_DOCS, {page.directory_name for page in active})
    remove_stale_directories(ARCHIVE_DOCS, {page.directory_name for page in archived})
    for page in [*active, *archived]:
        page.output_dir.mkdir(parents=True, exist_ok=True)
        (page.output_dir / "index.md").write_text(render_page(page), encoding="utf-8")
    INDEX_PATH.write_text(render_index(active, archived), encoding="utf-8")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(render_config(active, archived), encoding="utf-8")


def remove_stale_directories(root: Path, expected: set[str]) -> None:
    for path in root.iterdir():
        if path.is_dir() and path.name not in expected:
            shutil.rmtree(path)


def render_page(page: ChangePage) -> str:
    frontmatter = ["---", f"title: {page.title}", f"status: {page.status}", f"createdDate: {page.created_date}"]
    if page.archived_date:
        frontmatter.append(f"archivedDate: {page.archived_date}")
    frontmatter.append("---")
    source_prefix = (
        f"../../../openspec/changes/{page.directory_name}"
        if page.status == "active"
        else f"../../../openspec/changes/archive/{page.directory_name}"
    )
    sections = [
        *frontmatter,
        "",
        f"# {page.title}",
        "",
        f"> 状态：{'进行中' if page.status == 'active' else '已归档'}",
        "",
        "## 提案",
        "",
        f"<!--@include: {source_prefix}/proposal.md-->",
        "",
        "## 设计",
        "",
        f"<!--@include: {source_prefix}/design.md-->",
        "",
        "## 任务",
        "",
        f"<!--@include: {source_prefix}/tasks.md-->",
    ]
    specs = sorted((page.source_dir / "specs").glob("*/spec.md"))
    if specs:
        sections.extend(["", "## 规格变更"])
        for spec in specs:
            sections.extend(
                [
                    "",
                    f"### {spec.parent.name}",
                    "",
                    f"<!--@include: {source_prefix}/specs/{spec.parent.name}/spec.md-->",
                ]
            )
    return "\n".join(sections) + "\n"


def render_index(active: list[ChangePage], archived: list[ChangePage]) -> str:
    lines = ["---", "title: OpenSpec 变更", "---", "", "# OpenSpec 变更", "", "## 进行中", ""]
    lines.extend(render_link_list(active, empty="当前没有进行中的变更。"))
    lines.extend(["", "## 已归档", ""])
    lines.extend(render_link_list(archived, empty="当前没有已归档的变更。"))
    return "\n".join(lines) + "\n"


def render_link_list(pages: list[ChangePage], *, empty: str) -> list[str]:
    if not pages:
        return [empty]
    return [f"- [{page.title}]({page.link})" for page in pages]


def render_config(active: list[ChangePage], archived: list[ChangePage]) -> str:
    active_items = render_sidebar_items(active)
    archive_items = render_sidebar_items(archived)
    template = '''// Generated by .codex/skills/wiki-sync/scripts/sync_wiki.py
import { defineConfig } from "vitepress";

const activeItems = [
__ACTIVE_ITEMS__
];

const archivedItems = [
__ARCHIVE_ITEMS__
];

export default defineConfig({
  lang: "zh-CN",
  title: "Super AI WIKI",
  description: "Super AI 项目文档与 OpenSpec 变更记录",
  cleanUrls: true,
  themeConfig: {
    nav: [
      { text: "首页", link: "/" },
      { text: "变更 WIKI", link: "/changes/" }
    ],
    sidebar: {
      "/changes/": [
        { text: "总览", items: [{ text: "变更索引", link: "/changes/" }] },
        { text: "进行中", collapsed: false, items: activeItems },
        { text: "已归档", collapsed: true, items: archivedItems }
      ]
    },
    search: { provider: "local" },
    outline: { level: [2, 3], label: "本页目录" },
    docFooter: { prev: "上一页", next: "下一页" },
    sidebarMenuLabel: "目录",
    returnToTopLabel: "返回顶部"
  }
});
'''
    return template.replace("__ACTIVE_ITEMS__", active_items).replace(
        "__ARCHIVE_ITEMS__", archive_items
    )


def render_sidebar_items(pages: list[ChangePage]) -> str:
    return "\n".join(
        f"  {{ text: {json.dumps(page.title, ensure_ascii=False)}, link: {json.dumps(page.link)} }},"
        for page in pages
    )


def validate_outputs(active: list[ChangePage], archived: list[ChangePage]) -> None:
    expected_active = {page.directory_name for page in active}
    expected_archive = {page.directory_name for page in archived}
    actual_active = {path.name for path in ACTIVE_DOCS.iterdir() if path.is_dir()}
    actual_archive = {path.name for path in ARCHIVE_DOCS.iterdir() if path.is_dir()}
    if actual_active != expected_active or actual_archive != expected_archive:
        raise WikiSyncError("WIKI page directories do not match OpenSpec change directories")

    for page in [*active, *archived]:
        validate_page(page)

    expected_links = [page.link for page in [*active, *archived]]
    index_links = [
        match.group(1)
        for line in INDEX_PATH.read_text(encoding="utf-8").splitlines()
        if (match := INDEX_LINK_RE.match(line))
    ]
    config_links = CONFIG_LINK_RE.findall(CONFIG_PATH.read_text(encoding="utf-8"))
    if index_links != expected_links or config_links != expected_links:
        raise WikiSyncError("docs/changes/index.md and VitePress Sidebar ordering diverged")


def validate_page(page: ChangePage) -> None:
    output = page.output_dir / "index.md"
    text = output.read_text(encoding="utf-8")
    includes = INCLUDE_RE.findall(text)
    expected_prefix = (
        f"../../../openspec/changes/{page.directory_name}/"
        if page.status == "active"
        else f"../../../openspec/changes/archive/{page.directory_name}/"
    )
    if len(includes) < 3:
        raise WikiSyncError(f"{output}: proposal/design/tasks includes are incomplete")
    for include in includes:
        if not include.startswith(expected_prefix):
            raise WikiSyncError(f"{output}: invalid include path {include}")
        target = (output.parent / include).resolve()
        if not target.is_file():
            raise WikiSyncError(f"{output}: include target missing: {include}")
    required_frontmatter = [
        f"title: {page.title}",
        f"status: {page.status}",
        f"createdDate: {page.created_date}",
    ]
    if page.archived_date:
        required_frontmatter.append(f"archivedDate: {page.archived_date}")
    if any(field not in text for field in required_frontmatter):
        raise WikiSyncError(f"{output}: frontmatter does not match change state")


def include_count(page: ChangePage) -> int:
    output = page.output_dir / "index.md"
    return len(INCLUDE_RE.findall(output.read_text(encoding="utf-8")))


if __name__ == "__main__":
    raise SystemExit(main())
