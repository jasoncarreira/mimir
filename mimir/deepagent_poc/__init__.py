"""mimir + deepagents PoC.

End-to-end smoke that proves we can:
1. Wrap MemoryClient.query as a LangChain tool
2. Construct a deepagent with one tool + a system prompt
3. Run a LongMemEval question through it
4. Extract events from LangChain messages → mimir's TurnRecord schema
5. Write turns.jsonl in the existing mimir shape

If this lands at ≥80% accuracy on a 5-q slice of LongMemEval-S
single-session-user (saga 81.6 reference hits 94% here), retrieval +
reader parity is proven and the full migration is justified.

Not production code — exploratory only. Lives outside the canonical
``mimir/`` namespace conceptually but co-locates here for import ease.
"""
