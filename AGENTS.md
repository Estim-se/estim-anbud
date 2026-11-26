# Estim Anbud harness

This repo runs Estim Anbud's model work outside the app.

## Public
- Treat everything here as public: a commit can stay public even after the repo goes private.
- No customer content and no customer or project names anywhere: code, fixtures, file names, commit messages, branch names, PR text, CI output. Refer to companies and tenders by neutral ids.
- The list of names CI rejects lives outside the repo. Never add it, or anything from it, here.
- No security details in commits or PRs: no secret values, no Console workspace, account or key ids, and no description of an unfixed weakness.
- Document files (PDF, XLSX, DWG, IFC, images) live only in `fixtures/synthetic/`. Fixtures and few-shot examples are written by Estim and synthetic.

## Data
- Customer data lives in `~/estim-anbud-data/<company_id>/`, outside every repo. Every path and cache key includes the company id.
- Read app data only from files exported into `~/estim-anbud-data/`. Write nothing to the app.
- Never give the harness a Supabase key on a laptop or in this repo, and never use the app's service-role key.
- Logs carry ids, sizes, timings and costs, never document text or model output.

## Held-out tenders
- Never open, list, decrypt, copy or summarise held-out or prospective data. Only `anbud eval heldout` and `anbud eval prospective` touch it. They report aggregates only. They are never started from an agent session, with one exception: the eval lane (`anbud-eval`), which the orchestrator starts with `ANBUD_EVAL_LANE=1`. No other agent sets that variable.
- Agents run `anbud run` on development tenders and synthetic fixtures only.
- Never change a lens, prompt or threshold because of a held-out result. Thresholds are committed in `evals/thresholds/` before an exam.

## Model calls
- Call models only through `ModelClient`: plain Messages API, streaming, explicit `max_tokens`, `tool_choice` auto, model ids from `config/providers.toml`.
- Every Opus role in a run sends the same `tools` array and `system` blocks. Restrict a role's tools in the loop, never by changing the array.
- Paid runs go to the `anbud-dev` workspace and carry a run budget. Never raise a cap or budget yourself.
- Prompts contain no catalog prices and no answer-key text.

## Licensed texts
- Never paste text from AMA, MER, RA, AB 04, ABT 06, AB-U 07, ABT-U 07, SIS or SEK standards, or ATL into this repo. Cite clause or code numbers and write Estim's own paraphrase.

## Secrets
- Never print, read or copy secret values, `.env*` files or Keychain entries. The CLI reads the model key from the Keychain at run time.

## Checks
- `uv run pytest` must pass. `anbud replay <run>` must reproduce stored outputs byte for byte before a code-only change is scored.
- A change to a lens, prompt, effort or model needs `anbud eval dev` results in the PR: aggregate numbers only, never tender text or names.
