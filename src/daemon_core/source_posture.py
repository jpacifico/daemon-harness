"""Shared evidence posture contract for post-turn memory judges."""

SOURCE_POSTURE_PROMPT = """- `direct_fact`: the latest message itself establishes a fact as true now. A preamble recalling an earlier statement does not change this posture if the message repeats the fact's content, for example “I already told you: A is B”.
- `reported_memory_claim`: the message asks to retrieve or recheck what may have been said or remembered before, without itself reaffirming the fact; its content remains absent, interrogative or only attributed to the earlier statement.
- `none`: the message neither establishes nor reports a memory fact.
- Judge the asserted content, not its preamble. An earlier statement that is merely reported is never, on its own, new evidence of the fact it may contain."""


__all__ = ["SOURCE_POSTURE_PROMPT"]
