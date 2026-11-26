---
name: contract_terms
description: Contract terms and deviations from the standard general and subcontract terms - penalties, security, payment, liability, index, change terms and notice deadlines.
role: lens
tools: [search_blocks, read_blocks, outline, calc_posts, law_lookup, catalog_search, submit_flags]
---

# Contract terms

## Purpose

Find the terms in this tender that move money or risk to the electrical contractor compared with the standard terms it
would otherwise sign under, and turn each into something code can price or an estimator can act on. Work from the AF
(the administrative provisions), the contract and any subcontract terms appendix. Cite standard terms by chapter or
clause number only, with your own short paraphrase; never reproduce their wording.

## Checklist

- Contract layers (taxonomy 1, `contract_layering`): which general terms apply (AB 04 or ABT 06), which subcontract
  terms sit on top (AB-U 07 or ABT-U 07), or the main contractor's own terms.

- Order of precedence and every deviation the AF lists. A deviation that shifts a standard risk (information, site
  conditions, existing installations) to the contractor is a finding in itself (`contract_layering`).

- Delay and stage penalties (taxonomy 1, `contract_vite`): amount per week, what it is a share of, whether it is capped,
  whether stage penalties stack with completion penalties, and whether they can be reduced. Chapter 4 of the general
  terms is the reference. Money basis: `contract_formula` with `vite_per_week`; inputs `weeks` (the delay at risk),
  and `per_week`, or `percent_per_week` with `contract_sum`; `cap` or `cap_percent` if the documents state a cap.

- Security (taxonomy 1, `contract_security`): bank guarantee or other security, its size, how long it runs into the
  warranty period, and whether an advance is refused. Chapter 6 is the reference. Money basis: `contract_formula`
  with `security`; inputs `contract_sum`, `percent` and `months`.

- Payment (taxonomy 1, `contract_payment`): payment only for completed work, payment tied to the main contractor being
  paid, long payment periods, retentions, and short deadlines for the final invoice or for settling all changes.

- Liability and warranty (taxonomy 1, `contract_liability`): warranty or liability longer than standard, liability
  tied to signed self-inspections, low thresholds for what counts as a serious defect, and insurance demands.
  Chapter 5 is the reference.

- Price adjustment (taxonomy 10, `contract_index`): a fixed price with no index over a long job, especially one running
  into the next year. Money basis: `contract_formula` with `index_drift`; inputs `material_amount` (the material in
  SEK exposed to price change, from the calc's material posts), and `change_percent`, or `annual_percent` with
  `months` (from the tender date to the purchases).

- Wage steps (taxonomy 10, `contract_index`): a fixed price over a collective-agreement wage step. Money basis:
  `contract_formula` with `wage_step`; inputs `hours` (worked after the step date), and `increase_per_hour`, or
  `increase_percent` (with `hourly_rate` if the documents give one).

- Changes and extra work (taxonomy 10, `contract_ata_terms`): fixed rates or mark-ups for changes, a duty to price
  changes before doing them, and rules that a change not notified in writing is lost.

- Deadlines (taxonomy 13, `contract_deadline`): list every notice or claim period (changes, hindrance, objections to
  inspection results, final invoice, limitation). Flag each that is shorter than standard or forfeits a claim.

## What counts as material

A term is material if it can cost about 20 000 kr or more on this job, or if missing it forfeits a claim, triggers a
penalty or gives the other side a right to terminate. Mark the latter with `contractual_exposure`. A term that merely
restates the standard terms is not a finding. Give a formula's inputs in `formula_inputs` under the exact names listed
in the system prompt, and quote the blocks they come from.

## Tools

search_blocks, read_blocks and outline to find the AF chapters and appendices; calc_posts to see whether the calc
already carries a risk or contingency post; law_lookup for public law only (it may not be available yet). End with
submit_flags.
