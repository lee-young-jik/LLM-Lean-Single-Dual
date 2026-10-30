from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from typing import Iterable


@dataclass(frozen=True)
class ProofNetExample:
    id: str
    informal_statement: str
    informal_proof: str
    formal_statement: str
    allowed_imports: tuple[str, ...] = ()
    header: str = ""
    goal: str = ""
    split: str = ""
    source_format: str = "deepseek-v1.5"
    false_transform: str = ""
    false_fix: tuple[str, ...] = ()

    @property
    def problem_text(self) -> str:
        parts = [f"Statement:\n{self.informal_statement.strip()}"]
        if self.informal_proof.strip():
            parts.append(f"Informal proof:\n{self.informal_proof.strip()}")
        return "\n\n".join(parts)


def load_proofnet_jsonl(path: Path, split: str | None = None) -> list[ProofNetExample]:
    examples: list[ProofNetExample] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if split is not None and row.get("split") not in ("", None, split):
                continue
            examples.append(_example_from_row(row))
    return examples


def attach_hidden_false_keys(
    examples: Iterable[ProofNetExample], labels_path: Path
) -> list[ProofNetExample]:
    """Attach private false-transform keys without exposing them in public JSONL rows."""
    labels_by_name: dict[str, dict[str, object]] = {}
    with labels_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            name = row.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"{labels_path}:{line_number}: missing string name")
            if name in labels_by_name:
                raise ValueError(f"{labels_path}:{line_number}: duplicate name {name!r}")
            labels_by_name[name] = row

    attached: list[ProofNetExample] = []
    for example in examples:
        label = labels_by_name.get(example.id)
        if label is None:
            raise ValueError(f"{labels_path}: no private label for {example.id!r}")
        if label.get("gold") != "false":
            attached.append(example)
            continue
        false_fix = _false_fix_from_row(label)
        false_transform = str(label.get("false_transform", "") or "").strip()
        if not false_fix or not false_transform:
            raise ValueError(
                f"{labels_path}: false example {example.id!r} lacks "
                "false_transform or expected_false_fix"
            )
        attached.append(
            replace(
                example,
                false_transform=false_transform,
                false_fix=false_fix,
            )
        )
    return attached


def load_proofnet_split(dataset_root: Path, split: str) -> list[ProofNetExample]:
    deepseek_path = dataset_root / "datasets" / "proofnet.jsonl"
    if deepseek_path.exists():
        return load_proofnet_jsonl(deepseek_path, split=split)

    raise FileNotFoundError(f"Could not find DeepSeek ProofNet JSONL: {deepseek_path}")


def filter_examples(
    examples: Iterable[ProofNetExample],
    *,
    start: int = 0,
    limit: int | None = None,
    ids: set[str] | None = None,
) -> list[ProofNetExample]:
    selected = [example for example in examples if ids is None or example.id in ids]
    if start:
        selected = selected[start:]
    if limit is not None:
        selected = selected[:limit]
    return selected


def safe_example_id(example_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", example_id)


def _example_from_row(row: dict[str, str]) -> ProofNetExample:
    header = row.get("header", "")
    return ProofNetExample(
        id=row["name"],
        informal_statement=_clean_informal_prefix(row.get("informal_prefix", "")),
        informal_proof="",
        formal_statement=normalize_formal_statement(row["formal_statement"]),
        allowed_imports=_allowed_imports_from_row(row, header),
        header=header,
        goal=row.get("goal", ""),
        split=row.get("split", ""),
        source_format="deepseek-v1.5",
        false_transform=str(row.get("false_transform", "") or ""),
        false_fix=_false_fix_from_row(row),
    )


def normalize_formal_statement(formal_statement: str) -> str:
    return _replace_answer_wrappers(formal_statement)


def _replace_answer_wrappers(text: str) -> str:
    output: list[str] = []
    index = 0
    marker = "answer("
    while True:
        start = text.find(marker, index)
        if start < 0:
            output.append(text[index:])
            break
        output.append(text[index:start])
        expr_start = start + len(marker)
        expr_end = _matching_paren_index(text, expr_start - 1)
        if expr_end is None:
            output.append(text[start:])
            break
        expr = text[expr_start:expr_end].strip()
        output.append(f"({expr})")
        index = expr_end + 1
    return "".join(output)


def _matching_paren_index(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _clean_informal_prefix(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("/--") and stripped.endswith("-/"):
        stripped = stripped[3:-2]
    return stripped.strip()


def _allowed_imports_from_row(row: dict[str, str], header: str) -> tuple[str, ...]:
    raw = row.get("allowed_imports")
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return tuple(part.strip() for part in raw.splitlines() if part.strip())
        if isinstance(parsed, list):
            return tuple(str(item).strip() for item in parsed if str(item).strip())

    imports: list[str] = []
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imports.append(stripped.removeprefix("import ").strip())
    return tuple(imports)


def _false_fix_from_row(row: dict[str, str]) -> tuple[str, ...]:
    raw = (
        row.get("false_fix")
        or row.get("expected_false_fix")
        or row.get("correction_to_true")
        or row.get("expected_correction")
    )
    if raw is None:
        return ()
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return ()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return (stripped,)
        if isinstance(parsed, list):
            return tuple(str(item).strip() for item in parsed if str(item).strip())
        if isinstance(parsed, str) and parsed.strip():
            return (parsed.strip(),)
    return (str(raw).strip(),) if str(raw).strip() else ()
