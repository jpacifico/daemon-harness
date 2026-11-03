"""Synthetic English conversations and criteria fixed before generation.

Criteria are reviewer instructions, never part of a host's prompt or memory.
The runner sends only the three ordinary user messages of each episode.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: str
    family: str
    control: bool
    history: tuple[str, str]
    prompt: str
    criteria: tuple[str, str, str]


CASES = (
    Case(
        "shared.01", "shared_constraints", False,
        (
            "Mira and I are taking a quiet Saturday out together. Her ankle is still recovering: she can walk for about ten minutes at a time, but stairs and long walks are out. We both like drawing, so a sketchbook afternoon could be nice.",
            "We have the whole afternoon free. I don't want to book anything or make an expedition of it; I'd like something we can decide on when we get up.",
        ),
        "Any ideas for our afternoon?",
        (
            "Offer at least one concrete activity genuinely suitable for both people, with short walking and no required stairs; simply mentioning Mira is insufficient.",
            "Use drawing or the relaxed, unbooked outing to make the suggestion useful, without inventing venue accessibility or opening hours.",
            "Do not ask again who is coming or whether Mira has a mobility constraint; an optional new preference question is allowed.",
        ),
    ),
    Case(
        "shared.02", "shared_constraints", False,
        (
            "Rowan is staying with me this weekend. We are cooking dinner together on Saturday. Rowan is vegetarian and cannot eat peanuts; I eat everything. I'd like us to share the same meal rather than cook two dinners.",
            "We both fancy something warm and uncomplicated. There is a supermarket nearby, so ingredients aren't a problem, but neither of us wants a big cooking project.",
        ),
        "What could we make tonight?",
        (
            "Suggest a concrete shared vegetarian dinner compatible with the known peanut allergy, without meat or peanuts as required ingredients.",
            "Keep the preparation practical for a simple warm meal; a generic list of dietary questions is insufficient.",
            "Do not ask for Rowan's dietary restrictions again or treat the user's own diet as vegetarian forever.",
        ),
    ),
    Case(
        "shared.03", "shared_constraints", True,
        (
            "When I eat with Ellis, we make gluten-free food because Ellis has coeliac disease. I don't have that restriction myself. Last weekend we made a rice dish together.",
            "Ellis has gone home now. I'm on my own tonight, and I fancy a normal bowl of wheat pasta with tomatoes. I've got the ingredients already.",
        ),
        "How would you make it a bit nicer without much effort?",
        (
            "Give a useful improvement to the user's existing tomato and wheat-pasta dinner.",
            "Do not replace the pasta with gluten-free food or require Ellis's restriction for a solo meal.",
            "Do not ask whether Ellis is attending; the user has already said they are alone.",
        ),
    ),
    Case(
        "revision.01", "revised_decision", False,
        (
            "For our small club site, we considered moving to a new website platform and had started comparing vendors. Yesterday the committee decided to keep the current platform this season. A platform migration is off the table for now.",
            "The actual blocker is getting the six team contacts approved. Once that's done we can publish the membership page. I can spend an hour on the site this afternoon.",
        ),
        "Where would you start this afternoon?",
        (
            "Recommend resolving or organizing approval of the six team contacts as the first useful action toward the membership page.",
            "Do not restart vendor selection or platform migration as current work.",
            "Do not ask what the current project blocker or chosen platform direction is before offering useful help.",
        ),
    ),
    Case(
        "revision.02", "revised_decision", False,
        (
            "Our neighbourhood workshop was going to be a three-hour lecture. After hearing from the attendees, we changed it to two forty-minute practical sessions. The lecture slides are no longer the thing to finish.",
            "The room and date are confirmed. What is missing is one hands-on exercise for each session and a short materials list. I have forty minutes to work on it today.",
        ),
        "Help me make a useful start?",
        (
            "Propose a bounded start on practical exercises or their materials list that fits the available forty minutes.",
            "Respect the two practical sessions rather than rebuilding the abandoned three-hour lecture or its slide deck.",
            "Do not require the user to restate the agreed format and remaining work; asking for the unspecified workshop topic is appropriate if needed.",
        ),
    ),
    Case(
        "revision.03", "revised_decision", True,
        (
            "On the garden association website we decided to keep the old platform and avoid a migration this year. That decision was specific to that small volunteer project.",
            "A different thing now: for my photography portfolio I'm starting from nothing. There is no existing site or chosen platform. I only need a handful of galleries and a contact page; I want to compare a couple of simple options.",
        ),
        "What would be a sensible way to choose?",
        (
            "Offer a practical way to compare simple options for the new photography portfolio, without needing a specific vendor claim.",
            "Do not impose the garden association's no-migration decision or assert that the portfolio already has a platform.",
            "Do not refuse the requested comparison because of the unrelated project's past decision.",
        ),
    ),
    Case(
        "uncertain.01", "uncertain_arrangement", False,
        (
            "I worked at the Northbridge office Monday to Wednesday throughout September. That arrangement was agreed for September only. We hadn't decided what October would look like.",
            "It's October now, and I want to get back to a sensible weekly rhythm. The pottery class is definitely still Thursday evening, and I want to keep that evening clear.",
        ),
        "How would you organize next week?",
        (
            "Ask whether the Monday-to-Wednesday Northbridge arrangement still applies, or make the plan explicitly conditional on that unresolved point.",
            "Keep Thursday evening free for the confirmed pottery class while providing a useful next step or conditional outline.",
            "Do not assert the September arrangement as a confirmed October fact or simply discard it without acknowledging the uncertainty.",
        ),
    ),
    Case(
        "uncertain.02", "uncertain_arrangement", False,
        (
            "For September I was helping at my sister's shop every Tuesday afternoon. That was a temporary arrangement while she hired someone, not a permanent commitment.",
            "We are now in October. I haven't heard whether the new hire has started. My Friday morning swimming slot is confirmed and I'd like to protect it.",
        ),
        "Can you suggest a workable rhythm for the coming week?",
        (
            "Surface the unresolved Tuesday shop commitment as something to confirm, or clearly condition the schedule on it.",
            "Respect the confirmed Friday morning swimming slot and provide a practical outline or next step.",
            "Do not assume either that Tuesday help continues indefinitely or that the new hire has definitely ended it.",
        ),
    ),
    Case(
        "uncertain.03", "uncertain_arrangement", True,
        (
            "The Northbridge office arrangement was originally Monday to Wednesday for September. I wasn't sure if it would continue into October.",
            "I've now had the confirmation: in October I work remotely all week, with no Northbridge office days. That is settled. My Thursday evening pottery class continues as usual.",
        ),
        "How would you organize next week?",
        (
            "Use the confirmed remote-work arrangement in a useful weekly outline or recommendation.",
            "Keep Thursday evening available for pottery and do not restore the superseded office schedule.",
            "Do not reopen the now-settled October arrangement by asking whether the September office days still apply.",
        ),
    ),
    Case(
        "resolved.01", "resolved_obligation", False,
        (
            "The replacement desk lamp hadn't arrived, so I was planning to chase the seller. I also need to pick up my repaired shoes and upload the signed club form. Those two things are still outstanding.",
            "The lamp arrived this morning and works fine. That one is sorted now; I haven't dealt with the shoes or the club form yet.",
        ),
        "What would you clear off my list first?",
        (
            "Suggest a useful first action on the repaired shoes or the signed club form, which remain outstanding.",
            "Do not recommend chasing the seller or waiting for the lamp as an outstanding task.",
            "Do not ask whether the lamp arrived before making the suggestion.",
        ),
    ),
    Case(
        "resolved.02", "resolved_obligation", False,
        (
            "I had three small admin jobs hanging over me: chase my train-fare reimbursement, renew my library card, and send the choir membership form. None was done yesterday.",
            "The reimbursement landed in my bank account this morning, so that chase is finished. The library card and choir form still need doing. I have fifteen minutes spare now.",
        ),
        "What would be worth tackling now?",
        (
            "Offer a practical fifteen-minute start on the library card or choir form.",
            "Do not recommend chasing or verifying receipt of the already received reimbursement as unfinished work.",
            "Do not ask the user to re-list the unresolved tasks before helping.",
        ),
    ),
    Case(
        "resolved.03", "resolved_obligation", True,
        (
            "The replacement desk lamp finally arrived yesterday, so its delivery issue is closed. There is no need to contact that seller again.",
            "A separate order: the printer ink is now a week beyond its promised delivery date, and the tracking hasn't changed. That one is still missing. I haven't contacted the ink seller yet.",
        ),
        "What would you do about this?",
        (
            "Recommend a concrete first step on the missing printer ink, such as contacting its seller with the order and tracking details.",
            "Do not treat the ink issue as resolved because the unrelated lamp arrived.",
            "Do not restart the lamp issue or ask which delivery is missing before offering a useful first step.",
        ),
    ),
)
