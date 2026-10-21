"""Gap tools: the few things Claude can't do itself (a natural voice, generated images…).

Each tool is a folder in `src/nebula/gap_tools/<id>/` with:
- `manifest.yaml`: what it does, when NOT to use it, the gap it fills (required), kind,
  read/write, who can use it, owner and status (draft | live | retired)
- `handler.py`: `async def run(person, args: dict) -> dict` returning
  {"text": "...", "files": [{"key": ..., "filename": ..., "mime": ...}]}

Only `live` tools are loaded, and only if their manifest states the gap. Adding a tool
means adding a folder; retiring one means setting status: retired, which removes it
from Claude's tool list completely.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import logging
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

REQUIRED_GAP_FIELDS = ("claude_cannot", "checked_how", "recheck_by")


@dataclass
class GapTool:
    id: str
    description: str
    when_not_to_use: str
    gap: dict
    kind: str                       # api | source_action | sandbox
    access: str                     # read | write
    long_running: bool
    groups: list[str]
    owner: str
    status: str
    input_schema: dict
    path: Path
    handler: Any = field(default=None, repr=False)

    @property
    def full_description(self) -> str:
        return f"{self.description.strip()}\n\nWhen not to use: {self.when_not_to_use.strip()}"

    @property
    def recheck_overdue(self) -> bool:
        try:
            return dt.date.fromisoformat(str(self.gap.get("recheck_by"))) < dt.date.today()
        except ValueError:
            return True


class ManifestError(ValueError):
    pass


def load_manifest(folder: Path) -> GapTool:
    m = yaml.safe_load((folder / "manifest.yaml").read_text()) or {}
    missing = [f for f in ("id", "description", "when_not_to_use", "gap", "kind", "access", "status") if not m.get(f)]
    if missing:
        raise ManifestError(f"{folder.name}: manifest missing {', '.join(missing)}")
    gap = m["gap"] or {}
    gap_missing = [f for f in REQUIRED_GAP_FIELDS if not gap.get(f)]
    if gap_missing:
        raise ManifestError(f"{folder.name}: gap must state {', '.join(gap_missing)}")
    if m["kind"] not in ("api", "source_action", "sandbox"):
        raise ManifestError(f"{folder.name}: kind must be api, source_action or sandbox")
    if m["status"] not in ("draft", "live", "retired"):
        raise ManifestError(f"{folder.name}: status must be draft, live or retired")
    return GapTool(
        id=m["id"], description=m["description"], when_not_to_use=m["when_not_to_use"], gap=gap, kind=m["kind"],
        access=m["access"], long_running=bool(m.get("long_running")), groups=list(m.get("groups") or ["all_rl"]),
        owner=m.get("owner", ""), status=m["status"],
        input_schema=m.get("input_schema") or {"type": "object", "properties": {}}, path=folder,
    )


def _load_handler(tool: GapTool) -> Any:
    spec = importlib.util.spec_from_file_location(f"nebula_gap_{tool.id}", tool.path / "handler.py")
    if spec is None or spec.loader is None:
        raise ManifestError(f"{tool.id}: handler.py not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run"):
        raise ManifestError(f"{tool.id}: handler.py must define async def run(person, args)")
    return mod.run


def discover(root: Path | None = None) -> list[GapTool]:
    """All live gap tools with valid manifests. Broken manifests are logged and skipped."""
    base = root or Path(str(resources.files("nebula") / "gap_tools"))
    out: list[GapTool] = []
    if not base.exists():
        return out
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        try:
            t = load_manifest(folder)
            if t.status != "live":
                continue
            t.handler = _load_handler(t)
            if t.recheck_overdue:
                log.warning("gap tool %s: re-check that Claude still can't do this (was due %s)",
                            t.id, t.gap.get("recheck_by"))
            out.append(t)
        except Exception as e:
            log.error("skipping gap tool %s: %s", folder.name, e)
    return out


def result_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
