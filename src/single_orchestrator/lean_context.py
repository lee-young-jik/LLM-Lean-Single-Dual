from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Iterable


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'.]*")
UNAVAILABLE_IMPORT_RE = re.compile(r"imports unavailable module `([^`]+)`")

COMMON_MINIF2F_OPEN_SCOPES = (
    "open scoped Real",
    "open scoped Nat",
    "open scoped Topology",
    "open scoped Polynomial",
)

KNOWN_IMPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "Mathlib.Tactic.Nlinarith": ("Mathlib.Tactic.Linarith",),
    "Mathlib.Tactic.RingNF": ("Mathlib.Tactic.Ring.RingNF", "Mathlib.Tactic.Ring"),
    "Mathlib.FieldTheory.Minpoly": ("Mathlib.FieldTheory.Minpoly.Basic",),
    "Mathlib.Data.Rat.Cast": (
        "Mathlib.Data.Rat.Cast.Defs",
        "Mathlib.Data.Rat.Cast.Lemmas",
        "Mathlib.Data.Rat.Cast.Order",
    ),
    "Mathlib.Data.Nat.Digits": (
        "Mathlib.Data.Nat.Digits.Defs",
        "Mathlib.Data.Nat.Digits.Lemmas",
    ),
    "Mathlib.Data.Nat.Parity": (
        "Mathlib.Tactic.NormNum.Parity",
        "Mathlib.Algebra.Ring.Int.Parity",
    ),
    "Mathlib.Algebra.BigOperators.Basic": (
        "Mathlib.Algebra.BigOperators.Group.Finset.Basic",
        "Mathlib.Algebra.BigOperators.Ring.Finset",
    ),
}

TOKEN_IMPORT_HINTS: dict[str, tuple[str, ...]] = {
    "Real": ("Mathlib.Data.Real.Basic",),
    "sqrt": ("Mathlib.Analysis.SpecialFunctions.Sqrt",),
    "log": ("Mathlib.Analysis.SpecialFunctions.Log.Basic",),
    "logb": ("Mathlib.Analysis.SpecialFunctions.Log.Basic",),
    "Complex": ("Mathlib.Data.Complex.Basic",),
    "Nat": ("Mathlib.Data.Nat.Basic",),
    "gcd": ("Mathlib.Data.Nat.GCD.Basic",),
    "lcm": ("Mathlib.Data.Nat.GCD.Basic",),
    "Finset": ("Mathlib.Data.Finset.Basic",),
    "sum": ("Mathlib.Algebra.BigOperators.Group.Finset.Basic",),
    "prod": ("Mathlib.Algebra.BigOperators.Group.Finset.Basic",),
    "Polynomial": ("Mathlib.RingTheory.Polynomial.Basic",),
    "ZMod": ("Mathlib.Data.ZMod.Basic",),
    "Int": ("Mathlib.Data.Int.Basic",),
    "Rat": ("Mathlib.Data.Rat.Basic",),
    "Set": ("Mathlib.Data.Set.Basic",),
    "Icc": ("Mathlib.Order.Interval.Finset.Basic",),
    "Ico": ("Mathlib.Order.Interval.Set.Basic",),
    "Ioo": ("Mathlib.Order.Interval.Set.Basic",),
    "linarith": ("Mathlib.Tactic.Linarith",),
    "norm_num": ("Mathlib.Tactic.NormNum",),
    "ring": ("Mathlib.Tactic.Ring",),
    "ring_nf": ("Mathlib.Tactic.Ring.RingNF",),
    "omega": ("Mathlib.Tactic.Omega",),
}


def retrieve_lean_context(
    project_root: Path,
    *,
    formal_statement: str = "",
    informal: str = "",
    previous_feedback: object = None,
    max_imports: int = 12,
) -> str:
    """Return environment-only Lean hints without reading proof solutions."""

    available = available_modules(project_root)
    if not available:
        return ""

    text = "\n".join(
        part for part in (formal_statement, informal, str(previous_feedback or "")) if part
    )
    avoid = _unavailable_imports(text)
    candidates: list[str] = []

    if "MiniF2F.ProblemImports" in available:
        candidates.append("MiniF2F.ProblemImports")

    for bad_import in avoid:
        candidates.extend(KNOWN_IMPORT_ALIASES.get(bad_import, ()))

    tokens = set(TOKEN_RE.findall(text))
    for token in sorted(tokens):
        short = token.rsplit(".", 1)[-1]
        candidates.extend(TOKEN_IMPORT_HINTS.get(token, ()))
        candidates.extend(TOKEN_IMPORT_HINTS.get(short, ()))

    imports = _dedupe(
        module
        for module in candidates
        if module in available and module not in {"Mathlib", "Mathlib.Tactic"}
    )[:max_imports]

    lines = [
        "Lean environment guardrails:",
        "- Use only import modules that exist in the local Lean project.",
        "- Do not invent import paths. If an import is not listed here, prefer no extra import.",
        "- The retrieved context is not a proof and contains no solved miniF2F proof.",
    ]
    if imports:
        lines.append("Verified import candidates:")
        lines.extend(f"- import {module}" for module in imports)
    if "MiniF2F.ProblemImports" in imports:
        lines.append("Verified common open scopes:")
        lines.extend(f"- {line}" for line in COMMON_MINIF2F_OPEN_SCOPES)
    if avoid:
        lines.append("Unavailable imports already seen; do not use:")
        lines.extend(f"- import {module}" for module in avoid)
    return "\n".join(lines)


@lru_cache(maxsize=8)
def available_modules(project_root: Path) -> frozenset[str]:
    root = project_root.resolve()
    modules: set[str] = set()
    source_roots = [root]
    packages = root / ".lake" / "packages"
    if packages.exists():
        source_roots.extend(path for path in packages.iterdir() if path.is_dir())
    for source_root in source_roots:
        for path in source_root.rglob("*.lean"):
            if any(part in {".git", "Scratch"} for part in path.parts):
                continue
            try:
                relative = path.relative_to(source_root).with_suffix("")
            except ValueError:
                continue
            modules.add(".".join(relative.parts))

    olean_root = root / ".lake" / "build" / "lib" / "lean"
    if olean_root.exists():
        for path in olean_root.rglob("*.olean"):
            if "Scratch" in path.parts:
                continue
            modules.add(".".join(path.relative_to(olean_root).with_suffix("").parts))
    return frozenset(modules)


def _unavailable_imports(text: str) -> list[str]:
    return _dedupe(UNAVAILABLE_IMPORT_RE.findall(text))


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output
