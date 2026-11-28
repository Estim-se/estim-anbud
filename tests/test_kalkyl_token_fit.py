"""Kalkyl's dry-run token estimate errs high on the calls it was fitted on (audit B5). The estimate picks a run's hard
stop tier, so an estimate that runs low can pick a tier too low for the run. The dev tender's paid reading calls,
counts only, measured: the densest chunk message, the largest one, and the cached prefix with its tools. Each run
summary records chars and tokens to fit the factor again. Synthetic data and fakes only, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

from pathlib import Path

from kalkyl_synthetic import fake_sdk, write_catalog, write_prep

from anbud import paths
from anbud.kalkyl.run import CHARS_PER_TOKEN, TOOLS_TOKENS, chunk_chars, run_dev_tender
from anbud.kalkyl.settings import load_kalkyl_config

# (characters, tokens) measured on the dev tender's paid first reading turns: a chunk message against the call's
# uncached input tokens, the system blocks against its cached tokens (which hold the tools too).
DENSEST_MESSAGE = (623, 416)
LARGEST_MESSAGE = (25_045, 12_902)
PREFIX = (25_118, 14_624)


def test_the_estimate_errs_high_on_the_dev_tender_s_measured_reading_calls() -> None:
    """Mutation: `CHARS_PER_TOKEN = 1.4` -> `CHARS_PER_TOKEN = 3.0` in kalkyl/run.py, the factor before the fit: a
    chunk message is estimated at half its tokens, and a run's tier can be picked from an estimate that runs low."""
    for chars, tokens in (DENSEST_MESSAGE, LARGEST_MESSAGE):
        assert int(chars / CHARS_PER_TOKEN) >= tokens
    chars, tokens = PREFIX
    assert int(chars / CHARS_PER_TOKEN) + TOOLS_TOKENS >= tokens


def test_a_run_records_chars_and_tokens_to_fit_the_factor_again(data_root: Path) -> None:
    """Mutation: the summary's `"tokens": token_counts(...)` line -> deleted in `run_dev_tender`: nothing to fit the
    factor on again."""
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep")
    write_catalog(data_root)
    paid = run_dev_tender("co-001", "t-0001", sdk=fake_sdk(), catalog_name="synthetic", run_id="kalkyl-fit")
    tokens = paid["tokens"]
    assert tokens["chars_per_token"] == CHARS_PER_TOKEN and tokens["prefix_chars"] > 0
    assert tokens["chunks_measured"] == paid["chunks"] > 0
    assert tokens["chunk_tokens_measured"] == 1000 * paid["chunks"]  # the fake's input tokens per call
    assert 0 < tokens["chunk_chars"] <= sum(chunk_chars_of(tender))
    rebuilt = run_dev_tender(
        "co-001", "t-0001", sdk=None, catalog_name="synthetic", run_id="kalkyl-fit-r", base_run="kalkyl-fit"
    )
    assert rebuilt["tokens"]["chunks_measured"] == 0  # a $0 rebuild measures nothing


def chunk_chars_of(tender: Path) -> list[int]:
    from anbud.kalkyl.run import load_corpus

    corpus, _ = load_corpus(tender / "prep")
    return list(chunk_chars(corpus, load_kalkyl_config(), {}).values())
