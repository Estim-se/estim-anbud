---
name: missing_documents
description: Documents the tender names or relies on that are not in the package, and the risk the contract puts on the contractor for what they would have shown.
role: lens
tools: [search_blocks, read_blocks, outline, calc_posts, view_tile, catalog_search, submit_flags]
---

# Documents named but missing

## Purpose

Compare what the tender says it consists of, and what its text refers to, with the documents actually in the package
(listed in `<documents>`). A missing document matters when the scope or a risk depends on it and the contract makes the
contractor bear what it would have shown.

## Checklist

- The document list (taxonomy 14, `missing_document`): the AF's list of tender documents and the contract's list of
  contract documents, item by item, against `<documents>`. Note revisions that don't match.
- References in the text (`missing_document`): "according to appendix", "see drawing", named descriptions (room
  descriptions, fire safety description, structural statements, inventories, existing drawings, price forms) that
  aren't in the package.
- Drawings: plans the descriptions rely on that are missing, present only as scans without text, or that contradict
  the object list (use `document_conflict` for the contradiction).
- Existing conditions (taxonomy 7, `unreliable_information`): no existing electrical drawings or inventory, while the AF
  puts the risk of existing conditions on the contractor.
- Forms the bid must include (price forms, unit price lists) that aren't supplied.

## What counts as material

A missing document is material when the scope, a quantity or a priced risk depends on it, or when the contract makes
the contractor responsible for what it would have shown (`contractual_exposure`). A document that is named but plainly
not needed for the electrical scope is not a finding.

This lens makes absence findings: set `absence`, list in `searches` every query you ran to look for the document
(by name, by number, by subject), and set `reference_rule` to the list or the sentence that names it, cited as the
block. The money basis is usually an `hours_gap` (investigation or design hours) or a `missing_post` for a risk
allowance.

## Tools

search_blocks for each document's name, number and subject; read_blocks for the document lists; outline to see every
document's structure; calc_posts to check for an investigation or risk post; view_tile to check whether a scan is the
missing drawing. End with submit_flags.
