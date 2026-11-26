"""Planted variants against the integrated pipeline: the calc ingest on the regenerated layout, `anbud run --arm` on
each arm with a scripted fake SDK (no network, no key), calc-post citations in the calc-only arm, and `plant-score` on
what the runs wrote. The answer key is never opened by a run."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import ScriptedSDK, first_text, reply, usage
from planted_synth import calc_posts, write_tender

from anbud import cli, model_client, paths
from anbud.evals import split_heldout as sh
from anbud.evals.planted.calc import priced, regenerate
from anbud.evals.planted.key import CATEGORY_CLAIMS, EXPECTED_CLAIMS, Key
from anbud.evals.planted.variant import variant_dir
from anbud.granskning.calc import parse_calc
from anbud.granskning.contracts import CalcPost, RunInputs
from anbud.granskning.corpus import Corpus
from anbud.granskning.prefix import CALC_ONLY, post_line, system_blocks
from anbud.granskning.provenance import GitState
from anbud.granskning.taxonomy import CLAIM_TYPES
from anbud.granskning.validate import validate_flags
from anbud.models import Block, Flag

COMPANY, TENDER, SEED = "co-001", "t-0001", 2

# Every path a run opens or lists, while `_watching` is set. An audit hook can't be removed, so it stays idle outside.
_SEEN: list[str] = []
_WATCH: list[bool] = []


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if _WATCH and event in ("open", "os.listdir", "os.scandir", "glob.glob") and args:
        path = args[0]
        if isinstance(path, str | bytes | os.PathLike):
            _SEEN.append(os.fsdecode(path))


sys.addaudithook(_hook)


# The calc ingest on the regenerated layout


def test_calc_ingest_reads_the_regenerated_layout(tmp_path: Path) -> None:
    posts = calc_posts()
    data, places = regenerate(posts)
    (tmp_path / "calc.xlsx").write_bytes(data)
    parsed = parse_calc(tmp_path / "calc.xlsx")
    source = [(p, place) for p, place in zip(posts, places, strict=True) if priced(p)]
    assert len(parsed) == len(source)  # no Summa or Totalt row read as a post, no note read as one
    for got, (want, place) in zip(parsed, source, strict=True):
        assert (got.sheet, got.row) == (place.sheet, place.row)
        assert got.section == place.section  # from its heading, never from a note
        assert got.text == want.text
        assert (got.quantity, got.unit, got.unit_price, got.hours) == (
            want.quantity,
            want.unit,
            want.unit_price,
            want.hours,
        )
        amount = want.amount if want.unit_price is None else want.quantity * want.unit_price
        assert got.amount == amount
    assert not any(p.text.startswith(("Summa ", "Totalt")) for p in parsed)


def test_expected_claims_are_taxonomy_claim_types() -> None:
    used = {c for claims in [*EXPECTED_CLAIMS.values(), *CATEGORY_CLAIMS.values()] for c in claims}
    assert used <= set(CLAIM_TYPES)


# Calc-post citations


def _post_flag(post: CalcPost) -> dict[str, Any]:
    return {
        "lens_id": "quantities_hours",
        "claim_type": "quantity_realism",
        "claim": "Mängden i kalkylen verkar låg.",
        "citations": [{"block_id": post.id, "quote": post.text}],
        "money_basis": {"kind": "quantity_gap", "post_ref": post.id, "quantity": 10},
    }


def test_calc_posts_are_citable_only_when_the_corpus_has_no_blocks() -> None:
    post = CalcPost(id="c12345678-s1-r3", sheet="Kalkyl", row=3, section="El", text="Vägguttag 2-vägs", quantity=1)
    posts = {post.id: post}
    empty = Corpus({}, [])
    valid, rejects = validate_flags([_post_flag(post)], [], empty, set(CLAIM_TYPES), posts)
    assert len(valid) == 1 and not rejects
    block = Block(id="aaaaaa-000001", doc_id="aaaaaaaaaaaa", kind="paragraph", text="Vägguttag i kök.")
    corpus = Corpus({block.id: block}, [block.id])
    valid, rejects = validate_flags([_post_flag(post)], [], corpus, set(CLAIM_TYPES), posts)
    assert not valid and "does not exist" in rejects[0]["reason"]
    bad_quote = _post_flag(post) | {"citations": [{"block_id": post.id, "quote": "Något annat helt"}]}
    assert not validate_flags([bad_quote], [], empty, set(CLAIM_TYPES), posts)[0]
    assert CALC_ONLY in system_blocks(empty, [post])[1]["text"]
    assert CALC_ONLY not in system_blocks(corpus, [post])[1]["text"]
    assert post.text in post_line(post)


# Arm runs end to end


def _script(variant: Path, key: Key) -> Any:
    """The fake model: a map, one flag per arm from the lens that would find it, every verdict confirmed."""
    planted_blocks = Corpus.load(variant / "arms" / "planted" / "prep").blocks
    doc_plant = next(p for p in key.plants if p.family == "document" and p.locations["planted"].blocks)
    target = planted_blocks[doc_plant.locations["planted"].blocks[0]]
    calc_only_posts = parse_calc(variant / "arms" / "calc_only" / "calc.xlsx")

    def script(params: dict[str, Any]) -> Any:
        text = first_text(params)
        system = params["system"] if isinstance(params["system"], str) else params["system"][2]["text"]
        if "Map this tender" in text:
            return reply(("submit_map", {"documents": [], "stated_scope": "Syntetisk omfattning.", "lenses": []}))
        if "You are the verifier" in text:
            fp = re.search(r'fingerprint "([0-9a-f]{64})"', text).group(1)
            verdict = {
                "fingerprint": fp,
                "verdict": "confirmed",
                "electrical_scope": True,
                "covered_by_calc": False,
                "would_be_wrong_if": "En annan handling reder ut det.",
            }
            return reply(("submit_verdict", verdict), use=usage(read=10_000))
        lens = re.search(r"review lens `([a-z_]+)`", text).group(1)
        flags: list[dict[str, Any]] = []
        calc_only = "<doc " not in system
        if lens == "quantities_hours" and calc_only:
            flags = [_post_flag(calc_only_posts[0])]
        elif lens == "document_conflicts" and target.id in system:
            exp = doc_plant.expected
            flags = [
                {
                    "lens_id": lens,
                    "claim_type": exp.claim_types[0],
                    "claim": "Handlingarna säger olika saker.",
                    "citations": [{"block_id": target.id, "quote": target.text[:40]}],
                    "money_basis": {"kind": exp.basis_kinds[0], "formula": exp.formula},
                }
            ]
        return reply(("submit_flags", {"flags": flags}), use=usage(read=10_000))

    return script


@pytest.fixture
def planted(data_root: Path) -> tuple[Path, Path, Key]:
    sh.append_events(paths.ledger_path(COMPANY), [sh._assign(TENDER, sh.DEV, "test", ())])
    tender = paths.dev_tender_dir(COMPANY, TENDER)
    write_tender(tender)
    assert cli.main(["plant", COMPANY, TENDER, "--seed", str(SEED)]) == 0
    variant = variant_dir(tender, SEED)
    return tender, variant, Key.model_validate_json((variant / "key" / "plants.json").read_text())


def test_each_arm_runs_without_opening_the_key_then_scores(
    planted: tuple[Path, Path, Key], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tender, variant, key = planted
    sdk = ScriptedSDK(_script(variant, key))
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    # A paid run needs a clean tree; this one is scripted, so pretend the tree is clean wherever the test runs.
    monkeypatch.setattr("anbud.granskning.provenance.git_state", lambda *_: GitState("0" * 40, False, None))
    _SEEN.clear()
    _WATCH.append(True)
    try:
        for arm in ("planted", "unplanted", "calc_only"):
            assert cli.main(["run", COMPANY, TENDER, "--arm", str(variant / "arms" / arm), "--run-id", f"r-{arm}"]) == 0
    finally:
        _WATCH.clear()
    key_dir = str((variant / "key").resolve())
    assert _SEEN and not [p for p in _SEEN if str(Path(p).resolve()).startswith(key_dir)]

    for arm in ("planted", "unplanted", "calc_only"):
        run = tender / "runs" / f"r-{arm}"
        inputs = RunInputs.model_validate_json((run / "inputs.json").read_text())
        assert inputs.arm == f"planted/seed-{SEED}/arms/{arm}"
        assert Path(inputs.prep_dir) == (variant / "arms" / arm / "prep").resolve()
    calc_only_flags = [Flag.model_validate(f) for f in json.loads((tender / "runs/r-calc_only/flags.json").read_text())]
    assert len(calc_only_flags) == 1  # cited a calc post, passed the checks and reached the report
    assert calc_only_flags[0].citations[0].block_id.startswith("c")

    capsys.readouterr()
    assert cli.main(["plant-score", "r-planted", "r-calc_only", "r-unplanted"]) == 0
    report = json.loads((variant / "key" / "scores" / "r-planted.json").read_text())
    assert report["complete"] is True
    assert report["flags"]["planted"]["matched"] == 1 and report["flags"]["calc_only"]["total"] == 1
    out = capsys.readouterr().out
    assert "Handlingarna" not in out and "Vägguttag" not in out


def test_arm_runs_refuse_anything_but_an_arm(
    planted: tuple[Path, Path, Key], capsys: pytest.CaptureFixture[str]
) -> None:
    tender, variant, _ = planted
    for bad in (variant / "key", variant, variant / "arms", tender / "prep", variant / "arms" / "other"):
        assert cli.main(["run", COMPANY, TENDER, "--arm", str(bad), "--dry-run"]) == 3
    assert "refused" in capsys.readouterr().err
    fixture = paths.repo_root() / "fixtures" / "synthetic"
    assert cli.main(["run", COMPANY, "--fixture", str(fixture), "--arm", str(variant / "arms" / "planted")]) == 3
    assert cli.main(["run", COMPANY, TENDER, "--arm", str(variant / "arms" / "calc_only"), "--dry-run"]) == 0
    assert "0 blocks" in capsys.readouterr().out
