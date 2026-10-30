"""Normalize proof packaging without editing Lean terms or protected text."""
import re
import textwrap


def normalize_lean_proof(source: str) -> tuple[str, list[str]]:
    out = []
    i = breaks = 0
    size = len(source)
    while i < size:
        start = i
        if source.startswith('--', i):
            end = source.find('\n', i)
            i = size if end < 0 else end
        elif source.startswith('/-', i):
            depth = 1
            i += 2
            while i < size and depth:
                if source.startswith('/-', i):
                    depth += 1
                    i += 2
                elif source.startswith('-/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif source[i] == '«':
            end = source.find('»', i + 1)
            i = size if end < 0 else end + 1
        elif source[i] == 'r' and (raw := re.match(r'r(#+)"', source[i:])):
            close = '"' + raw.group(1)
            end = source.find(close, i + len(raw.group(0)))
            i = size if end < 0 else end + len(close)
        elif source[i] == '"':
            i += 1
            while i < size:
                if source[i] == '\\':
                    i = min(size, i + 2)
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        elif source[i] == '<' and (br := re.match(r'<br\s*/?>', source[i:], re.I)):
            out.append('\n')
            i += len(br.group(0))
            breaks += 1
            continue
        else:
            i += 1
        out.append(source[start:i])
    body = textwrap.dedent(''.join(out)).strip()
    changes = ['html_line_breaks'] if breaks else []
    # The immutable theorem header already ends in := by.
    if body == 'by':
        body = ''
        changes.append('leading_by_wrapper')
    elif body.startswith('by\n') or body.startswith('by\r\n'):
        body = textwrap.dedent(body[2:].lstrip('\r\n')).strip()
        changes.append('leading_by_wrapper')
    elif re.match(r'^by[ \t]', body):
        first, sep, rest = body[3:].partition('\n')
        body = first.strip()
        if sep:
            body += '\n' + textwrap.dedent(rest).rstrip()
        changes.append('leading_by_wrapper')
    return body, changes
