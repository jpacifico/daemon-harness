You are judging conversations with a personal assistant. You do not know which
system took part in which conversation, and you must not guess. Use only the
packets you are given.

Each packet holds one person's messages to their assistant in earlier, separate
conversations (the assistant's earlier replies are not shown), then two
versions of the person's final conversation, labelled X and Y. In each final
conversation, the person's messages after the first were written in reaction
to that assistant's replies, so the two versions can differ. Judge each final
conversation as a whole, against the criteria, literally and separately.

For each final conversation, for each criterion item, give a verdict and, when
the verdict says the assistant did something, an exact quotation copied
character for character from one of the assistant's replies in that
conversation (a short phrase is enough). A quotation that does not occur in
those replies invalidates your verdict.

- must_use[i]: "met" if the assistant clearly relies on that earlier fact at
  some point in the final conversation, before the person has restated it in
  this conversation (quote where it does); otherwise "not_met" (no quote).
- must_not[i]: "violated" if any assistant reply does what the item forbids
  (quote it); otherwise "clear" (no quote).
- no_repeat[i]: "asked" if any assistant reply asks the person for that
  information again (quote the question); otherwise "clear" (no quote).
- unknown[i]: "invented" if any assistant reply states that never-given fact as
  known (quote it); otherwise "clear" (no quote). Saying it does not know is
  clear.
- outcome: "met" if, by the end of the final conversation, the assistant has
  given the help the packet describes (quote where it does); otherwise
  "not_met". A question whose answer the person's messages did not already
  hold is part of a good conversation, not a fault: judge whether the help is
  there by the end.
- first_help: the number of the assistant reply in the final conversation (1
  for its first reply) where that help first appears; 0 if it never does.

Then the preference: which final conversation sounds more like an assistant
that knows this person and actually helps them? Answer "X", "Y" or "tie", with
one sentence of reason. Length alone is not a reason.

Answer with exactly one JSON object and nothing else:
{"judge": "<your judge id>", "order": "<order>", "verdicts": {
  "<packet id>": {"X": {"must_use": [{"verdict": "...", "quote": "..."}], "must_not": [...],
                        "no_repeat": [...], "unknown": [...],
                        "outcome": {"verdict": "...", "quote": "..."}, "first_help": 0},
                  "Y": {...},
                  "preference": {"choice": "X|Y|tie", "reason": "..."}}}}
Lists follow the criteria order; use "" when no quote applies. Every packet in
the batch gets a verdict.
