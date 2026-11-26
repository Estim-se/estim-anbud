"""Planted defects: plants on a regenerated calc and on a patched copy of the prep output, two control arms (a
code-only structural detector and a calc-only run), an unplanted control copy as the false-positive baseline, and a
rule-first matcher that scores runs against the answer key.

Every variant lives in the company's data folder, never in the repo, and its answer key sits outside every arm, so no
run can read it.
"""
