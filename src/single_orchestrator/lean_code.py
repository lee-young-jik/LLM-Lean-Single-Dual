from __future__ import annotations

import re
import textwrap


LEAN_FENCE_RE = re.compile(r"```(?:lean|lean4)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
DECL_RE = re.compile(r"(?s)\b(?:theorem|lemma)\s+[A-Za-z_][A-Za-z0-9_']*.*?:=\s*by\b")
LEAN_BODY_START_RE = re.compile(
    r"^(?:"
    r"·|--|/-|"
    r"exact\b|refine\b|apply\b|intro\b|intros\b|have\b|show\b|"
    r"let\b|"
    r"calc\b|constructor\b|cases\b|rcases\b|induction\b|rw\b|rewrite\b|"
    r"subst\b|rfl\b|decide\b|native_decide\b|norm_num\b|simp\b|omega\b|"
    r"ring\b|ring_nf\b|linarith\b|nlinarith\b|aesop\b|tauto\b|"
    r"interval_cases\b|fin_cases\b|all_goals\b"
    r")"
)
SIMPLE_TACTIC_LINE_RE = re.compile(
    r"^(?:"
    r"--|"
    r"exact\b|refine\b|apply\b|intro\b|intros\b|have\b|show\b|"
    r"let\b|"
    r"rw\b|rewrite\b|subst\b|simp\b|norm_num\b|omega\b|ring\b|ring_nf\b|"
    r"linarith\b|nlinarith\b|aesop\b|tauto\b|rfl\b|decide\b|"
    r"native_decide\b|constructor\b|left\b|right\b|assumption\b|"
    r"change\b|convert\b|congr\b"
    r")"
)


def extract_lean_code(model_output: str) -> str:
    match = LEAN_FENCE_RE.search(model_output)
    if match:
        return match.group(1).strip()
    return model_output.strip()


def complete_lean_source(
    model_output: str,
    formal_statement: str = "",
    *,
    preserve_header: bool = True,
) -> str:
    code = extract_lean_code(model_output)
    if not formal_statement.strip():
        return code
    stripped_code = code.lstrip()
    imports = _leading_imports_and_opens(code) if preserve_header else ""
    statement = formal_statement.rstrip()
    if stripped_code.startswith("by"):
        if statement.endswith(":= by"):
            body = stripped_code[2:].lstrip()
            wrapped = f"{statement}\n{_indent_body(body)}" if body else statement
            return _with_imports(imports, wrapped)
        return f"{statement}\n{stripped_code}"
    body = _proof_body_from_decl(code)
    if body is not None:
        wrapped = f"{statement}\n{_indent_body(body)}" if body.strip() else statement
        return _with_imports(imports, wrapped)
    body = _body_like_code_without_imports(code)
    if body == "":
        return ""
    if body is not None and statement.endswith(":= by"):
        return _with_imports(imports, f"{statement}\n{_indent_body(body)}")
    return code


def lean_generation_rejection_reason(model_output: str) -> str:
    code = extract_lean_code(model_output)
    lower = code.lower()
    if any(
        marker in lower
        for marker in (
            "no valid lean proof",
            "no valid proof",
            "no placeholder-free lean proof",
            "no placeholder-free proof",
            "does not exist",
            "is false as written",
            "target statement is false",
            "provided statement is false",
            "statement is false",
            "false for arbitrary",
        )
    ):
        return (
            "Lean generation produced no proof and reported possible falsity. "
            f"Model output: {code.strip()}"
        )
    return ""


def _indent_body(body: str) -> str:
    body = _normalize_simple_tactic_body(body)
    return "\n".join(f"  {line}" if line.strip() else line for line in body.splitlines())


def _normalize_simple_tactic_body(body: str) -> str:
    dedented = textwrap.dedent(body).strip()
    dedented = _normalize_tactic_let_lines(dedented)
    dedented = _normalize_finset_sum_membership(dedented)
    lines = dedented.splitlines()
    if not lines:
        return ""
    if any(":= by" in line for line in lines):
        return _normalize_nested_by_body(dedented)
    if all(
        (not line.strip()) or SIMPLE_TACTIC_LINE_RE.match(line.strip())
        for line in lines
    ):
        return "\n".join(line.strip() if line.strip() else "" for line in lines)
    return dedented


def _normalize_tactic_let_lines(body: str) -> str:
    normalized: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("let ") and stripped.endswith(" in"):
            normalized.append(line[: len(line) - 3])
        else:
            normalized.append(line)
    return "\n".join(normalized)


def _normalize_finset_sum_membership(body: str) -> str:
    return re.sub(r"∑\s+([A-Za-z_][A-Za-z0-9_']*)\s+in\s+", r"∑ \1 ∈ ", body)


def _normalize_nested_by_body(body: str) -> str:
    lines = body.splitlines()
    if not lines:
        return body
    if (len(lines[0]) - len(lines[0].lstrip(" "))) != 0:
        return body
    indented = [
        len(line) - len(line.lstrip(" "))
        for line in lines[1:]
        if line.strip()
    ]
    if not indented or min(indented) != 2:
        return body
    normalized = [lines[0]]
    for line in lines[1:]:
        if line.startswith("  "):
            normalized.append(line[2:])
        else:
            normalized.append(line)
    return "\n".join(normalized)


def _leading_imports_and_opens(code: str) -> str:
    lines: list[str] = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("open "):
            lines.append(line)
            continue
        if not stripped:
            if lines:
                lines.append(line)
            continue
        break
    return "\n".join(lines).strip()


def _leading_opens(code: str) -> str:
    lines: list[str] = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            continue
        if stripped.startswith("open "):
            lines.append(line)
            continue
        if not stripped:
            if lines:
                lines.append(line)
            continue
        break
    return "\n".join(lines).strip()


def _with_imports(imports: str, code: str) -> str:
    if not imports:
        return code
    return f"{imports.rstrip()}\n\n{code.lstrip()}"


def _body_like_code_without_imports(code: str) -> str | None:
    body_lines: list[str] = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            continue
        if stripped.startswith("open "):
            continue
        if not stripped and not body_lines:
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body:
        return None
    first = body.splitlines()[0].strip()
    if LEAN_BODY_START_RE.match(first):
        return body
    lower = body.lower()
    if any(
        marker in lower
        for marker in (
            "no valid lean proof",
            "no valid proof",
            "does not exist",
            "is false as written",
            "target statement is false",
            "false for arbitrary",
        )
    ):
        return ""
    return None


def _proof_body_from_decl(code: str) -> str | None:
    match = DECL_RE.search(code)
    if not match:
        return None
    return code[match.end() :].strip()
