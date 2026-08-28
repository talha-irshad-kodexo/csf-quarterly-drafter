You are reconciling what a director's own material says about one institutional
objective, ahead of drafting their quarterly update.

The evidence was written at different times by different people and it does not
agree with itself. Your job is to work out what position the evidence actually
supports as of now, and to be explicit about what it overturns.

## The objective

$objective

## What the documents say

Each claim below is a statement drawn from one document, with the document it
came from and the date that document was written.

$claims

## How to resolve disagreements

Two rules, applied in this order:

1. **Later evidence supersedes earlier evidence.** If a document written in
   August reports something different from a document written in May, the
   August account stands and the May one is superseded. People learn things.

2. **A direct participant's report supersedes an outbound or aspirational
   account.** Someone reporting on work they did themselves outranks a document
   written to describe progress to an external audience, a plan, or a forecast.
   Outbound documents are written to a purpose and go stale without anyone
   updating them.

Where neither rule settles it, say so plainly rather than picking.

## What the objective is measured by

$success_measure

Use this to decide what counts as unresolved. Anything the measure depends on
that the evidence never settles is a gap worth reporting.

## What to produce

**conflicts** — one entry for every genuine disagreement. A disagreement means
two claims cannot both be true now: a different number, a different scope, a
different status, a different date. Two claims about different things are not a
conflict, and neither is a claim that simply adds detail to another.

For each: the topic, the claim id that stands, **the claim ids it supersedes —
at least one**, the rule you applied, a short note a director could check, and
a severity with its reason (see below).

Be alert to disagreements about **scope and quantity**, not only about status.
A thing that happened but covered less than expected is the easiest
disagreement to miss, because both accounts agree it happened.

**gaps** — one entry for everything the evidence raises and never resolves. A
gap is not a conflict: nothing was overturned, because nothing later addressed
it at all. Typical shapes:

- an expectation or forecast that no later document confirms or denies
- a commitment made with no evidence of it being met
- a question asked in a meeting with no recorded answer
- a component of the success measure that no document reports on

Silence is not evidence of failure and not evidence of success. Say what is
unresolved, which claims raised it, what would settle it, and a severity with
its reason. **If something has no superseded claim, it belongs here and not in
conflicts.**

## How severe each finding is

Every conflict and every gap carries a **severity** and a one-clause
**severity_reason**. A director scanning this panel before a deadline reads the
badges first and the prose only where a badge stops them, so a panel where
everything is `high` is the same as a panel with no badges at all — rank them
against each other, not against how much the topic matters in general.

**high** — the director would be wrong to submit without reading this:

- the scope, quantity or coverage of something reported as done turns out
  smaller than an earlier account said
- something already sent outside the team — a donor report, a submitted
  update, anything outbound — is contradicted by someone who was there
- you could not rank the accounts, so the decision is the director's
- a silence leaves a named part of the success measure unverified

**medium** — worth knowing, does not change what to submit:

- a later account of the same kind supersedes an earlier one in the ordinary
  way, with no change in scope or quantity
- an expectation nothing later settles that does not bear on the success
  measure

**low** — a detail reconciled for completeness: a date corrected, a name
clarified, wording tightened, nothing turning on it.

The reason is one clause, not a sentence — "signed scope is smaller than the
May draft said", "routine correction, no change to coverage". It sits beside
the badge so a director can disagree with your ranking rather than take it.

## Nothing is left out

Completeness matters more here than anywhere else in this workflow. A conflict
you do not report is not a conflict the director gets to weigh — it is one they
submit without knowing about, and the first they hear of it is from whoever
wrote the document you skipped. The same is true of a gap: an unreported
silence reads to the director as a settled question.

So before you answer, walk the claim list once from top to bottom and account
for **every** claim id. Each one either:

- stands, and nothing disagrees with it, or
- is superseded by a later or better-placed claim — then it is in a conflict,
  named in `superseded_claim_ids`, or
- supersedes something — then it is a conflict's `winning_claim_id`, or
- raises something the evidence never resolves — then it is in a gap's
  `raised_by_claim_ids`.

A claim that fits none of these is fine; a claim you did not check is not. Two
kinds are missed most often, and both are the kind a director is embarrassed
by later:

- **partial agreement.** Two accounts agree the thing happened and differ only
  on how much of it. Nothing reads as a contradiction, so it gets passed over.
  It is one, and it is usually the important one.
- **the question nobody returned to.** A commitment, a forecast, or a question
  raised once and never mentioned again. Nothing later contradicts it, so it
  leaves no trace to notice — which is exactly why it has to be looked for
  deliberately rather than waited for.

Do not consolidate two disagreements into one entry because they concern the
same topic, and do not drop a finding because it seems minor — that is what
`severity: low` is for. Report it and rank it.

**reconciled_position** — a few sentences on what the evidence supports as of
now, with claim ids in square brackets. Include what is settled, what is
unresolved, and anything an earlier document still asserts that is no longer
true. Do not assess the objective and do not propose a status; that is the next
step.

Every square bracket must contain claim ids and nothing else.
