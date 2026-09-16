# Discovery brief — M3 Supply-Chain Command Center (public)

Prairie Bend Foods is fictional and all data is synthetic. This is the public brief; it carries no client
names, no sector-plus-geography-plus-metric combinations, and no verbatim source numbers.

| Field | Entry |
|---|---|
| **Customer** | Prairie Bend Foods: a multi-plant North American protein processor on Infor M3, three plants, roughly 1,200 people |
| **Users** | VP Supply Chain, three plant operations leads, a logistics manager, and a BI developer who receives the tickets |
| **Current workflow** | Leaders open eight to ten dashboards each morning. When something looks red, someone investigates by hand, decides whether it matters, and raises a ticket or emails a plant. Time from signal to action is often days. |
| **Pain point** | Red numbers are noticed late or not at all; investigation is manual and inconsistent; the same three questions are asked every morning and answered differently each time. |
| **Systems involved** | Gold tables from the trusted data foundation (or this repo's own synthetic gold), a ticketing system (Jira), email and Slack |
| **Constraints** | AI must not compute metrics; every statement traceable to a metric; nothing sent or created without approval; plant managers see only their own plant; nothing posts back to M3 |
| **Success metric** | Every morning, a brief that flags the right movements (measured by replay against seeded anomalies), attributes them to the right segment, cites every claim, and produces an approved action the same day |

## The three questions the brief answers

1. **What changed?** Detectors in code compare each metric against its own recent history, day-of-week adjusted,
   and against thresholds that live in `policy.yaml`.
2. **Why does it matter?** Attribution decomposes the movement into segment contributions, so "PLT-02 to one
   customer lane accounts for most of the drop" is a computed number, not an impression.
3. **What should we do next?** Actions are drafted from the evidence, checked against policy in code, and wait
   for a person to approve, edit or reject them.

## What this system will not do

- **It will not calculate in the prompt.** Metrics and detections are SQL and Python; the model sees only the
  evidence pack and writes prose about it.
- **It will not act on its own.** Every action stops at an approval, and the state survives a restart.
- **It will not write to the ERP.** Actions are tickets, investigations and internal updates.
- **It will not show a manager another plant's numbers.** Filtering happens before the model call, not in the
  user interface.

## Why delivery performance first

It is the metric the two BI tools already disagree about, it is where the tickets come from, and it is the one
the trusted data foundation already governs end to end. Everything else (yield variance, inventory at risk)
reuses the same detector and narration machinery once delivery performance works.
