# Controlled-context grounding review — September 13, 2026

Two live runs used the existing claude-haiku-4-5 alias. Provider responses identify
claude-haiku-4-5-20251001. Each run exercised eight cases, of which seven called the
model; absent evidence abstained locally. All source text is synthetic.

## Baseline

Raw automated checks passed 8/8, but assistant inspection found a factual error in
`comparison`: the response stated Alpha=four and Beta=eleven working days, then
claimed Alpha was three days faster. Therefore 8/8 automated pass is NOT a quality
pass. The initial rubric missed a material arithmetic error. Other seven responses
matched their supplied facts/status/citations on this inspection.

## Follow-up

Added an instruction against unstated calculations and a comparison regression
against derived calculations. The current suite passed 7/8. The model now computed
seven correctly, but still added the prohibited derived quantity. The gate remains
open. The other seven responses matched their supplied facts/status/citations on
assistant inspection. No independent human semantic review has occurred; raw
reports deliberately retain semantic_review=pending.

Estimated standard API usage cost: baseline $0.010587, follow-up $0.010918,
combined $0.021505. These are token-derived estimates, not account invoices. Each
run reserved no more than $0.15 against counted input and maximum output tokens.
No LLM judging calls, automatic retries or new cloud resources were used.

## Interpretation and next work

The API verifies valid citation references but does not prove factual support for
every claim. A prompt-only restriction did not reliably prevent extra numeric
claims. Next: implement and test a bounded response policy for numeric comparisons
(for example validated calculations or an explicit extractive path), then rerun
this unchanged gate. Do not promote this release on the strength of passing HTTP
contracts or a lucky model run.

This benchmark controls the supplied contexts. It does not evaluate embeddings,
retrieval recall, current Nacha accuracy, general prompt-injection resistance, or
production traffic. Full retrieval and deployment remain separate release gates.

Raw reports retain commit, dirty-worktree status, fixture hashes and relevant code
hashes. The baseline used the earlier fixture; the follow-up includes the new
regression. Preserve both results rather than replacing the failed baseline.
