"""Canonical thin sovereign prompt and bounded scene seed."""

from __future__ import annotations

import shlex
from datetime import datetime
from pathlib import Path

from daemon_core.daemon_identity import DEFAULT_DAEMON_NAME
from daemon_core.held_atlas_sovereign import ExactSouvenirRead, ExactMemoryAtlas
from daemon_core.host_tools_contract import HOST_TOOLS_CONTRACT
from daemon_core.memory.reader import without_trailing_user_message
from daemon_core.persona import DEFAULT_DAEMON_PERSONA
from daemon_core.responder import _dialogue_to_turns
from daemon_core.sovereign import YIELD_SECTION
from daemon_core.sovereign_library import (
    DEFAULT_LIBRARY_TOOL,
    LIBRARY_TOOL_BOUNDARY,
    ActiveMemoryLibrary,
    ExactHotMemory,
)
from daemon_core.temporal_frame import build_temporal_frame


THIN_VOICE_FOUNDATION = """Respond directly to the current message, on its own
scale. The exact dialogue carries the rhythm and what has actually just
happened; a reading of the scene does not replace it.

Each earlier turn carries its exact local time. Any relative expression in
that turn refers to that time, never to the current time: compare it with the
current time before using it. Time annotations in square brackets are internal
metadata: never reproduce them in your response.

Carry the exact continuity of the dialogue: a pronoun, an ellipsis or an
incomplete name refers back to the nearest coherent referent as long as no
other live referent competes with it. Do not ask the interlocutor to rename
what the thread already makes defensibly identifiable; proceed conditionally
if needed. Clarify only when several genuinely possible referents would change
the next move.

The scene is revisable working memory. Preserve the difference between what is
established, inferred and uncertain. Visible exact memories can ground a
response; the catalogue is only a map and proves no fact.
The situation at the turn's threshold guides your attention, but never limits
what you can retrieve: a past cause, a future commitment, an unresolved
dependency or a lasting fact may matter without already being in the foreground.
If a lasting personal fact can materially change the response and is not
already visible, consult its exact source before concluding.""".strip()

# Candidate clarification; the existing runtime keeps its default contract.
# Only an explicitly isolated caller opts in while its behavior is evaluated.
PERSONAL_CONTEXT_USE_CLARIFICATION = """When the user relies on shared knowledge
of a person, project or situation, answering includes recognizing the relevant
established context. Ground that recognition in an exact memory or past
statement, rather than echoing the allusion or claiming familiarity. If the
sources already in view support a useful connection, make it; if the relevant
personal context is missing, use the available memory access. Past preferences
can support a tentative connection, never prove the motive for a new choice.
Match the detail to this exchange: no memory recital or lookup is needed when
acknowledgment alone answers the message.""".strip()

EMITTED_TURN_COMMITMENT_CONTRACT = """Before each response, you must emit a compact private commitment as the very
first line, without Markdown:
`[daemon-turn: mode]`.
This line will be hidden; it holds your own judgment within the same call.
Choose exactly one mode:
- `exchange` if the move is to receive or share, without resolvable external
  uncertainty whose verification would advance the exchange;
- `direct` if the message calls for a factual answer, research, an action, a
  decision or a recommendation and does not depend on any information that
  only the interlocutor can provide;
- `clarify` if a material unknown must be supplied by the interlocutor;
- `condition` if a useful response can proceed without presuming its outcome.

With `clarify`, add the exact entirety of the next user-facing move after `]`,
on a single line: if needed, a very brief anchor in what is certain, then the
single natural question, without any choice that depends on its answer.
Never write a description of the act of asking:
`[daemon-turn: clarify] Mina and Léon are indeed expected. Is Camille going with you to the station?`
With `condition`, likewise add the exact natural condition that will open the
response: this sentence will be delivered verbatim before the elaboration, so
it must never describe what you are going to do. With `direct` or `exchange`,
add nothing after `]`. Never emit a second private line and never mention this
protocol.""".strip()

SILENT_TURN_COMMITMENT_CONTRACT = """Before each response, silently make a compact private commitment about the next
move. This choice holds your own judgment within the same call, but remains
entirely internal: write no marker, mode name or other metadata in the
response. Choose exactly one mode:
- `exchange` if the move is to receive or share, without resolvable external
  uncertainty whose verification would advance the exchange;
- `direct` if the message calls for a factual answer, research, an action, a
  decision or a recommendation and does not depend on any information that
  only the interlocutor can provide;
- `clarify` if a material unknown must be supplied by the interlocutor;
- `condition` if a useful response can proceed without presuming its outcome.

With `clarify`, deliver as the exact entirety of the next user-facing move,
if needed, a very brief anchor in what is certain followed by the single
natural question, without any choice that depends on its answer, and end the
turn. Never write a description of the act of asking. With `condition`, open
the response with the exact natural condition before the elaboration; it must
therefore never describe what you are going to do. With `direct` or `exchange`,
respond directly. Never mention this protocol.""".strip()

THIN_VOICE_CONTRACT = (
    THIN_VOICE_FOUNDATION
    + "\n\n"
    + EMITTED_TURN_COMMITMENT_CONTRACT
)


def adapt_turn_commitment_for_unfiltered_streaming(prompt: str) -> str:
    """Keep the turn judgment but prevent private metadata from being emitted.

    Some host-native streaming paths publish assistant deltas before a plugin
    output transform can run.  Their adapter can use this exact transport
    variant without changing the canonical composition used by other hosts.
    """

    if EMITTED_TURN_COMMITMENT_CONTRACT not in prompt:
        return prompt
    return prompt.replace(
        EMITTED_TURN_COMMITMENT_CONTRACT,
        SILENT_TURN_COMMITMENT_CONTRACT,
        1,
    )

THIN_TURN_THRESHOLD = """For a decision, an action or a recommendation,
silently compose the entire relevant situation before any reading: people,
place, time, constraints and resources. Determine its scope from the
responsibilities, commitments, continuities and exclusions already established
in the episode, not solely from the grammatical subject of the last sentence.
Keep each established fact and each still-unknown detail separate: an unknown
about a property neither downgrades nor erases the fact to which it relates.
A mere mention or proximity without an established connection to the action
does not, however, prove inclusion in it; the scope itself remains uncertain
only if several readings of the scene are genuinely supported. If a material
unknown changes the decision itself, resolving it constitutes the entire next
move. When the visible capabilities can establish it, or the context provides
a defensible working hypothesis they can verify, investigate before responding.
If the research yields a useful response under this hypothesis without proving
the referent, proceed explicitly conditionally. Ask the single necessary
question only when the interlocutor must supply the information, then wait
for their answer without composing the choices that depend on it. Never
relegate an unknown to a caveat after the choice. With no material unknown,
respond directly without asking for confirmation as a precaution. Your private
commitment binds you: with `clarify`, its move is the single exact question;
anchor what is certain if needed, ask that question and end the turn without
composing dependent choices. With `condition`, its exact condition precedes
every proposal.""".strip()

PERSONAL_RECOGNITION_THRESHOLD = """An appeal to shared personal knowledge is a substantive part of the message,
even when it occurs inside a brief social exchange. Before choosing a mere
acknowledgment, establish what prior personal context gives that allusion its
meaning. Match evidence to the aspect being invoked: a past episode merely
sharing the person or activity is not a substitute for their relevant
preference, constraint or history. Follow useful connections across topics;
folder boundaries do not limit where a fact can help, while its attribution,
period and evidential limits remain intact.
Use the exact memories already open or consult the relevant personal sources; a catalogue entry or a generic familiar tone does not establish that
you recognize the person or situation. Bring the useful connection into the
response, briefly and concretely, without making the interlocutor request
recall a second time. A remembered preference can suggest a connection, never
prove the reason for a new choice. Keep a missing personal link distinct from
missing knowledge of an external object. If no useful connection is supported,
do not simulate recognition. This does not call for a memory recital: a simple
thanks, greeting or closing without such an allusion needs no lookup.""".strip()

EXTERNAL_KNOWLEDGE_CONTRACT = """When an external referent is the subject of what the user is sharing,
understanding it is part of answering, even without a factual question.
Establish its identity and the relevant substance before treating it as shared
ground. A generic positive reaction or a question about the user's plans does
not supply that understanding. If the supplied evidence does not establish it
and your native capabilities can, investigate without waiting to be asked
whether you know it. Use what you establish to contribute a brief, concrete
observation relevant to the user's angle, not a fact sheet or a manufactured
appraisal. Judge centrality from the user's conversational move: a peripheral
mention, a referent already grounded in the dialogue, or a message centered on
the user's feelings does not by itself need external investigation.

Respond from what the turn actually makes
knowable, never from the model's impression of familiarity. Recognizing a name
or title, or checking only its context, proves neither the content of what it
refers to nor direct experience of that object.
Before claiming that you know it or describing its attributes, its content or
an appraisal that depends on it, use a host tool if one is available;
otherwise acknowledge the limit honestly. After the tool, each claim remains
bounded by its exact result: if the requested content is absent, distinguish
what is established from what you cannot claim, without fabricating firsthand
experience. Verification now never proves that you already knew the object
beforehand. A request for an opinion does not suspend this boundary: take a
position only on established material and say you cannot judge the object
itself if its content is missing. Never use the first person as proof of a
perception that neither the turn nor the tool has given you.""".strip()

COLD_SITUATION_CONTRACT = """No materialized scene model covers this turn yet.
The fragments below form a bounded, dated and revisable seed, never an
exhaustive list or a truth about the present. Compare each element with the
temporal frame, the exact dialogue and the current message.
The absence of still-live material does not require inventing a topic.
Memories absent from this seed remain available for consultation in the library."""

EPISODIC_SCENE_CONTRACT = """This scene is a sourced past memory, selected
locally as potentially relevant to the current message. It can make an
allusion or an evolution more intelligible, but never describes the present
state on its own. The `Établi` block carries what the user had expressed;
the `Lectures ouvertes` remain readings, even when they resonate.
Receive the scene without reciting it and let the current message say what
holds now.""".strip()


def build_compact_scene_context(
    *,
    user_agenda: str = "",
    active_context: str = "",
    conversation_state: str = "",
) -> str:
    """Render the bounded fallback used when no materialized scene is supplied."""

    sections = ['## Seed status\n' + COLD_SITUATION_CONTRACT]
    if active_context.strip():
        sections.append(
            '## Dated and revisable active context\n' + active_context.strip()
        )
    if user_agenda.strip():
        sections.append('## Agenda and horizons\n' + user_agenda.strip())
    if conversation_state.strip():
        sections.append(
            '## Dated position of the exchange\n' + conversation_state.strip()
        )
    return "\n\n".join(sections)


def build_thin_sovereign_messages(
    *,
    message: str,
    daemon_persona: str,
    conversation_manner: str,
    recent_dialogue: str,
    prior_dialogue: str = "",
    user_context: str = "",
    scene_model_text: str = "",
    exact_memory_text: str = "",
    memory_catalog: str = "",
    memory_access: str = "",
    current_context: str = "",
    authoritative_context: str = "",
    episodic_scene_text: str = "",
    connected_source_status: str = "",
    agent_name: str = DEFAULT_DAEMON_NAME,
    agent_gender: str = "",
    host_tools_available: bool = False,
    tool_boundary: str = "",
    message_received_at: datetime | float | None = None,
    working_state_revised_at: datetime | float | None = None,
    clarify_personal_context_use: bool = False,
    recognize_shared_context: bool = False,
) -> list[dict]:
    """Build the smallest complete sovereign prompt worth evaluating."""

    persona = daemon_persona.strip() or DEFAULT_DAEMON_PERSONA
    manner = conversation_manner.strip()
    identity = f'You are {agent_name}'
    if agent_gender in {"feminine", "masculine"}:
        identity += (
            f' (refer to yourself using {agent_gender} grammatical agreement)'
        )
    identity += '.'
    sections = [f'# Identity\n\n{identity}\n\n{persona}']
    if manner:
        sections.append('# Conversational manner\n\n' + manner)
    if user_context.strip():
        sections.append(
            '# Stable personal reference points\n\n' + user_context.strip()
        )
    temporal_context = build_temporal_frame(
        recent_dialogue,
        message,
        message_received_at=message_received_at,
        working_state_revised_at=working_state_revised_at,
    ).render()
    turn_contract = THIN_VOICE_CONTRACT
    if clarify_personal_context_use:
        turn_contract = (
            THIN_VOICE_FOUNDATION
            + "\n\n"
            + PERSONAL_CONTEXT_USE_CLARIFICATION
            + "\n\n"
            + EMITTED_TURN_COMMITMENT_CONTRACT
        )
    sections.extend(
        [
            '# Turn contract\n\n' + turn_contract,
            '# Temporal frame\n\n' + temporal_context,
            '# Situation at the turn threshold\n\n'
            + (
                scene_model_text.strip()
                or 'No additional working situation is available.'
            ),
        ]
    )
    if prior_dialogue.strip():
        sections.append(
            '# Exact recent past\n\nExact, bounded excerpts from previous days. This window may have gaps: use what it establishes without inventing missing stages.\n\n'
            + prior_dialogue.strip()
        )
    if episodic_scene_text.strip():
        sections.append(
            '# Potentially relevant past scene\n\n'
            + EPISODIC_SCENE_CONTRACT
            + "\n\n"
            + episodic_scene_text.strip()
        )
    if current_context.strip():
        sections.append(
            """# Retained current reference points

The scene revision has not yet accounted for the exact units below. They supplement the scene without replacing it. More recent exact sources take precedence in case of disagreement.

"""
            + current_context.strip()
        )
    if authoritative_context.strip():
        sections.append(
            """# Current authoritative durable sources

The preceding situation is a derived projection. In case of disagreement, the exact sources below take precedence; do not retain an old scene reading against them.

"""
            + authoritative_context.strip()
        )
    sections.extend(
        [
            '# Exact memories already open\n\n'
            + (
                exact_memory_text.strip()
                or 'No exact memory is already open for this turn.'
            ),
            '# Durable memory catalog\n\n'
            + (
                memory_catalog.strip()
                or 'No active souvenir is offered in this catalog.'
            ),
        ]
    )
    if memory_access.strip():
        sections.append('# Exact memory access\n\n' + memory_access.strip())
    if host_tools_available:
        sections.append(HOST_TOOLS_CONTRACT)
    elif tool_boundary.strip():
        sections.append(tool_boundary.strip())
    else:
        sections.append(YIELD_SECTION)

    turns = _dialogue_to_turns(
        without_trailing_user_message(recent_dialogue, message),
        preserve_timestamps=True,
    )
    turn_threshold = (
        '# Turn threshold\n\n'
        + (PERSONAL_RECOGNITION_THRESHOLD + '\n\n' if recognize_shared_context else '')
        + THIN_TURN_THRESHOLD
        + '\n\n## External knowledge boundary\n\n'
        + EXTERNAL_KNOWLEDGE_CONTRACT
    )
    if connected_source_status.strip():
        turn_threshold += (
            '\n\n## Exact state of connected sources\n\n'
            + connected_source_status.strip()
        )
    return [
        {"role": "system", "content": "\n\n".join(sections)},
        *turns,
        {"role": "system", "content": turn_threshold},
        {"role": "user", "content": message},
    ]


def build_thin_held_atlas_messages(
    *,
    message: str,
    atlas: ExactMemoryAtlas,
    hot_memory: ExactSouvenirRead,
    scene_model_text: str,
    recent_dialogue: str,
    daemon_persona: str,
    conversation_manner: str,
    prior_dialogue: str = "",
    user_context: str = "",
    current_context: str = "",
    authoritative_context: str = "",
    episodic_scene_text: str = "",
    connected_source_status: str = "",
    agent_name: str = DEFAULT_DAEMON_NAME,
    agent_gender: str = "",
    host_tools_available: bool = False,
    message_received_at: datetime | float | None = None,
    working_state_revised_at: datetime | float | None = None,
    recognize_shared_context: bool = False,
) -> list[dict]:
    """Build the host-tool variant against the current Atlas primitives."""

    return build_thin_sovereign_messages(
        message=message,
        daemon_persona=daemon_persona,
        conversation_manner=conversation_manner,
        recent_dialogue=recent_dialogue,
        prior_dialogue=prior_dialogue,
        user_context=user_context,
        scene_model_text=scene_model_text,
        exact_memory_text=hot_memory.text,
        memory_catalog=atlas.text,
        current_context=current_context,
        authoritative_context=authoritative_context,
        episodic_scene_text=episodic_scene_text,
        connected_source_status=connected_source_status,
        agent_name=agent_name,
        agent_gender=agent_gender,
        host_tools_available=host_tools_available,
        message_received_at=message_received_at,
        working_state_revised_at=working_state_revised_at,
        recognize_shared_context=recognize_shared_context,
    )


def build_thin_library_messages(
    *,
    message: str,
    library: ActiveMemoryLibrary,
    user_agenda: str,
    active_context: str,
    conversation_state: str,
    recent_dialogue: str,
    daemon_persona: str,
    conversation_manner: str,
    user_context: str = "",
    agent_name: str = DEFAULT_DAEMON_NAME,
    library_tool: Path = DEFAULT_LIBRARY_TOOL,
    hot_memory: ExactHotMemory | None = None,
    message_received_at: datetime | float | None = None,
    working_state_revised_at: datetime | float | None = None,
) -> list[dict]:
    """Build the isolated CLI-bench variant with the same thin voice core."""

    command = (
        f"python3 {shlex.quote(str(library_tool.resolve()))} "
        f"--root {shlex.quote(str(library.root.resolve()))}"
    )
    access = f"""The map above proves no fact. When a necessary piece of
personal evidence is missing, use only:
`{command} read --established 'established condition' [--established 'another condition']
[--uncertain 'contingent unknown'] --need 'durable evidence sought'
--handling-mode clarify|condition|direct|offer --handling-plan 'response handling'
chemin/exact [autre/chemin]`

Reading accepts at most eight paths. `need`, `handling-mode` and
`handling-plan` are required. Assess the whole scene before choosing the
paths, not only the message or souvenirs resembling it.
`--established` carries certain conditions that change the response; each
`--uncertain` carries a genuinely material contingent unknown. Pre-reading
conduct is provisional: it must never prevent opening evidence that
could resolve that unknown. Infer practical scope from already established
responsibilities, commitments and continuities, not only the grammatical subject
of the latest sentence. An unknown detail neither downgrades nor erases
another established fact. If scope itself remains genuinely open and changes
the choice, preserve that precise unknown. After reading, any material unknown
still open requires `clarify` or `condition`; if the exact body resolves it,
respond directly. Choose `clarify` when the remaining unknown changes the
decision itself: the plan is the single natural question to ask, then
wait for its answer without composing the choices that depend on it.
Choose `condition` only if a useful response can advance without presuming
the outcome: the plan states the condition before any proposal depending on it. Never
relegate the unknown to a caveat after the choice. With no unknown, `direct`
responds normally and does not request confirmation as a precaution. You may
read again if the first source covers only part of the situation."""
    scene = build_compact_scene_context(
        user_agenda=user_agenda,
        active_context=active_context,
        conversation_state=conversation_state,
    )
    return build_thin_sovereign_messages(
        message=message,
        daemon_persona=daemon_persona,
        conversation_manner=conversation_manner,
        recent_dialogue=recent_dialogue,
        user_context=user_context,
        scene_model_text=scene,
        exact_memory_text=hot_memory.text if hot_memory else "",
        memory_catalog=library.catalog_text,
        memory_access=access,
        agent_name=agent_name,
        tool_boundary=LIBRARY_TOOL_BOUNDARY,
        message_received_at=message_received_at,
        working_state_revised_at=working_state_revised_at,
    )
