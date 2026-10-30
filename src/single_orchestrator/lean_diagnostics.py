"""Keep planner error memory anchored to compiler facts, not guessed causes."""
import re


def compact_errors(log):
    lines = log.splitlines()
    messages = []
    for i, line in enumerate(lines):
        if 'error:' not in line:
            continue
        message = line.split('error:', 1)[1].strip()
        details = []
        for following in lines[i+1:i+5]:
            if not following.strip() or 'depends on axioms' in following:
                break
            details.append(following.strip())
        messages.append('\n'.join([message, *details])[:300])
        if len(messages) == 2:
            break
    if messages:
        return '\n'.join(messages)[:600]
    return next((s.strip()[:300] for s in lines if s.strip() and 'depends on axioms' not in s),
                'No compiler diagnostic was recorded.')


def grounded_summary(proposed, log):
    def norm(s):
        return re.sub(r'\s+', ' ', s.replace('`', '').strip(' \"\''))
    observed = compact_errors(log)
    excerpts = [norm(s) for s in proposed.splitlines() if s.strip()]
    if (excerpts and len(proposed) <= 600 and
            all(s in norm(observed) for s in excerpts)):
        return proposed.strip(), 'repair_quote'
    return observed, 'compiler_excerpt'
