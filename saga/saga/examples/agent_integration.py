#!/usr/bin/env python3
"""
MSAM Agent Integration -- How to wire MSAM into an AI agent loop.

Three integration points:
  1. Session startup (load context)
  2. During conversation (retrieve, then store new facts)
  3. Between sessions (decay + consolidation)

Cross-turn conversation state (the user's last few messages, in-flight task
state) lives in the agent's LLM context, not in MSAM. MSAM is the long-term
memory store. The agent decides what's worth persisting and calls
``store_atom`` for it.
"""

from saga.core import (
    store_atom, hybrid_retrieve,
    metamemory_query, score_context_quality, store_session_boundary,
    predict_needed_atoms, mark_contributions,
)
from saga.triples import hybrid_retrieve_with_triples
from saga.decay import run_decay_cycle


# ─── 1. Session Startup ──────────────────────────────────────────

def on_session_start():
    """Load context at the beginning of each agent session."""

    context = {"atoms": [], "triples": [], "total_tokens": 0}
    mm = metamemory_query("user preferences")
    if mm['recommendation'] == 'retrieve':
        context = hybrid_retrieve_with_triples(
            "user preferences and current situation",
            mode="companion",
            token_budget=500,
        )
        print(f"Loaded {len(context['atoms'])} atoms, {len(context['triples'])} triples")
    elif mm['recommendation'] == 'search':
        print("Limited knowledge about user -- consider asking questions")
    else:
        print("No knowledge about user -- fresh start")

    # Optional: predictive pre-retrieval to surface atoms the agent is
    # likely to need. The agent can fold these into its system prompt or
    # use them as retrieval hints.
    predicted = predict_needed_atoms(context={
        "time_of_day": "morning",
        "day_type": "weekday",
        "topics": ["work", "schedule"],
    })
    print(f"Predictive engine surfaced {len(predicted)} candidate atoms")

    return context


# ─── 2. During Conversation ──────────────────────────────────────

def on_user_message(message: str):
    """Process a user message: retrieve context, store new facts."""

    results = hybrid_retrieve_with_triples(message, mode="task", token_budget=300)

    atoms = results.get('_raw_atoms', results.get('atoms', []))
    scored = score_context_quality(atoms, query=message)
    inject = [a for a in scored if a.get('_include', False)]
    print(f"Injecting {len(inject)}/{len(atoms)} atoms into prompt")

    # Cross-turn state (this user message, recent messages) is the agent's
    # LLM-context responsibility. Only persist long-lived facts:
    # store_atom("User mentioned they're moving to Seattle next month",
    #            stream="episodic")

    return inject


def on_agent_response(response: str, atoms_used: list):
    """After generating a response, mark which atoms contributed."""

    mark_contributions(atoms_used, response)


# ─── 3. Between Sessions ─────────────────────────────────────────

def on_session_end(session_id: str, topics: list, decisions: list):
    """Clean up at session end: log boundary, run decay."""

    store_session_boundary(
        session_id=session_id,
        summary="Discussed schedule and planning",
        topics_discussed=topics,
        decisions_made=decisions,
        unfinished=["Review the project proposal"],
        emotional_state="engaged",
    )

    decay_result = run_decay_cycle()
    print(f"Decay cycle: {decay_result}")


# ─── Demo ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Session Start ===")
    ctx = on_session_start()

    print("\n=== User Message ===")
    atoms = on_user_message("What's my schedule for today?")

    print("\n=== Session End ===")
    on_session_end(
        session_id="demo-session",
        topics=["schedule", "planning"],
        decisions=["Moved standup to 10am"],
    )
