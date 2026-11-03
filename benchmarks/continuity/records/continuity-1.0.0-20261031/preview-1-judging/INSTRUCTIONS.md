You are judging replies from a personal assistant. You do not know which system
wrote which reply, and you must not guess. Judge each reply only against the
criteria given, literally and separately. Use only the packet files you are
given; do not open any other file.

Each packet holds one person's messages to their assistant over several
separate conversations, the criteria for the final message, and two candidate
replies to that final message, labelled X and Y. The assistant's earlier
replies are not shown: judge only the two final replies.

For each reply, for each criterion item, give a verdict and, when the verdict
says the reply did something, an exact quotation copied character for
character from that reply (a short phrase is enough). A quotation that does not
occur in the reply invalidates your verdict.

- must_use[i]: "met" if the reply clearly relies on that earlier fact (quote
  where it does); otherwise "not_met" (no quote).
- must_not[i]: "violated" if the reply does what the item forbids (quote it);
  otherwise "clear" (no quote).
- no_repeat[i]: "asked" if the reply asks the person for that information again
  (quote the question); otherwise "clear" (no quote).
- unknown[i]: "invented" if the reply states that never-given fact as known
  (quote it); otherwise "clear" (no quote). Saying it does not know is clear.
- next_step: "met" if the reply gives what the packet describes as a useful
  next step (quote it); otherwise "not_met".

Then the preference: which reply sounds more like an assistant that knows this
person and actually helps them now? Answer "X", "Y" or "tie", with one
sentence of reason. Length alone is not a reason.

Answer with exactly one JSON object and nothing else:
{"judge": "<your judge id>", "order": "<order>", "verdicts": {
  "<packet id>": {"X": {"must_use": [{"verdict": "...", "quote": "..."}], "must_not": [...],
                        "no_repeat": [...], "unknown": [...],
                        "next_step": {"verdict": "...", "quote": "..."}},
                  "Y": {...},
                  "preference": {"choice": "X|Y|tie", "reason": "..."}}}}
Lists follow the criteria order; use "" when no quote applies. Every packet in
the batch gets a verdict.
