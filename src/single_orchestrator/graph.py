from __future__ import annotations

from typing import Literal

from langgraph.graph import END, StateGraph

from .runtime import SingleOrchestratorRuntime
from .state import GraphState


def build_graph(runtime: SingleOrchestratorRuntime):
    graph = StateGraph(GraphState)

    graph.add_node("orchestrator", runtime.create_strategy)
    graph.add_node("nature_agent", runtime.create_nature_solution)
    graph.add_node("lean_generator", runtime.generate_lean)
    graph.add_node("verifier", runtime.verify_lean)
    graph.add_node("retry_planner", runtime.create_retry_plan)
    graph.add_node("trial_recorder", runtime.record_trial)
    graph.add_node("false_decision", runtime.decide_false_after_round)

    graph.set_entry_point("orchestrator")
    graph.add_edge("orchestrator", "nature_agent")
    graph.add_edge("nature_agent", "lean_generator")
    graph.add_edge("lean_generator", "verifier")
    graph.add_conditional_edges(
        "verifier",
        _route_after_verifier,
        {"success": "trial_recorder", "failure": "retry_planner"},
    )
    graph.add_edge("retry_planner", "trial_recorder")
    graph.add_conditional_edges(
        "trial_recorder",
        _route_after_trial,
        {
            "retry": "nature_agent",
            "false_decision": "false_decision",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "false_decision",
        _route_after_false_decision,
        {"restrategy": "orchestrator", "end": END},
    )

    return graph.compile()


def _route_after_verifier(state: GraphState) -> Literal["success", "failure"]:
    return "success" if state["verifier_result"]["success"] else "failure"


def _route_after_trial(state: GraphState) -> Literal["retry", "false_decision", "end"]:
    if state.get("done"):
        return "end"
    if state.get("needs_false_decision"):
        return "false_decision"
    return "retry"


def _route_after_false_decision(state: GraphState) -> Literal["restrategy", "end"]:
    if state.get("done"):
        return "end"
    return "restrategy"
