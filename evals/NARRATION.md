# Narration sample

`python evals/narration_eval.py` over **20 briefs**, model `openai/gpt-oss-120b` via groq.
Every number in every brief was checked against the evidence pack that produced it.

| Measure | Result | Target |
|---|---|---|
| Citation coverage | **100%** | 100% (hard gate) |
| Valid on the first attempt | **100%** | reported |
| Corrected once, then valid | **0** | reported |
| Fell back to the templated brief | **0%** | reported, with reasons |
| Claims checked | 275 | |
| Numbers checked | 620 | |
| Cost per brief | $0.00167 | reported |
| Latency, median / p95 | 5086 ms / 8379 ms | reported |

No brief fell back to the template in this sample.

The validator raised no complaints in this sample.
