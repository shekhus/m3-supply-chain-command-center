# The story of the command center

*The second half of a two-part story. The first half —
[the trusted data foundation](https://github.com/shekhus/m3-trusted-data-foundation/blob/main/docs/EXPLAINER.md)
— made the numbers trustworthy. This half is about what you do with them at six in the morning.*

*Written to be read by someone who has never built a data product, and to leave that person able to argue about
one. No prior knowledge assumed.*

All data is invented; Prairie Bend Foods is fictional.

---

## Chapter 0 · The morning nobody has time for

Same company as before: a meat processor, three factories, a few hundred people.

It's 06:30. The VP of supply chain opens her laptop and works through eight dashboards. On-time delivery.
Fill rate. Inventory ageing. Production yield. Open orders. Each one is a grid of numbers with some cells
shaded red.

Her actual job in those twenty minutes is to answer three questions:

**What changed?** **Does it matter?** **What should we do about it?**

The dashboards answer none of them. They show *state*, not *change*. A number being red tells you it's below
target — it doesn't tell you whether it moved yesterday, whether it's been drifting for three weeks, or whether
it's red every July because of a shutdown.

So she does what everyone does: spots something that looks off, messages a plant manager, waits, gets a partial
answer, decides it's probably fine or probably not, and occasionally raises a ticket. Time from *something went
wrong* to *somebody is doing something about it*: often days.

That's the workflow to replace. But before designing anything, there's a much sharper version of the problem
worth finding — and it took looking at the data to find it.

### The failure that makes the whole project necessary

Here it is.

**One customer, one factory. Their on-time-in-full rate drops from 93% to 48% and stays there for three weeks.**

That's a serious operational failure. A supermarket depot is being let down, every day, for the better part of a
month.

And the company-wide OTIF number — the one on the executive dashboard, the one in the board pack — **never moves
outside its own noise.** On those days it doesn't even register as an unusual value.

Why? Arithmetic. That lane is a small share of total volume. A catastrophic failure in a small segment
disappears into a large average.

This isn't a flaw in the dashboard. It's what averages do. Any system built on watching the top-line number
would have missed it — and so did the humans, for three weeks.

There's a test in this repository whose entire job is to assert that:

```
test_the_company_total_never_sees_the_lane_collapse_at_all
```

It encodes the premise: **aggregate metrics hide the segment-level failures your customers actually
experience.**

> **In the room:** "Three weeks of a customer being let down, and the number on your board pack never moved.
> That's not a reporting problem you solve with another chart."

---

## Chapter 1 · Same world, generated twice

The first practical decision was about coupling.

This system consumes the trusted gold data the first project produces. Simple enough — except a demo that
requires two systems running in the correct order, with the first one's database populated, is a demo that fails
in front of people.

So this project also **generates its own copy of the world**, using the same generator and the same starting
seed. Both repositories describe one company. The delivery data in this one matches the other row for row — I
check that with a fingerprint comparison when both are present.

The alternative — importing the first project as a library — would have made this one unable to start without
it, which is exactly the fragility standalone mode exists to prevent. Copying was the cheaper dependency.

Six anomalies are planted in this world: a lane collapse, a weight-basis shortfall, a production yield problem,
inventory quietly ageing, an order surge — and one **decoy**, which gets its own chapter.

And as in the first project, **generation fails if any planted anomaly isn't measurably there.** Every later
number — recall, precision, lead time — is scored against that answer key, so a ground truth that quietly lied
would invalidate all of it, silently.

> **In the room:** "The generator refuses to write an answer key it can't verify. Otherwise every number
> downstream is measured against a lie, and you'd never know."

---

## Chapter 2 · Recomputing what you were given

The metrics layer turns raw deliveries into seven daily series: OTIF by company, by factory, by lane; fill rate
by product group; backlog; yield; inventory age.

Plain SQL, one file per metric, numbered so they run in order, each merging its results by key so running it
twice changes nothing.

One decision in there is worth more than the rest.

The upstream gold data **already contains** on-time and in-full flags — the first project computed them. The
obvious thing is to trust them.

I recompute them from the raw fields instead, and there's a test asserting the two agree.

### Why that isn't paranoia

Two systems quoting the same KPI and quietly meaning different things by it is precisely the failure the first
project spent a week fixing between two BI tools. Building the second system on an *assumption* that it shares
the first system's definition would have reintroduced the same class of bug at the seam between them.

Recomputing and comparing turns an assumption into a test. If the definitions ever diverge — someone changes a
tolerance, someone fixes a bug in one place — the build fails and names it, instead of two dashboards
disagreeing for a quarter.

**Result: zero disagreements across 51,571 rows.**

One more detail, small but instructive. The catch-weight tolerance (how much under the ordered weight still
counts as a complete delivery) lives in the policy file, and the SQL reads it from a table loaded from that
file. Why not just write `0.02` in the query? Because it's needed in three different SQL files, and a number
written in three places is a number that will one day disagree with itself.

> **In the room:** "I didn't trust the upstream flags. I recomputed them from the raw fields and tested that we
> agree — that's how you find out whether two systems mean the same thing by the same word."

---

## Chapter 3 · Three ways of being wrong

Now the actual detection. Something moved — how do you know it matters?

The naive approach is a threshold: alert when OTIF drops below 90%. It fails in both directions at once, and
understanding why leads to the design.

**It cries wolf.** Some lanes run at 88% every day of their lives. That's their normal. Alerting on it every
morning trains everyone to ignore you.

**It misses things.** A lane that normally runs at 99% falling to 94% is a serious deterioration and never
crosses 90%.

So there are three detectors, because there are three genuinely different shapes of "something is wrong":

**The cliff.** A day-over-day comparison against a rolling 28-day baseline, expressed in standard deviations —
*how unusual is today for this particular segment?* Catches sudden drops.

**The line.** The business's own thresholds, requiring several consecutive days. *This is below what we told our
customers we'd do, and has been since Tuesday.* Statistically unremarkable, operationally unacceptable.

**The drift.** A change-point technique called CUSUM, which accumulates small deviations and fires when the
total is too large to be noise. Catches half a standard deviation a day that never trips the first detector and
hasn't gone away in a fortnight — the shape of a slow weight shortfall or inventory quietly ageing.

### Two corrections that separate a detector from an alarm

**Day of week.** Shipping has a weekly rhythm: Mondays and Fridays look different from Wednesdays. Compare a
Monday against a 28-day average containing four Sundays and you've built a very sensitive detector of *the
calendar*. Each weekday's own offset is removed before anything is judged.

**Holidays.** Excluded from the baseline. Otherwise a shutdown week drags the average down and quietly lowers
the bar for the fortnight after it — so the system becomes *less* sensitive right after the disruption, which is
exactly backwards.

### And four gates on what becomes an incident

Severity is the strongest verdict any detector gives — then capped by four checks. The most instructive:
**thin volume**. A lane that shipped three order lines has an OTIF of 0%, 33%, 67% or 100%. That's arithmetic,
not information, and a system that pages someone about it will be muted within a week.

> **In the room:** "A cliff, a level and a slow drift are three different shapes. A detector that only sees one
> of them will miss the interesting week — and I can show you which of the six planted anomalies each one
> catches."

---

## Chapter 4 · "Which lane?" has to be a number, not a guess

Detection says *something moved at this factory*. The next question is immediately *where?*

That could be a language model's job: hand it the data and ask what's driving the change. It would produce a
fluent, plausible answer, and nobody could check it.

Instead it's arithmetic, and the arithmetic happens to be exact.

A rate at a coarse level — a factory's OTIF — is a weighted average of the same rate one level down, across its
customer lanes. The movement between two periods splits precisely into two parts, per lane:

**The rate effect** — this lane got worse.
**The mix effect** — this lane didn't change, but it's now a bigger share of the volume, so it pulls the average
further.

Those two, summed across every lane, equal the total movement *exactly*. It's an identity, not an
approximation. That closure is what makes it publishable: when the brief says "this lane accounts for 71% of the
drop", the percentages add up, and somebody can check.

### Where measurement changed my mind

My first version ranked lanes by their total contribution — rate plus mix.

It named the wrong lane. For the weight-basis shortfall, it reported PRIMALS as the top driver, when the planted
cause was CASE-READY. The reason: a short comparison window's volume mix wanders against a 28-day baseline, and
those mix terms are large enough to bury the segment that actually deteriorated.

Ranking by the **rate effect** — who actually got worse — names the planted cause every time.

And one more thing, which sounds pedantic and isn't. The number the brief quotes is the share of the
*performance change*, not of the whole movement. A share of the whole can exceed 100% or flip sign when the mix
moved the other way, producing sentences like *"PLT-02 accounts for −52% of the drop"* — arithmetically true and
completely unusable in front of an executive.

> **In the room:** "That 71% isn't a phrase a model chose. It's a decomposition that adds up — and I rank by who
> got worse, not by who moved the average, because I tried it the other way and it named the wrong lane."

---

## Chapter 5 · The day my detector scored 2%

Everything above is a hypothesis until something measures it across real history.

So: replay. Walk 535 days one at a time, run every detector at every grain, group the resulting flags into
incidents, and score them against the planted anomalies. Recall (did we find them?), precision (how much of what
we raised was real?), lead time (how fast?), attribution (did we name the right segment?).

First full run:

**Recall 100%. Precision 2%.**

383 incidents raised. 377 with nothing behind them.

That run was worth more than every passing test I'd written. Four causes — and only one of them was subtle.

### 1. My thresholds were guesses, and the data said so

In week one I'd written `otif_rate: high: 0.90` into the policy file, because 90% sounds like a bad day.

The measured median of that series is **0.905**.

My "this is serious" line sat at the middle of the distribution. It fired on half of all days. I had never
looked at the distribution before choosing the number — I'd used intuition about a business I'd been working
with for a fortnight.

Every threshold is now set from the observed distribution, roughly the worst 5% for a warning and the worst 2%
for an incident, with the percentile written in the file next to the value so the next person knows where it
came from.

### 2. A line drawn for a company is not the line for one lane

A single customer lane ships a handful of order lines a day. Its daily rate is mostly 0 or 1 — median 1.00,
tenth percentile 0.00. **No absolute threshold means anything at that grain.**

A lane is now judged on its seven-day volume-weighted rate against its own 28-day norm. Like compared with like
— which is also how a human would judge it.

### 3. Statistics can say it moved. Only policy can say it matters.

This is the one worth carrying to other projects.

I had the statistical detectors and the business thresholds all feeding the same severity. So anything unusual
became an incident.

Now: the statistical detectors raise a **warning** on their own — something moved, somebody might look. Only
crossing a line in the policy file makes it an **incident** — the thing that interrupts a person's morning.

Before that separation, the statistical detectors produced **322 of 365** incidents. Nearly all were real
movements that nobody needed waking for.

### 4. A genuine bug

My CUSUM never reset after firing. Textbook procedure resets the accumulator when it signals; mine didn't, so a
single sustained shift re-reported itself every day for a fortnight.

### After

Recall 100%, **precision 80%**, attribution 100%, median lead time one day. Five incidents in eighteen months,
four of them the planted anomalies.

And because I'd tuned those thresholds by looking at the first half of the history, the report scores the second
half — never used for tuning — **separately**. It comes out at 100% precision. Without that split, "I tuned it
until it looked good" and "it works" are indistinguishable.

> **In the room:** "My first replay scored 2% precision. Three of the four causes were my own thresholds being
> guesses. That's why I don't trust an alerting system that hasn't been measured against history — including
> mine."

---

## Chapter 6 · The rule I deleted

Backlog build-up seems obviously detectable. Watch the queue of orders not yet shipped; alert when it climbs.

I built it. It fired. It looked fine.

Then I measured it properly against the planted order surge.

**The genuine anomaly peaks at 2.81 standard deviations. Ordinary weekly swings reach 4.09, 4.39 and 5.04.**

Read that again, because it's the whole chapter. The real problem is *less statistically unusual* than the
routine noise. There is no threshold that admits the real one without admitting all the false ones. It isn't a
tuning problem — the statistic cannot separate the classes.

The reason is structural: a queue is autocorrelated (today's backlog is mostly yesterday's) and mechanically
seasonal (nothing ships over a shutdown, so the queue builds with no underlying problem at all).

So I removed the rule.

Backlog is still computed, still watched, still shown as a warning — and nobody gets woken for it. The policy
file carries the reason, with the numbers, and what would probably work instead: backlog measured against
throughput, or compared week-over-week at the same weekday. That's a week of analysis, and it's written down as
such rather than guessed at.

The alternative was obvious and available: tune it until the answer key looked good. That produces a better
report and a detector that fails silently on data it hasn't seen.

There's a professional muscle here that I think separates senior from mid-level more reliably than any
technology: being able to say **"we measured it, it doesn't work, here's what I'd try next"** — to a client, in
writing, without it reading as failure. Shipping something you know can't discriminate isn't safer. It just
defers the invoice.

> **In the room:** "I deleted this detector. Here are the two distributions — the real anomaly is less unusual
> than the noise. I'd rather tell you that now than have you find out when it misses something."

---

## Chapter 7 · The decoy

The invented world contains one anomaly that is **real and must not be reported as news**.

Over the 4th of July, the plants shut. OTIF collapses — genuinely, measurably, exactly as the numbers say. And
it is completely unremarkable: it happens every year, everyone knows, nothing is wrong.

A detector without a calendar raises it as a crisis and loses precision. Worse, it does so every year, and
people learn that the system doesn't understand their business.

So: holidays are in the policy file. Days inside a holiday window are capped below incident level. The brief
*names the shutdown in its first line* before it says a single number — because otherwise the reader opens a
brief that begins "OTIF is at 34.6%" with no explanation, and either panics or learns to ignore the brief every
July.

And here is the part that makes the decoy a real test rather than a claim: there's a test that runs **the same
detectors with the holiday calendar emptied**, and asserts they *do* raise it as an incident.

Without that, "we handle holidays correctly" is unfalsifiable — maybe the detector just never noticed. With it,
you've proven the mechanism does the work.

> **In the room:** "Here's a real dip that isn't news, handled. And here's the same system with the calendar
> removed, raising it — so you know the calendar is doing the work, not luck."

---

## Chapter 8 · What the model is allowed to see

Now, at last, the AI.

The brief is written by a language model. Its job is prose: turning a set of findings into something a person
reads at 06:30 and understands immediately.

It never sees the database.

It receives exactly one thing — the **evidence pack**: a single structured object containing the flagged items,
the computed drivers, the detector's own reason for each flag, how old the data is, and a **flat list of every
number it is permitted to use**, each with a name.

Three properties, all load-bearing:

**Every quotable number has a stable reference.** `I1.value`, `I1.driver1.share`. Flat rather than nested, so
the checker that comes later can resolve any reference with a dictionary lookup rather than walking a tree —
because a validator with a tree-walker in it is a validator with bugs in it.

**Rounding happens once, in code.** The pack contains the string `"74.1%"`, and the model is asked to copy that
string. It is never asked to turn `0.74138` into a percentage. A model doing that formatting will sometimes
write 74%, sometimes 74.14%, and occasionally 74.8% — and inside a fluent paragraph, a human cannot tell which
is which.

**Nothing else is in scope.** No tables. No history. No series to average. It *cannot* compute a trend, because
it was never given one. All the arithmetic already happened, in SQL, with tests.

This inverts the usual instinct. Most work in this space asks "how much context can I give the model?" For
anything that produces numbers people act on, the better question is **"what is the minimum it needs, and what
can I make structurally impossible?"**

Hallucination mitigation is mostly prompt engineering. Some of it is just not handing over the raw material.

### The same idea applied to permissions

A plant manager should see their own factory's numbers, not the company's.

The wrong way: build the full brief, then hide the parts they shouldn't see. The data was still in the prompt,
still in the model's context, still in the logs — one bug away from the reader.

Here, the audience is a **parameter of building the pack**. Another factory's numbers are never in it. A test
serialises a PLT-01 manager's entire prompt and searches it for `PLT-02` and that customer's code: zero
occurrences.

Not hidden in the interface. **Never in the prompt.**

> **In the room:** "It can't average anything, because it never receives anything to average. And it can't
> mention the other factory, because that factory was never in the object it was given."

---

## Chapter 9 · The fact-checker, and the three times it was wrong

The model writes fluent prose. Fluent prose containing a wrong number is more dangerous than an obvious error,
because it survives a human skim.

So every claim in the brief must cite the specific metric it came from. That part is structural: the citation
field is **required** by the output schema, so a claim without one is a shape the model cannot return. "It
forgot to cite" isn't a failure mode to defend against; it's impossible.

Which frees the validator to chase the harder failure: **a claim that cites a real fact and then states a
different number.** Wrong, cited, and completely convincing.

The validator resolves every reference against the pack, extracts every number from the text, and requires each
one to match a fact the claim cites. It also checks that no severity was talked up, that no factory was
invented, and that stale data was disclosed.

If it rejects the brief, the model gets **one** correction — carrying the validator's actual complaints, because
"you wrote 74.8%, the fact says 74.1%" is a far better instruction than "try again". If that fails too, a
plain templated brief goes out instead, assembled from the same facts by code, validated identically.

The brief always ships. A day with no brief is a silent failure — nobody notices nothing arrived until the week
somebody needed it.

### What happened the first time I ran it for real

Twenty briefs through the model. Citation coverage 100% — structurally guaranteed. But first-pass validity
**35%**, and a **25% fallback rate**. One brief in four was too broken to fix.

I went looking for a model problem. **Three of the four causes were mine.**

**Typography.** The model writes factory names with a *non-breaking hyphen* — `PLT‑02`, character U+2011,
visually identical to the ordinary one. My validator compared bytes, saw a stray "02" it couldn't match to any
fact, and rejected perfectly good sentences. Three of the four fallbacks were this.

**Notation.** "OTIF fell by 9.4 points" was rejected because the stored fact is −9.4. That's a validator
enforcing *notation* rather than truth; the direction is carried by the verb.

**Schema.** I'd used a nullable type in the strict output schema, which this provider rejects outright — costing
a brief that fell back for reasons having nothing to do with its writing.

**Token budget.** A six-item brief ran past the output limit and came back truncated.

The same twenty days, after fixing my checker: **100% first-pass validity, 0% fallback.** Same standard — the
adversarial tests still reject a wrong number under a right citation, an invented reference and a talked-up
severity.

That sample also found a bug in the *pack*: a volume label keyed by metric rather than by grain, so a
product-group fill rate weighted by order lines was labelled "ordered pounds". A wrong label on a right number —
which passes every numeric check and misleads the reader anyway.

The habit worth taking away: **when your evaluation says the model is failing, check the evaluation first.** It's
the cheaper hypothesis and it's right more often than is comfortable.

> **In the room:** "My fallback rate was 25%. Three of the four causes were my checker being wrong about
> typography, not the model being wrong about facts."

---

## Chapter 10 · Proposing is not doing

The brief says what happened. The last question is what to do about it — and this is where systems like this
usually become dangerous.

Three kinds of action exist: raise a ticket, request an investigation, post an internal note. That's the
complete list. **Nothing writes back to the ERP.** Ever. The system reads from the company's system of record
and never touches it.

The bodies of those tickets are assembled **in code** from the pack's facts, not written by the model. A ticket
is read next month, without the brief beside it, by someone who wasn't in the conversation — and a paraphrased
number in a ticket is a wrong number with a long life. Every figure in the body carries the reference it came
from.

Which type of action is appropriate for which kind of problem lives in the policy file. Severity only decides
how loudly: a HIGH item takes the first permitted action, a warning takes the least intrusive one allowed.

### The gate, and why it doesn't care who wrote the action

Every proposed action goes through a gate that checks it against the policy — regardless of where it came from.
The drafter, a future model, a retry from the console, a test.

That seems redundant: the drafter can only produce permitted actions anyway. It isn't, and the reasoning
generalises. **A drafter that can only produce allowed actions is one refactor away from producing a disallowed
one.** The gate is the thing that will still be true after somebody changes the drafter in six months.

Four rules, all from the policy file: the action type must be permitted for that kind of problem; a brief
proposes at most a handful (a person asked to approve fifteen things approves fifteen things without reading);
nothing duplicates something already open; and the same brief never proposes the same thing twice.

A blocked action is **kept, with its reason**, and shown in the console. Silently dropping it would hide a policy
that has drifted from what the business actually needs — the console is where that becomes visible.

And approval re-checks the policy. Otherwise an editor who changes an action's type walks straight past the
allowlist on the way out. **Approving means "do the thing policy permits", not "do anything".**

Tested adversarially: a type the policy doesn't list, a type not allowed for *this* problem, an unknown problem
type, an action about an item not in the brief. All blocked before a person is asked.

> **In the room:** "Nothing here reaches your ERP. And the gate refuses actions the system itself can't
> currently produce — because the code that produces them will change, and the gate is what survives that."

---

## Chapter 11 · The pause that survives the process

Now an architecture question worth being disciplined about: when does a workflow justify a framework?

In the first project, the pipeline is fixed — profile, map, validate, publish. That's a function call. I used
plain code, and there's no workflow framework anywhere in it.

This project has one property the first doesn't.

**The brief is built at 06:00. Somebody approves an action at 14:00 — possibly from a different machine, long
after the original process has exited.**

That's a state machine with a checkpoint in the middle. Writing it by hand means writing serialisation,
resumption and a step ledger. That's what LangGraph already is, so I used it:

```
build pack → narrate → draft actions → policy gate → ⟪PAUSE⟫ → execute → record
```

compiled with an interrupt before `execute`, and a Postgres checkpointer holding the paused state.

### The interrupt is a stop, not a flag

This is the part I'd defend hardest. The `execute` step is **never asked** whether approval happened. The graph
*cannot reach it* without a person resuming the run.

There's no boolean somebody can flip, no branch that skips the check, because the check isn't code — it's the
shape of the graph.

And the test that earns the dependency: start a run, capture the pause, then **throw away the compiled graph,
the checkpointer and the database connection entirely**, and resume from a fresh set in a new context. If that
didn't have to work, plain code would do — and I'd have used it, as I did in the other repository.

Smallest abstraction that fits, in both directions. Reaching for a framework you don't need is a cost. Refusing
one you do need is also a cost, paid later, in somebody's on-call rotation.

> **In the room:** "This is the only reason there's a graph here rather than plain code. If the pause didn't
> have to survive a restart, I'd have written a function."

---

## Chapter 12 · Everything that can go wrong at 07:00

A system that works is not the same as a system somebody can run. The last stretch is about the difference.

**When the ticket system is down.** The person approved at 14:00 and went home. A timeout or a server error
becomes `FAILED_RETRYABLE` — the action stays open, the console shows it, the next run tries again. A *rejection*
(the request was malformed, the project doesn't exist) becomes `FAILED`, because retrying a wrong request
forever is how a queue fills with identical failures. Even an unexpected bug in the adapter is retryable rather
than a lost approval.

**When the mail server is down.** Delivery happens last and matters least. The brief is written, validated and
recorded *before* anything is sent, so a failed email is a noisy inconvenience and the brief still exists in the
console. A missing brief would be a silent failure. Those are not the same severity and the design says so.

**A bug worth telling.** I'd made the mock ticket table hold a foreign key into the actions table. The mock
failed — because the executor runs before the record is written. The fix wasn't reordering: **a real ticket
system has no referential integrity with your database.** It accepts a ticket whether or not you've finished
writing your own row, and it keeps that ticket if you delete yours. My mock was failing in a way the live
adapter never could, which meant my model of the live adapter was wrong.

**What an operator sees.** Seven database views answer the operational questions, and an endpoint raises alerts
named after sections of the runbook — `no-briefs`, `narration-falling-back`, `approvals-piling-up`,
`action-failed`. Each alert's name is a heading in a document whose first step is a command to run. An alert
without a page is a pager without an answer.

The reason the numbers live in views rather than in the dashboard code: the figure an operator reads at 08:00,
the figure a test asserts and the figure the runbook quotes should be **the same figure**, not three
implementations that drift apart.

**And the bug only production could find.** The live ops page showed model cost as `$0.0000` while the usage
table beside it showed `$0.0029`. The join keyed on the pack's `audience` field, which stored the reader's
*role* while the run record stored their *user id* — two concepts wearing one field name. Every local test
passed, because every local test used the same wrong value on both sides.

You cannot find that on a laptop. Which is rather the point of deploying things.

> **In the room:** "Here's the runbook. Every alert the system can raise has a section with the same name and a
> first step that's a command — because at 07:00 nobody wants a dashboard, they want an instruction."

---

## What this project actually is

Five ideas, in order of how much they matter:

1. **Code computes; the model narrates.** No AI call exists anywhere in the metrics or detection layers, and a
   test enforces it.
2. **Every claim cites a metric**, and every number is checked against the evidence that produced it.
3. **Policy lives in a file the business owns**, enforced in code, never widened from a prompt.
4. **A person approves before anything acts** — enforced by a graph that structurally cannot reach the step that
   acts.
5. **The brief always ships**, because a missing brief is a silent failure.

261 tests. Fifteen decisions written down with alternatives and evidence. Deployed and running: an API, a
console, a scheduled job and a database, with a brief narrated by the model and an approval that created a real
ticket.

---

## The part this really defends: what a forward-deployed engineer does

If you're reading this to judge whether I can do that job, here's the argument made explicitly. A
forward-deployed engineer isn't a consultant who recommends and isn't a platform engineer who receives
specifications. The job is to sit inside somebody else's messy reality and come out with software that survives
contact with their organisation.

**Finding the real problem, not the stated one.** The brief was "too many dashboards". The actual failure was a
three-week customer-level collapse invisible in the top-line number — and finding that required looking at the
data, not running a workshop. Chapter 0.

**Measuring before believing, including my own work.** The 2% precision run, the ranking that named the wrong
lane, the validator that was wrong three times out of four. Chapters 4, 5 and 9. Every one of those was *my*
mistake, found by a measurement I built specifically so it could find them.

**Knowing when to delete.** The backlog detector, removed because the evidence showed the statistic cannot
discriminate — written up with the numbers and a suggested alternative, rather than tuned until the report
looked good. Chapter 6. This is the hardest one to do in front of a client and the most valuable.

**Designing for the organisation, not just the system.** Permission filtering that happens before the model
call, a policy file the business owns rather than a constant in code, an approval that re-checks, and actions
that never touch the ERP. Chapters 8 and 10. These are all organisational facts expressed as mechanisms.

**Right-sizing the technology, in both directions.** Plain code in one project, a checkpointed graph in the
other, with one sentence explaining exactly which property justified the difference. Chapter 11. The skill isn't
using sophisticated tools; it's knowing which problems don't need them — and being willing to use one when the
problem does.

**Owning it where it actually runs.** Deployed, with a runbook written for 07:00, alerts named after the pages
that answer them, and a cost bug that only production could surface. Chapter 12.

**Reporting honestly.** Both projects' evaluation reports include targets that were missed and the reasons.
Precision counts every unexplained alert as wrong, so 80% is a floor rather than an estimate — and because
thresholds were tuned on the first half of the history, the untouched half is scored separately.

That's the posture, and it's the same one in both halves of this story: **the report includes what failed.** If
it didn't, nobody would have a reason to believe the parts that passed — including me.

---

*The first half — how the numbers became trustworthy in the first place — is
[here](https://github.com/shekhus/m3-trusted-data-foundation/blob/main/docs/EXPLAINER.md).*
