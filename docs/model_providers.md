# Model providers

A model is offered for a daemon only when the whole daemon works well with it:
its replies, what it remembers, and what it keeps track of between messages
(what is going on right now, what is still pending, what has been settled). A
model that only gets part of this right is not offered, with or without a
warning.

| Provider | Model tested | Tested | Offered | Why |
| --- | --- | --- | --- | --- |
| ChatGPT subscription | GPT-6 Astra | 2026-10-03, 2026-10-24 | Yes | The reference daemon runs entirely on it since 2026-10-03, every day. A new daemon gives it the same inputs as the reference. |
| ChatGPT subscription | GPT-6.1 Sol | 2026-10-29 | Offered until fully tested | Kept track of the situation as well as the reference in four test conversations. Its replies and memory on a real daemon are not tested yet. |
| ChatGPT subscription | GPT-6.1 Terra | 2026-10-29 | Not yet | Kept track of the situation as well as the reference in four test conversations, three times faster. Its replies and memory on a real daemon are not tested yet. |
| ChatGPT subscription | GPT-5.6 Terra | 2026-08-31 | No | Withdrawn on 2026-10-29: Companion warned that the experience may be degraded with it, and a warned model is not offered. |
| Mistral (API key) | Mistral Large 4 | 2026-10-29, again after its public release the same evening | No | Loses track of what is going on in the conversation, and records unconfirmed things as facts. See below. |
| OpenAI (API key) | — | Not tested | To decide | Same model family as the subscription, but reached through another route, with other settings. Not yet tested. |
| Anthropic (API key) | Claude Opus 5.5 | 2026-10-29, 2026-10-30 | Only this model | On a real test daemon the maintainer judged its replies relevant and the daemon at the expected level overall. Known gap below. The other Claude models are not offered. |
| SpaceXSI (API key) | Grok 4.7 | 2026-10-29 | No | Its replies are shorter, drier and slower than the reference's. See below. |

Setup recommends the ChatGPT subscription with GPT-6 Astra, on both hosts.

## Claude Opus 5.5: known gap

Opus replies are longer than Astra's; the maintainer keeps them as they are
(measure). On the maintainer's test
daemon, its memory radar did not keep a one-off exception to a daily routine
slipped into a sign-off (a sleep-in the next morning), in five replays of five,
where Astra kept it each time; when the maintainer later corrected the daemon, it
kept the correction each time
(report). The instruction is
not adapted to that case.

## Why Mistral Large 4 is not offered

After each message, the daemon updates its picture of the conversation: what
is going on, what is still pending, what has been settled. With Mistral Large 4
this picture is never updated, for two reasons:

- **Too slow.** Each update takes 1 to 7 minutes, with Mistral's maximum
  reasoning, which it uses by default. The daemon stops waiting after 2
  minutes. With reasoning turned off it answers in under 30 seconds, but makes
  more mistakes.
- **Facts it cannot back.** Even when given all the time it needs, it records as
  certain things nobody confirmed, such as the assistant's own suggestions, or
  gives inconsistent sources. The daemon then discards the whole update, on
  purpose: it must never turn a guess into a fact.

On the maintainer's test daemon it failed in both settings, and with reasoning
turned off it also failed two of four test conversations. Its replies and what
it remembers worked, but a daemon that loses track of the situation is not the
complete experience. It also keeps retrying in the background after each
message, which can cost usage on the Mistral account.

## Why Grok 4.7 (SpaceXSI) is not offered

Measured on 2026-10-29, with the same daemon, the same persona and the same
seven-message conversation sent to Grok 4.7 and to GPT-6 Astra:

- **Shorter replies.** After the three welcome replies, Grok wrote half as
  much as Astra with its usual reasoning setting (809 characters against
  1 706), and a third as much with maximum reasoning (524). Its replies are
  clipped statements ("Oh. Ta sœur, enceinte. C'est pour quand ?") where
  Astra reacts to the person.
- **Slower.** 19 seconds per reply on average, 29 with maximum reasoning,
  against 8 for Astra.
- **On a real test daemon** the maintainer found its replies sober and impersonal.

On that test daemon, the daemon's persona and context reached it in full
(checked from the request sizes, about 12 000 tokens per message).

Before a new provider is offered (for example OpenRouter), check its
accepted reasoning settings, how long it takes on the daemon's work between
messages, the format of its answers, and run the same tests.

Reports: Grok 4.7 and Astra replies,
GPT-6.1 Sol and Terra,
Mistral Large 4,
GPT-5.6 Terra,
GPT-6 Sol and Terra replies,
reference parity.
