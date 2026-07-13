"""M&A intelligence (epic #26): acquisition research + propose→review→commit.

Mirrors People Intelligence (``app.agents.people``): a research step produces
raw, UNTRUSTED structured output; a pure build step deterministically drops any
deal fact not backed by a citation (the amount guardrail is enforced here, not by
trusting the model); a durable ``acquisition_proposal`` job stages the reviewable
facts; and only an explicit commit writes ``(acquirer)-[:ACQUIRED]->(target)`` to
the graph. No direct-write path — human-in-the-loop preserved.
"""
