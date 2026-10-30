from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from typing import Callable

from .proof_audit import audit_lean_proof
from .state import VerifierResult


CommandRunner = Callable[
    [list[str], Path, int],
    subprocess.CompletedProcess[str],
]

MAX_VERIFIER_RSS_KIB = int(os.getenv("DUAL_ARC_MAX_VERIFIER_RSS_KIB", "0"))


@contextmanager
def _project_verification_lock(project_root: Path):
    """Serialize memory-heavy Lean processes that share one Lake project."""
    lock_path = project_root / ".dual_arc_lean_verify.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


PROHIBITED_PLACEHOLDER_RE = re.compile(r"\b(sorry|admit|axiom)\b")
TIMEOUT_SENSITIVE_TACTIC_RE = re.compile(r"\b(nlinarith|linarith)\b")
ARITHMETIC_AUTOMATION_RE = re.compile(r"\b(nlinarith|linarith)\b")
BROAD_NORM_NUM_AT_TARGET_RE = re.compile(r"\bnorm_num\s+at\s+[^\n]*⊢")
REAL_THEOREM_RE = re.compile(r"\btheorem\b[\s\S]*?:\s*[^:=]*\bℝ\b[\s\S]*?:=\s*by")
BARE_NORM_NUM_LINE_RE = re.compile(r"(?m)^\s*norm_num\s*(?:--.*)?$")
BARE_SIMP_LINE_RE = re.compile(r"(?m)^\s*simp\s*(?:--.*)?$")
LARGE_EXPLICIT_FINSET_RE = re.compile(
    r"\(\{(?:[^{}]*,){8,}[^{}]*\}\s*:\s*Finset\b", re.DOTALL
)
THEOREM_WITH_BINDERS_RE = re.compile(r"\btheorem\b[\s\S]*?\([^)]*:[^)]*\)[\s\S]*?:=\s*by")
DECISION_TACTIC_RE = re.compile(r"\b(native_decide|decide)\b")
ANALYTIC_EXPRESSION_RE = re.compile(r"\bReal\.log(?:_mul|_pos)?\b")
COERCION_ORDER_PATTERN_RE = re.compile(r"\bmul_lt_mul_of_pos_left\b")
FRAGILE_DIVISION_REWRITE_RE = re.compile(r"\bdiv_mul_eq_mul_div\b|\bmul_div_cancel")
INTERVAL_CASES_NORM_NUM_TARGET_RE = re.compile(
    r"^(?P<indent>\s*)interval_cases\s+(?P<var>[A-Za-z_][A-Za-z0-9_']*)\s*"
    r"<;>\s*norm_num\s+at\s+[^;\n]*⊢\s*$"
)
INTERVAL_CASES_SIMP_ALL_RE = re.compile(
    r"(?m)^\s*interval_cases\s+(?P<var>[A-Za-z_][A-Za-z0-9_']*)\b"
    r"[^\n]*\bsimp_all\b[^\n]*$"
)
INTERVAL_CASES_ALL_GOALS_SIMP_ALL_RE = re.compile(
    r"(?ms)^\s*interval_cases\s+[A-Za-z_][A-Za-z0-9_']*\s*$"
    r".{0,120}?^\s*all_goals\s*$\s*^\s*simp_all\b"
)
DISJUNCTION_BY_OMEGA_RE = re.compile(
    r"(?m)^\s*have\s+\S+\s*:\s*[^\n]*\s∨\s[^\n]*:=\s*by\s*$\s*^\s*omega\s*$"
)
# Anchor to the end of the declaration, not any local `have ... := by omega`.
# Goal verification appends a declaration-specific axiom query after the proof.
BARE_OMEGA_PROOF_RE = re.compile(
    r"(?s):=\s*by\s*omega\s*(?:#print\s+axioms\s+[A-Za-z0-9_'.]+\s*)?\Z"
)
NONLINEAR_VARIABLE_EXPRESSION_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_']*\s*\*\s*[A-Za-z_][A-Za-z0-9_']*\b"
    r"|\b[A-Za-z_][A-Za-z0-9_']*\s*\^\s*[2-9]\d*\b"
)
INTERVAL_CASES_RE = re.compile(r"\binterval_cases\s+(?P<var>[A-Za-z_][A-Za-z0-9_']*)\b")
# A zero heartbeat budget is unlimited in Lean. The evaluation's wall-clock
# verifier timeout is configured separately; do not add a hidden tactic timeout.
MINIF2F_MAX_HEARTBEATS = 0
MINIF2F_HEARTBEAT_OPTION = f"set_option maxHeartbeats {MINIF2F_MAX_HEARTBEATS}"
MAX_HEARTBEATS_OPTION_RE = re.compile(
    r"(?m)(^[ \t]*set_option[ \t]+maxHeartbeats[ \t]+)\d+"
)
UNAVAILABLE_API_NAMES = (
    "Real.cbrt",
    "Real.cbrt_pow",
    "Real.logb",
    "irrational_cbrt_two",
    "irrational_nrt_of_notint_nrt",
    "Polynomial.irreducible_X_pow_sub_C",
    "Nat.dvd_mul_of_dvd_right",
    "Nat.pos_of_gt",
)
UNAVAILABLE_API_RE = re.compile(
    r"(?<![A-Za-z0-9_'.])("
    + "|".join(re.escape(name) for name in UNAVAILABLE_API_NAMES)
    + r")(?![A-Za-z0-9_'.])"
)
BROAD_IMPORT_RE = re.compile(
    r"(?m)^\s*import\s+(Mathlib|Mathlib\.Tactic|MiniF2F\.(Test|Valid))\s*$"
)
IMPORT_RE = re.compile(r"(?m)^\s*import\s+([A-Za-z0-9_'.]+)\s*$")
IMPORT_LINE_RE = re.compile(r"^(\s*import\s+)([A-Za-z0-9_'.]+)(\s*)$")
IMPORT_REWRITE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Mathlib.Tactic.Nlinarith": ("Mathlib.Tactic.Linarith",),
    "Mathlib.Tactic.Omega": ("Mathlib.Tactic.Omega", "Mathlib.Tactic"),
    "Mathlib.Tactic.NativeDecide": ("Mathlib.Tactic.NativeDecide",),
    "Mathlib.Tactic.ExactModCast": ("Mathlib.Tactic.NormNum",),
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
LEAN_STATEMENT_KEYWORDS = (
    "have ",
    "let ",
    "suffices ",
    "show ",
    "exact ",
    "apply ",
    "refine ",
    "rw ",
    "simp ",
    "simpa ",
    "norm_num",
    "linarith",
    "nlinarith",
    "ring",
    "ring_nf",
    "omega",
    "positivity",
    "by_cases ",
    "rcases ",
    "cases ",
    "intro ",
    "calc",
)


def _default_runner(
    command: list[str], cwd: Path, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as stdout_file:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
            timed_out = False
            memory_exceeded = False

            while process.poll() is None:
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except Exception:
                        process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except Exception:
                            process.kill()
                        process.wait()
                    break
                if (
                    MAX_VERIFIER_RSS_KIB > 0
                    and _process_group_rss_kib(process.pid) > MAX_VERIFIER_RSS_KIB
                ):
                    memory_exceeded = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except Exception:
                        process.kill()
                    process.wait()
                    break
                time.sleep(0.2)

            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
            if timed_out:
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    output=stdout,
                    stderr=stderr,
                )
            if memory_exceeded:
                stderr = "\n".join(
                    part
                    for part in (
                        stderr.strip(),
                        "Lean verification exceeded the configured process-group memory limit.",
                    )
                    if part
                )
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _process_group_rss_kib(process_group_id: int) -> int:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pgid=,rss="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return 0

    total = 0
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pgid, rss_kib = map(int, fields)
        except ValueError:
            continue
        if pgid == process_group_id:
            total += rss_kib
    return total


class LakeVerifier:
    def __init__(
        self,
        project_root: Path,
        module_name: str = "Scratch.Generated",
        lean_file: Path | None = None,
        prelude: str = "",
        timeout_seconds: int = 60,
        strict_timeout_guard: bool | None = None,
        command_runner: CommandRunner = _default_runner,
    ):
        self.project_root = project_root
        self.module_name = module_name
        self.lean_file = lean_file or project_root / "Scratch" / "Generated.lean"
        self.prelude = prelude
        self.timeout_seconds = timeout_seconds
        self.strict_timeout_guard = (
            strict_timeout_guard
            if strict_timeout_guard is not None
            else os.environ.get("DUAL_ARC_STRICT_TIMEOUT_GUARD", "").lower()
            in {"1", "true", "yes", "on"}
        )
        self.command_runner = command_runner

    def verify(self, lean_code: str, formal_statement: str = "") -> VerifierResult:
        command = ["lake", "build", self.module_name]
        if not lean_code.strip():
            return {
                "success": False,
                "formal_state": "",
                "lean_error_log": "No Lean code was generated.",
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": True,
            }

        code_before_interval_rewrite = self._with_verifier_options(
            self._rewrite_imports(self._with_prelude(lean_code))
        )
        code_to_verify = self._with_verifier_options(
            self._rewrite_imports(
                self._rewrite_no_goals_prone_interval_cases(self._with_prelude(lean_code))
            )
        )
        if PROHIBITED_PLACEHOLDER_RE.search(code_to_verify):
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": "Generated Lean contains a prohibited placeholder: sorry/admit/axiom.",
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": True,
            }
        broad_arithmetic_log = self._broad_arithmetic_automation_log(code_to_verify)
        if broad_arithmetic_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": broad_arithmetic_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        broad_norm_num_log = self._broad_norm_num_timeout_guard_log(
            code_before_interval_rewrite
        )
        if broad_norm_num_log:
            return {
                "success": False,
                "formal_state": code_before_interval_rewrite,
                "lean_error_log": broad_norm_num_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        real_norm_num_log = self._real_norm_num_timeout_guard_log(code_to_verify)
        if real_norm_num_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": real_norm_num_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        expensive_finset_log = self._expensive_finset_normalization_log(code_to_verify)
        if expensive_finset_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": expensive_finset_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        broad_interval_log = self._broad_interval_cases_target_log(
            code_before_interval_rewrite
        )
        if broad_interval_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": broad_interval_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        omega_disjunction_log = self._omega_disjunction_log(code_to_verify)
        if omega_disjunction_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": omega_disjunction_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        large_interval_log = self._large_interval_cases_log(code_before_interval_rewrite)
        if large_interval_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": large_interval_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        decision_log = self._decision_timeout_guard_log(code_to_verify)
        if decision_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": decision_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        strict_timeout_guard_log = self._strict_timeout_guard_log(code_to_verify)
        if strict_timeout_guard_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": strict_timeout_guard_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        fragile_log = self._fragile_log_proof_pattern_log(code_to_verify)
        if fragile_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": fragile_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        # Dataset-provided imports are the trusted verification environment.
        # The broad-import performance guard applies only to new model imports.
        trusted_broad_imports = {
            match.group(1) for match in BROAD_IMPORT_RE.finditer(self.prelude)
        }
        broad_import = next(
            (match for match in BROAD_IMPORT_RE.finditer(code_to_verify)
             if match.group(1) not in trusted_broad_imports),
            None,
        )
        if broad_import:
            imported = broad_import.group(1)
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": (
                    f"Generated Lean imports broad module `{imported}`, which is too "
                    "expensive for per-trial verification. Retry by generating "
                    "specific narrow imports in the Lean source."
                ),
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        unavailable_imports = self._unavailable_imports(code_to_verify)
        if unavailable_imports:
            imported = unavailable_imports[0]
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": (
                    f"Generated Lean imports unavailable module `{imported}` in "
                    "this local Lean/Mathlib environment. Retry with an import "
                    "that exists in the current project snapshot, or avoid the "
                    "extra import if it is not needed."
                ),
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": False,
            }
        unavailable_api = UNAVAILABLE_API_RE.search(code_to_verify)
        if unavailable_api:
            api = unavailable_api.group(0)
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": (
                    f"Generated Lean uses unavailable API `{api}` in this MiniF2F "
                    "environment. Retry without that API; use local algebraic facts, "
                    "hypotheses already in context, or already imported lemmas only."
                ),
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": True,
            }
        malformed_log = self._cheap_malformed_lean_log(code_to_verify)
        if malformed_log:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": malformed_log,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": True,
            }

        # Target alignment is syntactic and does not require Lean. Rejecting a
        # different theorem (or a standalone counterexample) before Lake avoids
        # spending minutes compiling code that can never count as a proof.
        pre_build_audit = audit_lean_proof(code_to_verify, formal_statement)
        if not pre_build_audit.success:
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": pre_build_audit.rationale,
                "returncode": None,
                "command": command,
                "timed_out": False,
                "has_prohibited_placeholder": True,
            }

        self.lean_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Loading the full MiniF2F/Mathlib environment can consume gigabytes.
            # Lock the write and build together so another process cannot replace
            # the generated module while Lake is reading it.
            with _project_verification_lock(self.project_root):
                self.lean_file.write_text(
                    code_to_verify.rstrip() + "\n", encoding="utf-8"
                )
                completed = self.command_runner(
                    command, self.project_root, self.timeout_seconds
                )
        except subprocess.TimeoutExpired as exc:
            log = self._join_logs(exc.output, exc.stderr)
            timeout_tactic = TIMEOUT_SENSITIVE_TACTIC_RE.search(code_to_verify)
            tactic_note = (
                f"\nTimed-out Lean proof used `{timeout_tactic.group(1)}`; "
                "the next retry should avoid that tactic and use smaller local steps."
                if timeout_tactic
                else ""
            )
            return {
                "success": False,
                "formal_state": code_to_verify,
                "lean_error_log": (
                    f"lake build timed out after {self.timeout_seconds} seconds."
                    f"{tactic_note}\n{log}"
                ).strip(),
                "returncode": None,
                "command": command,
                "timed_out": True,
                "has_prohibited_placeholder": False,
            }

        log = self._join_logs(completed.stdout, completed.stderr)
        if completed.returncode == 0:
            audit = audit_lean_proof(code_to_verify, formal_statement)
            if not audit.success:
                audit_log = "\n".join(part for part in (log, audit.rationale) if part).strip()
                return {
                    "success": False,
                    "formal_state": code_to_verify,
                    "lean_error_log": audit_log,
                    "returncode": completed.returncode,
                    "command": command,
                    "timed_out": False,
                    "has_prohibited_placeholder": True,
                }
        return {
            "success": completed.returncode == 0,
            "formal_state": code_to_verify,
            "lean_error_log": log,
            "returncode": completed.returncode,
            "command": command,
            "timed_out": False,
            "has_prohibited_placeholder": False,
        }

    def _with_prelude(self, lean_code: str) -> str:
        if not self.prelude.strip():
            return lean_code
        imports: list[str] = []
        opens: list[str] = []
        body: list[str] = []
        seen: set[str] = set()

        def add_header_line(line: str) -> bool:
            stripped = line.strip()
            if not stripped:
                return True
            if stripped.startswith("import "):
                if stripped not in seen:
                    imports.append(stripped)
                    seen.add(stripped)
                return True
            if stripped.startswith("open "):
                if stripped not in seen:
                    opens.append(stripped)
                    seen.add(stripped)
                return True
            return False

        for line in self.prelude.strip().splitlines():
            add_header_line(line)
        for line in lean_code.lstrip().splitlines():
            if add_header_line(line):
                continue
            body.append(line)

        sections = [
            "\n".join(imports).strip(),
            "\n".join(opens).strip(),
            "\n".join(body).strip(),
        ]
        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _rewrite_no_goals_prone_interval_cases(lean_code: str) -> str:
        lines: list[str] = []
        for line in lean_code.splitlines():
            match = INTERVAL_CASES_NORM_NUM_TARGET_RE.match(line)
            if match:
                lines.append(f"{match.group('indent')}interval_cases {match.group('var')} <;> simp_all")
            else:
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _with_verifier_options(lean_code: str) -> str:
        if "MiniF2F.ProblemImports" not in lean_code:
            return lean_code
        if "set_option maxHeartbeats" in lean_code:
            return MAX_HEARTBEATS_OPTION_RE.sub(
                rf"\g<1>{MINIF2F_MAX_HEARTBEATS}", lean_code
            )

        lines = lean_code.splitlines()
        insert_at = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("import ") or not stripped:
                insert_at = index + 1
                continue
            break
        lines.insert(insert_at, MINIF2F_HEARTBEAT_OPTION)
        return "\n".join(lines)

    @staticmethod
    def _broad_arithmetic_automation_log(lean_code: str) -> str:
        if not ANALYTIC_EXPRESSION_RE.search(lean_code):
            return ""
        tactics = ARITHMETIC_AUTOMATION_RE.findall(lean_code)
        if not tactics:
            return ""
        tactic_list = ", ".join(f"`{tactic}`" for tactic in sorted(set(tactics)))
        return (
            "Generated Lean uses broad arithmetic automation "
            f"({tactic_list}) inside a Real.log/logarithm proof. This pattern is "
            "timeout-prone in the MiniF2F verifier. Retry with smaller local "
            "steps: use explicit positivity/nonzero facts, `rw`/`calc` to expose "
            "the exact algebraic equality, and reserve arithmetic normalization "
            "for tiny numeric subgoals."
        )

    def _strict_timeout_guard_log(self, lean_code: str) -> str:
        if not self.strict_timeout_guard:
            return ""
        tactics = TIMEOUT_SENSITIVE_TACTIC_RE.findall(lean_code)
        if not tactics:
            return ""
        tactic_list = ", ".join(f"`{tactic}`" for tactic in sorted(set(tactics)))
        return (
            "Strict timeout guard rejected generated Lean before lake build "
            f"because it uses timeout-sensitive tactic(s): {tactic_list}. "
            "Retry with a shorter proof using direct rewrites, explicit local "
            "`have` facts, `norm_num` for tiny numeric goals, `ring`/`ring_nf` "
            "for algebraic normalization, or direct case splits. This avoids "
            "spending the verifier timeout on broad proof search."
        )

    def _broad_norm_num_timeout_guard_log(self, lean_code: str) -> str:
        if not self.strict_timeout_guard:
            return ""
        if not BROAD_NORM_NUM_AT_TARGET_RE.search(lean_code):
            return ""
        return (
            "Strict timeout guard rejected generated Lean before lake build "
            "because it uses broad normalization `norm_num at ... ⊢`. "
            "Retry by normalizing only closed numeric side goals with `norm_num`; "
            "for hypotheses or goals containing variables, use direct rewrites, "
            "explicit local `have` facts, `ring`/`ring_nf`, or finite case splits "
            "that close each branch locally."
        )

    def _real_norm_num_timeout_guard_log(self, lean_code: str) -> str:
        if not self.strict_timeout_guard:
            return ""
        if not REAL_THEOREM_RE.search(lean_code):
            return ""
        if not BARE_NORM_NUM_LINE_RE.search(lean_code):
            return ""
        if "rw [" not in lean_code and "simp [" not in lean_code:
            return ""
        return (
            "Strict timeout guard rejected generated Lean before lake build "
            "because it uses a broad bare `norm_num` cleanup after rewriting a "
            "Real-valued theorem. This pattern has repeatedly spent the full "
            "MiniF2F verifier timeout. Retry with a proof that avoids broad Real "
            "normalization: use exact local equalities, a smaller `calc` chain, "
            "or normalize only closed numeric side goals."
        )

    def _decision_timeout_guard_log(self, lean_code: str) -> str:
        if not self.strict_timeout_guard:
            return ""
        tactic = DECISION_TACTIC_RE.search(lean_code)
        if not tactic:
            return ""
        if not THEOREM_WITH_BINDERS_RE.search(lean_code):
            return ""
        return (
            "Strict timeout guard rejected generated Lean before lake build "
            f"because it uses `{tactic.group(1)}` on a theorem with binders. "
            "This repeatedly times out on MiniF2F goals with free variables or "
            "non-finite domains. Retry with explicit local reasoning, bounded "
            "case splits, direct rewrites, or small closed numeric checks only."
        )

    @staticmethod
    def _expensive_finset_normalization_log(lean_code: str) -> str:
        lines = [line.strip() for line in lean_code.rstrip().splitlines() if line.strip()]
        final_line = lines[-1] if lines else ""
        if not re.match(r"(?:simp|norm_num)(?:\s|$)", final_line):
            return ""
        if "Finset.Icc" not in lean_code and not LARGE_EXPLICIT_FINSET_RE.search(lean_code):
            return ""
        return (
            "Generated Lean asks bare `simp`/`norm_num` to normalize a large Finset. "
            "This proof shape exhausts memory in the MiniF2F environment. Retry "
            "with a cardinality lemma or a small local cardinality calculation "
            "instead of expanding the entire Finset during lake build."
        )

    @staticmethod
    def _broad_interval_cases_target_log(lean_code: str) -> str:
        match = INTERVAL_CASES_SIMP_ALL_RE.search(lean_code)
        multiline_match = INTERVAL_CASES_ALL_GOALS_SIMP_ALL_RE.search(lean_code)
        if not match and not multiline_match:
            return ""
        return (
            "Generated Lean combines `interval_cases` with broad "
            "`simp_all`, which expands the full context in every branch "
            "and exhausts memory in the MiniF2F environment. Retry by closing each "
            "branch with a local tactic that does not simplify the full target."
        )

    @staticmethod
    def _large_interval_cases_log(lean_code: str) -> str:
        match = INTERVAL_CASES_RE.search(lean_code)
        if not match:
            return ""
        variable = re.escape(match.group("var"))
        bounds = re.findall(rf"\b{variable}\s*(<|≤)\s*(\d+)\b", lean_code)
        if not bounds:
            return ""
        case_counts = [int(raw) if op == "<" else int(raw) + 1 for op, raw in bounds]
        case_count = min(case_counts)
        if case_count <= 10:
            return ""
        return (
            f"Generated Lean asks `interval_cases` to expand at least {case_count} "
            f"branches for `{match.group('var')}`. This exhausts memory in the "
            "MiniF2F environment. Retry with an algebraic argument or derive a "
            "tighter bound before finite case analysis."
        )

    @staticmethod
    def _omega_disjunction_log(lean_code: str) -> str:
        if DISJUNCTION_BY_OMEGA_RE.search(lean_code):
            return (
                "Generated Lean asks `omega` to derive a disjunction of nonlinear "
                "solutions. This proof shape exhausts memory in the MiniF2F "
                "environment. Retry with explicit factorization and bounded cases."
            )
        if BARE_OMEGA_PROOF_RE.search(
            lean_code
        ) and NONLINEAR_VARIABLE_EXPRESSION_RE.search(lean_code):
            return (
                "Generated Lean asks bare `omega` to solve a nonlinear theorem. "
                "This proof shape exhausts memory in the MiniF2F environment. "
                "Retry with explicit algebraic intermediate steps."
            )
        return ""

    @staticmethod
    def _fragile_log_proof_pattern_log(lean_code: str) -> str:
        if not ANALYTIC_EXPRESSION_RE.search(lean_code):
            return ""
        if COERCION_ORDER_PATTERN_RE.search(lean_code):
            return (
                "Generated Lean uses `mul_lt_mul_of_pos_left` inside a Real.log "
                "proof with natural variables coerced to reals. This commonly "
                "creates Nat/Real type mismatches in MiniF2F. Retry by proving the "
                "needed product inequalities directly as explicit `ℝ` facts, or "
                "avoid those inequalities when nonzero facts are sufficient."
            )
        if FRAGILE_DIVISION_REWRITE_RE.search(lean_code):
            return (
                "Generated Lean uses fragile division rewrites inside a Real.log "
                "proof. This commonly fails because Lean cannot find the exact "
                "syntactic pattern. Retry with direct conversions like "
                "`(div_eq_iff h).mp hypothesis` or `(eq_div_iff h).2 ...`, using "
                "the required nonzero fact explicitly."
            )
        return ""

    def _rewrite_imports(self, lean_code: str) -> str:
        lines: list[str] = []
        inserted: set[str] = set()
        for line in lean_code.splitlines():
            match = IMPORT_LINE_RE.match(line)
            if not match:
                lines.append(line)
                continue
            module = match.group(2)
            replacement = self._replacement_imports(module)
            if replacement is None:
                if module not in inserted:
                    lines.append(line)
                    inserted.add(module)
                continue
            for replacement_module in replacement:
                if replacement_module in inserted:
                    continue
                lines.append(f"{match.group(1)}{replacement_module}{match.group(3)}")
                inserted.add(replacement_module)
        return "\n".join(lines)

    def _replacement_imports(self, module: str) -> tuple[str, ...] | None:
        if self._module_exists(module):
            return None
        candidates = IMPORT_REWRITE_CANDIDATES.get(module, ())
        available = tuple(candidate for candidate in candidates if self._module_exists(candidate))
        return available or None

    def _unavailable_imports(self, lean_code: str) -> list[str]:
        if not (self.project_root / ".lake").exists():
            return []
        unavailable: list[str] = []
        for module in IMPORT_RE.findall(lean_code):
            if not self._module_exists(module):
                unavailable.append(module)
        return unavailable

    def _module_exists(self, module: str) -> bool:
        relative = Path(*module.split("."))
        source_candidates = [self.project_root / f"{relative}.lean"]
        source_candidates.extend(
            package / f"{relative}.lean"
            for package in (self.project_root / ".lake" / "packages").glob("*")
        )
        if any(path.exists() for path in source_candidates):
            return True

        olean_candidates = [self.project_root / ".lake" / "build" / "lib" / "lean" / f"{relative}.olean"]
        olean_candidates.extend(
            package / ".lake" / "build" / "lib" / "lean" / f"{relative}.olean"
            for package in (self.project_root / ".lake" / "packages").glob("*")
        )
        return any(path.exists() for path in olean_candidates)

    @staticmethod
    def _cheap_malformed_lean_log(lean_code: str) -> str:
        lines = lean_code.splitlines()
        for index, line in enumerate(lines[:-1]):
            stripped = line.strip()
            if not stripped or ":=" not in stripped:
                continue
            if stripped.endswith(":= by") or stripped.endswith(":= by;"):
                continue
            if " := by " in stripped or stripped.startswith(("theorem ", "lemma ", "example ")):
                continue
            next_line = next((candidate for candidate in lines[index + 1 :] if candidate.strip()), "")
            if not next_line:
                continue
            indent = len(line) - len(line.lstrip(" "))
            next_indent = len(next_line) - len(next_line.lstrip(" "))
            next_stripped = next_line.strip()
            if next_indent > indent and next_stripped.startswith(LEAN_STATEMENT_KEYWORDS):
                return (
                    "Generated Lean appears malformed before verification: a line "
                    "with a complete `:= ...` proof is followed by a more-indented "
                    f"`{next_stripped.split(maxsplit=1)[0]}` statement. This usually "
                    "means the proof body indentation is broken. Retry with a flat "
                    "top-level theorem body where sibling `have` statements use the "
                    "same indentation."
                )
        return ""

    @staticmethod
    def _join_logs(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
        def normalize(value: str | bytes | None) -> str:
            if value is None:
                return ""
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value

        return "\n".join(
            part.strip()
            for part in (normalize(stdout), normalize(stderr))
            if part and part.strip()
        )
