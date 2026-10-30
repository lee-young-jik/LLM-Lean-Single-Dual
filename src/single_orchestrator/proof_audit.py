from __future__ import annotations

from dataclasses import dataclass
import re


DECL_KEYWORD_RE = re.compile(
    r"^(theorem|lemma|axiom|opaque|def|abbrev|instance|constant|example)\b"
)
PROHIBITED_RE = re.compile(
    r"\b(sorry|admit|axiom|opaque|unsafe|constant)\b"
)
THEOREM_HEADER_RE = re.compile(r"(?s)\btheorem\s+.*?:=\s*by\b")


@dataclass(frozen=True)
class ProofAudit:
    success: bool
    rationale: str = "Proof audit passed."


def audit_lean_proof(lean_source: str, formal_statement: str = "") -> ProofAudit:
    """Static post-build guard for generated Lean proofs.

    Lake proves type correctness. This guard rejects proofs that only passed by
    changing the target surface, adding top-level facts, or using placeholder-like
    escape hatches. It intentionally stays conservative and syntactic.
    """
    source = lean_source.strip()
    if not source:
        return ProofAudit(False, "Proof audit failed: generated Lean source is empty.")

    # Local-hypothesis tactics (assumption, False.elim) are sound when Lean
    # checks them against the fixed target; they are not proof escapes.
    prohibited = PROHIBITED_RE.search(source)
    if prohibited:
        return ProofAudit(
            False,
            f"Proof audit failed: prohibited proof escape `{prohibited.group(0)}` was used.",
        )

    declarations = _top_level_declarations(source)
    if formal_statement.strip():
        theorem_headers = [
            declaration
            for declaration in declarations
            if declaration.lstrip().startswith("theorem ")
        ]
        if len(theorem_headers) != 1:
            return ProofAudit(
                False,
                "Proof audit failed: expected exactly one top-level theorem declaration.",
            )
        expected = _normalize_statement(formal_statement)
        actual = _normalize_statement(theorem_headers[0])
        if actual != expected:
            return ProofAudit(
                False,
                "Proof audit failed: generated theorem statement differs from the target statement.",
            )

    extra_declarations = [
        declaration
        for declaration in declarations
        if not declaration.lstrip().startswith("theorem ")
    ]
    if extra_declarations:
        return ProofAudit(
            False,
            "Proof audit failed: generated source introduced extra top-level declarations.",
        )

    return ProofAudit(True)


def _top_level_declarations(source: str) -> list[str]:
    declarations: list[str] = []
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        index += 1
        if not stripped or stripped.startswith("--"):
            continue
        if line[:1].isspace():
            continue
        if stripped.startswith("import ") or stripped.startswith("open "):
            continue
        if DECL_KEYWORD_RE.match(stripped):
            if not (stripped.startswith("theorem ") or stripped.startswith("lemma ")):
                declarations.append(stripped)
                continue
            declaration_lines = [line]
            while ":= by" not in "\n".join(declaration_lines) and index < len(lines):
                declaration_lines.append(lines[index])
                index += 1
            declarations.append(_header_only("\n".join(declaration_lines).strip()))
    return declarations


def _header_only(declaration: str) -> str:
    match = THEOREM_HEADER_RE.search(declaration)
    if match:
        return match.group(0)
    return declaration


def _normalize_statement(statement: str) -> str:
    text = statement.strip()
    match = THEOREM_HEADER_RE.search(text)
    if match:
        text = match.group(0)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
