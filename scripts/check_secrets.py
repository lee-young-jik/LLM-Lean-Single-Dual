from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SECRET_TEMPLATES = {".env.example"}
FORBIDDEN_FILENAMES = {
    ".env",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
SECRET_PATTERNS = {
    "OpenAI/OpenRouter key": re.compile(r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{16,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "macOS home path": re.compile("/" + r"Users/[^/\s]+/"),
    "Linux home path": re.compile("/" + r"home/[^/\s]+/"),
}
NONEMPTY_SECRET_ASSIGNMENT = re.compile(
    r"(?m)^\s*(?:export\s+)?"
    r"[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY)"
    r"\s*=\s*(?P<value>[^\s#]+)"
)


def git_paths(staged: bool) -> list[str]:
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
    if not staged:
        command = ["git", "ls-files", "-z"]
    output = subprocess.check_output(command, cwd=ROOT)
    return [
        item.decode("utf-8")
        for item in output.split(b"\0")
        if item
    ]


def read_path(path: str, staged: bool) -> str | None:
    try:
        if staged:
            data = subprocess.check_output(
                ["git", "show", f":{path}"],
                cwd=ROOT,
                stderr=subprocess.DEVNULL,
            )
        else:
            data = (ROOT / path).read_bytes()
        return data.decode("utf-8")
    except (UnicodeDecodeError, OSError, subprocess.CalledProcessError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Inspect the exact content staged for commit.",
    )
    args = parser.parse_args()

    findings: list[str] = []
    for path in git_paths(args.staged):
        name = Path(path).name
        if name in FORBIDDEN_FILENAMES and path not in ALLOWED_SECRET_TEMPLATES:
            findings.append(f"{path}: forbidden secret-bearing filename")
            continue

        text = read_path(path, args.staged)
        if text is None:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line}: possible {label}")
        if path not in ALLOWED_SECRET_TEMPLATES:
            for match in NONEMPTY_SECRET_ASSIGNMENT.finditer(text):
                value = match.group("value").strip("\"'")
                if value and not value.startswith(("$", "{", "<")):
                    line = text.count("\n", 0, match.start()) + 1
                    findings.append(
                        f"{path}:{line}: non-empty secret environment assignment"
                    )

    if findings:
        print("Potential secrets or personal paths were found:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    scope = "staged files" if args.staged else "tracked files"
    print(f"Secret scan passed for {scope}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
