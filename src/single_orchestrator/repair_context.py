"""Bounded compiler locations and actual edit history, shared by both searches.

This is diagnostic data, not a model-generated diagnosis or a proof certificate.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

_ERROR = re.compile(
    r"^(?:error:\s*)?(?P<file>[^\n]+?\.lean):(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?:error(?:\([^\n)]*\))?:\s*)?(?P<message>.*)$"
)
_BARE_ERROR = re.compile(r"^error(?:\([^\n)]*\))?:\s*(.*)$")
_BOUNDARY = re.compile(r"^(?:error|warning|info|trace)(?:\([^)]*\))?:|^.+\.lean:\d+:\d+:")


def _signature(message):
    return re.sub(r"\?m\.\d+", "?m", re.sub(r"\s+", " ", message)).strip()


def diagnostics(log):
    """Extract source diagnostics, excluding Lake's redundant failure trailers."""
    lines = log.splitlines()
    found = []
    for i, line in enumerate(lines):
        if re.match(r'^(?:info|warning|trace)(?:\([^)]*\))?:', line):
            continue
        match = _ERROR.match(line)
        bare = _BARE_ERROR.match(line) if not match else None
        if not match and not bare:
            continue
        message = match['message'] if match else bare[1]
        if re.match(r'^(?:info|warning|trace)(?:\([^)]*\))?:', message):
            continue
        if message in ('build failed', 'Lean exited with code 1'):
            continue
        detail = [message]
        for following in lines[i + 1:i + 19]:
            if _BOUNDARY.match(following) or following.startswith('Some required targets'):
                break
            detail.append(following)
        item = {'message': '\n'.join(detail).strip()[:1400]}
        if match:
            item.update(source_file=match['file'], line=int(match['line']),
                        column=int(match['column']))
        found.append(item)
    return found


_IDENTIFIER = re.compile(r"[^\W\d][\w']*", re.UNICODE)
_DECLARATION = re.compile(r"^\s*(?:·\s*)?have\s+([^\W\d][\w']*)\s*:\s*(.*)$")


def _masked_source(lines):
    """Mask nested comments and strings, preserving source coordinates."""
    depth, quoted, escaped = 0, False, False
    result = []
    for line in lines:
        out, i = [], 0
        while i < len(line):
            pair, ch = line[i:i + 2], line[i]
            if depth:
                if pair == '/-':
                    depth += 1
                    out.extend('  '); i += 2
                elif pair == '-/':
                    depth -= 1
                    out.extend('  '); i += 2
                else:
                    out.append(' '); i += 1
            elif quoted:
                out.append(' '); i += 1
                if escaped:
                    escaped = False
                elif ch == '\\':
                    escaped = True
                elif ch == '"':
                    quoted = False
            elif pair == '--':
                out.extend(' ' * (len(line) - i)); break
            elif pair == '/-':
                depth = 1
                out.extend('  '); i += 2
            elif ch == '"':
                quoted = True
                out.append(' '); i += 1
            else:
                out.append(ch); i += 1
        result.append(''.join(out))
    return result


def _local_statement_candidates(source, masked, line):
    """A conservative lexical index, never a Lean scope/type certificate.

    Include preceding explicit `have name : type := ...` statements mentioning
    identifiers at the error site. Skip apparent closed blocks and shadowed names.
    The existing full source remains authoritative; no new facts are inferred.
    """
    site = '\n'.join(masked[max(0, line - 2):line + 1])
    query = set(_IDENTIFIER.findall(site)) - {
        'ℝ', 'ℕ', 'ℤ', 'ℂ', 'Prop', 'Type', 'Set', 'Real', 'Nat', 'Int',
        'have', 'by', 'show', 'rw', 'simp', 'calc', 'exact', 'norm',
    }
    candidates = {}
    stop = line - 1
    for i in range(max(0, stop - 250), stop):
        match = _DECLARATION.match(masked[i])
        if not match:
            continue
        name, statement = match.groups()
        end = i
        while ':=' not in statement and end < min(i + 3, stop - 1):
            end += 1
            statement += ' ' + masked[end].strip()
        if ':=' not in statement:
            continue
        statement, rhs = statement.split(':=', 1)
        statement = statement.strip()
        indent = len(masked[i]) - len(masked[i].lstrip())
        # A declaration currently being proved is not a usable local hypothesis.
        # Only recognize the easy multiline `:= by` case; the scope disclaimer
        # still applies to arbitrary term/tactic syntax.
        if rhs.strip() == 'by' and not any(
                later.strip() and len(later) - len(later.lstrip()) <= indent
                for later in masked[end + 1:stop]):
            continue
        closed = False
        for later in masked[end + 1:stop + 1]:
            if not later.strip():
                continue
            level = len(later) - len(later.lstrip())
            if level < indent or (level == indent and
                    re.match(r'(?:·|case\b|next\b|\|)', later.lstrip())):
                closed = True
                break
        if not closed:
            candidates[name] = (i, statement)
    ranked = []
    for name, (i, statement) in candidates.items():
        overlap = (set(_IDENTIFIER.findall(statement)) | {name}) & query
        if overlap:
            ranked.append((len(overlap), i, name, statement))
    ranked.sort(reverse=True)
    result, size = [], 0
    for _, i, name, statement in ranked[:8]:
        text = source[i].strip()[:240]
        if size + len(text) > 1600:
            break
        result.append({'line': i + 1, 'name': name, 'source_statement': text})
        size += len(text)
    return result


def build_repair_context(*, lean_proof, formal_state, error_log, previous=None,
                         source_file=None):
    """Use compiled-source coordinates; tactic-body diff coordinates stay separate."""
    errors = diagnostics(error_log)
    prior_errors = diagnostics(previous.get('lean_error_log', '')) if previous else []
    prior_signatures = {_signature(e['message']) for e in prior_errors}
    source = formal_state.splitlines()
    masked = _masked_source(source)
    locations = []
    for error in errors[:6]:
        item = dict(error)
        item['repeated_from_previous_attempt'] = _signature(error['message']) in prior_signatures
        same_file = source_file is not None and Path(error.get('source_file', '')).name == Path(source_file).name
        line = error.get('line', 0)
        if same_file and 1 <= line <= len(source):
            start, end = max(1, line - 3), min(len(source), line + 3)
            item['compiled_source_excerpt'] = [
                {'line': n, 'text': source[n - 1][:400]} for n in range(start, end + 1)]
            candidates = _local_statement_candidates(source, masked, line)
            if candidates:
                item['local_statement_candidates'] = candidates
        locations.append(item)
    context = {
        'schema_version': 2,
        'evidence_scope': 'Compiler errors and source excerpts only. Lines without errors are not certified correct.',
        'diagnostics': locations,
        'diagnostics_total': len(errors),
        'diagnostics_omitted': max(0, len(errors) - len(locations)),
        'local_statement_scope': 'Exact preceding source declarations selected lexically, not verified facts or a guarantee of current scope. Check their proofs, binders and applicability in the full code. No derived statements or error summaries are generated.',
    }
    if previous is not None:
        diff = '\n'.join(difflib.unified_diff(
            previous.get('lean_proof', '').splitlines(), lean_proof.splitlines(),
            fromfile='previous_tactic_body', tofile='current_tactic_body', lineterm='', n=2))
        context['previous_to_current_tactic_diff'] = diff[:6000]
        context['diff_truncated'] = len(diff) > 6000
        context['previous_repair_guidance'] = previous.get('retry_plan', '')[:4000]
        context['previous_compiler_errors'] = [e['message'] for e in prior_errors[:3]]
    return context
