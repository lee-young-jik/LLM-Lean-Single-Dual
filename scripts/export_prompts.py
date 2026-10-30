#!/usr/bin/env python3
"""Export readable role instructions from runtime constants; no model calls."""
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("goal_prompts", ROOT / "src/single_orchestrator/goal_prompts.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def exports():
    result = {}
    for mode in ("single", "dual"):
        dual = mode == "dual"
        direction = p.DUAL_DIRECTION if dual else p.SINGLE_DIRECTION
        roles = {
            "orchestrator": p.planner_instruction(direction, use_memory=True) + ("\n" + p.DUAL_PLAN_RESTART if dual else ""),
            "natural": p.NATURAL + ("\n" + p.DUAL_NATURAL_CONTINUE if dual else ""),
            "lean": p.LEAN + ("\n" + p.DUAL_LEAN_CONTINUE if dual else ""),
            "repair": p.REPAIR + ("\n" + p.DUAL_REPAIR_CONTINUE if dual else ""),
        }
        for role, text in roles.items():
            result[f"{mode}/{role}.txt"] = text + "\n"
    result["dual/role_assumptions.json"] = json.dumps(p.DUAL_ROLES, ensure_ascii=False, indent=2) + "\n"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for relative, content in exports().items():
        path = ROOT / "prompts" / relative
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Prompt export is outdated: {relative}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    print("Prompt exports match runtime instructions." if args.check else "Exported role instructions.")
