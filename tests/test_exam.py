"""The exam on a synthetic sealed store: quarantine inside the exam, two inputs per tender, prep and why it stopped, the
estimate and the prefix sizes, the old engine's dry run, deadlines, and (with a fake runner) paid runs frozen into the
sealed ledger within the budget. Every tender, file and name here is invented."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import assert_no_fake_names
from test_baseline import ENGINE, LIB, fake_app, git
from test_intake_blind import notice_page

from anbud import cli, intake, paths
from anbud.evals import exam, freeze
from anbud.evals import split_heldout as sh

NOW = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
LOCAL_NOW = datetime(2026, 12, 1, 10, 0)
PDFS = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "injection-tender" / "clean"
BUN = shutil.which("bun")
SECRET_WORDS = ("Fiktivbolaget", "Ändring", "FoS", "Annons", "Tilldelning", "Dnr", "t-000", "202100")


def tender_zip(deadline: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Annons.html", notice_page("Fiktivbolaget AB", "202100-0001", "Dnr 2026/0101", "2026-10-01", deadline)
        )
        for pdf in sorted(PDFS.glob("*.pdf")):
            zf.writestr(f"Förfrågningsunderlag/{pdf.name}", pdf.read_bytes())
        zf.writestr("Frågor och svar/FoS 1.txt", "Fråga: om Fiktivbolaget. Svar: ja.")
        zf.writestr("Kompletteringar/Ändring 1.txt", "Ändring: 40 uttag i stället för 30.")
        zf.writestr("Tilldelningsbeslut.txt", "Tilldelning till Fiktivbolaget.")
        zf.writestr("Ritningar/E-10.dwg", b"AC1027" + bytes(64))  # prep skips drawings and counts them
    return buf.getvalue()


def sealed_store(data_root: Path) -> None:
    """Three prospective tenders, laid out as intake leaves them: two zips (one open, one past its deadline) and one
    folder delivery with no notice; and one held-out tender the prospective exam never touches."""
    statuses = {"t-0001": sh.HELDOUT, "t-0002": sh.PROSPECTIVE, "t-0003": sh.PROSPECTIVE, "t-0004": sh.PROSPECTIVE}
    sh.append_events(
        paths.ledger_path("co-002"),
        [sh._assign(t, s, "test", (), prospective=s == sh.PROSPECTIVE) for t, s in statuses.items()],
    )
    for t, deadline in (("t-0001", "2026-06-01"), ("t-0002", "2026-12-13 12:00"), ("t-0003", "2026-11-20")):
        folder = data_root / "heldout" / "co-002" / t
        data = tender_zip(deadline)
        (folder / "originals").mkdir(parents=True)
        (folder / "originals" / f"{hashlib.sha256(data).hexdigest()}.zip").write_bytes(data)
        iso = datetime.fromisoformat(deadline.replace(" ", "T")).isoformat()
        (folder / "intake.json").write_text(json.dumps({"tender": t, "notice": {"deadline": iso}}))
    folder = data_root / "heldout" / "co-002" / "t-0004"
    (folder / "Frågor och svar").mkdir(parents=True)
    shutil.copyfile(sorted(PDFS.glob("*.pdf"))[0], folder / "beskrivning.pdf")
    (folder / "Frågor och svar" / "FoS 1.txt").write_text("Svar: nej.")
    (data_root / "catalogs").mkdir()
    catalog = {"name": "estim-default", "label": "x", "currency": "SEK", "hourly_rate": 650.0, "items": []}
    (data_root / "catalogs" / "estim-default.json").write_text(json.dumps(catalog))


def files(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def test_the_exam_is_refused_outside_the_eval_lane(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sealed_store(data_root)
    assert cli.main(["eval", "prospective", "co-002"]) == cli.REFUSED
    assert not (data_root / "sealed").exists()


@pytest.mark.skipif(BUN is None, reason="bun isn't installed")
def test_a_dry_exam_quarantines_prepares_estimates_and_prints_aggregates_only(
    data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sealed_store(data_root)
    held_out_before = files(data_root / "heldout")
    app = fake_app(tmp_path, ENGINE)
    for rel, text in LIB.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(text)
    git(app, "add", "-A")
    git(app, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "lib")
    lines: list[str] = []
    report = exam.run_exam(
        "co-002", "prospective", app=app, app_ref="HEAD", now=NOW, local_now=LOCAL_NOW, say=lines.append
    )
    printed = "\n".join(lines)
    assert_no_fake_names(printed)
    for word in SECRET_WORDS:
        assert word not in printed  # aggregates only: no tender id, file name, buyer or notice value

    # Quarantine: three answer-key kinds held in each zip, one Q&A in the folder delivery; amendments go to Kalkyl.
    assert report.tenders == 3 and report.quarantined == 3 and report.held_tenders == 3
    assert report.held == {"qa": 3, "amendment": 2, "award": 2}
    assert report.amendments_to_kalkyl == 2 and report.same_input == 1
    for t in ("t-0002", "t-0003"):
        g = files(paths.dev_tender_dir("co-002", t, exam.variant_root("co-002", "prospective", "granskning")) / "inbox")
        k = files(paths.dev_tender_dir("co-002", t, exam.variant_root("co-002", "prospective", "kalkyl")) / "inbox")
        assert not any(("FoS" in f or "Ändring" in f or "Tilldelning" in f) for f in g)
        assert "Kompletteringar/Ändring 1.txt" in k and not any(("FoS" in f or "Tilldelning" in f) for f in k)
        assert len(files(exam.held_dir("co-002", t))) == 4  # three held files and their manifest
        record = json.loads((exam.exam_dir("co-002", "prospective") / "tenders" / f"{t}.json").read_text())
        assert record["variants"]["kalkyl"].startswith("with the amendments")
    assert files(data_root / "heldout") == held_out_before  # the sealed deliveries are read, never changed
    assert not (exam.exam_dir("co-002", "heldout")).exists()  # the held-out tender isn't part of this exam

    # Prep, the estimate and the dry runs.
    assert report.prepared == 3 and report.prep_failed == 0 and report.pages > 0
    assert report.skipped_by_type == {"dwg": 2} and "skipped by type: dwg 2" in printed
    assert set(report.estimate_usd) == {"granskning", "kalkyl", "baseline"} and report.estimate_unknown == 0
    assert 0 < report.estimate_total <= report.worst_case_usd
    assert report.baseline_runs == 5 and report.dry_runs_ok == {"baseline": 5}
    assert "dry runs: baseline 5 of 5 reached the first model call" in printed
    assert report.prefix_tokens["runs"] == 3 and 0 < report.prefix_tokens["median"] <= report.prefix_tokens["max"]
    assert report.prefix_tokens["over_context"] == report.prefix_tokens["over_large"] == 0
    assert "granskning prefix (dry-run estimate, before page cards): median " in printed

    # Deadlines: one open in 12 days, one passed (warned), one unknown.
    assert "deadlines: 1 open, nearest open deadline in 12 days; 1 passed; 1 unknown" in printed
    assert "warning: 1 tenders are past their deadline" in printed

    # Everything the exam wrote is in sealed storage, apart from the exported app code.
    assert {p.name for p in data_root.iterdir()} <= {"sealed", "heldout", "co-002", "catalogs", "baseline"}
    assert files(data_root / "baseline") and all(f.startswith("app/") for f in files(data_root / "baseline"))

    # A second exam reuses the quarantine (same deliveries), and the held answers stay put.
    again = exam.run_exam(
        "co-002",
        "prospective",
        engines=("kalkyl",),
        app=app,
        app_ref="HEAD",
        now=NOW,
        local_now=LOCAL_NOW,
        say=lambda _: None,
    )
    assert again.held == report.held and again.estimate_usd.keys() == {"kalkyl"}


class FakeEngines(exam.Engines):
    """Prep writes a summary; runs write outputs and a record, cost what they're told, and never call a model."""

    def __init__(
        self,
        costs: dict[str, float | None],
        stops: dict[str, str] | None = None,
        tokens: dict[str, int] | None = None,
    ) -> None:
        self.costs = costs
        self.stops = stops or {}
        self.tokens = tokens or {}
        self.calls: list[tuple[str, str]] = []

    def prep(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> str | None:
        if tender_id in self.stops:
            return self.stops[tender_id]
        prep = paths.dev_tender_dir(company_id, tender_id, vroot) / "prep"
        prep.mkdir(parents=True, exist_ok=True)
        (prep / "summary.json").write_text(json.dumps({"documents": 2, "pages_by_kind": {"text": 50}}))
        return None

    def granskning_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> int | None:
        return self.tokens.get(tender_id, 10_000)

    def kalkyl_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> float | None:
        return 1.0

    def _run(
        self, engine: str, variant: str, vroot: Path, company_id: str, tender_id: str, run_id: str
    ) -> exam.EngineRun:
        self.calls.append((engine, variant))
        out = vroot / "fake-runs" / run_id
        (out / "outputs").mkdir(parents=True)
        (out / "outputs" / "result.json").write_text("{}")
        (out / "record").mkdir()
        (out / "record" / "run.json").write_text(json.dumps({"code": {"harness_sha": "x"}}))
        settings = {"basis": "matched"} if engine == "baseline" else None
        return exam.EngineRun(
            engine, variant, run_id, 0, True, self.costs[engine], out / "outputs", out / "record", settings
        )

    def granskning(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        return self._run("granskning", "granskning", vroot, company_id, tender_id, run_id)

    def kalkyl(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        return self._run("kalkyl", "kalkyl", vroot, company_id, tender_id, run_id)

    def baseline(self, vroot, company_id, tender_id, variant, run_id, run_dir, *, dry_run):  # type: ignore[no-untyped-def]
        return self._run("baseline", variant, vroot, company_id, tender_id, run_id)


def register(data_root: Path, text: str = "Scoring plan v1.\n") -> dict:
    plan = data_root / "predictions" / "co-002" / "scoring-plan-v1.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(text)
    return freeze.register_plan("co-002", plan)


def test_a_scoring_plan_is_registered_once_per_version_and_must_stay_unchanged(data_root: Path) -> None:
    assert freeze.current_plan("co-002") is None
    first = register(data_root)
    assert first["new"] and first["line_number"] == 1
    assert register(data_root)["new"] is False  # the same bytes: a no-op
    assert freeze.current_plan("co-002")["sha256"] == first["sha256"]
    (data_root / "predictions" / "co-002" / "scoring-plan-v1.md").write_text("Edited.\n")
    with pytest.raises(freeze.FreezeError, match="changed or is gone"):
        freeze.current_plan("co-002")
    second = register(data_root, "Scoring plan v1, edited.\n")
    assert second["new"] and second["line_number"] == 2 and freeze.current_plan("co-002")["line_number"] == 2
    assert freeze.verify(freeze.ledger_file("co-002")).kinds == {"preregistration": 2}
    elsewhere = data_root / "plan.md"
    elsewhere.write_text("x")
    with pytest.raises(freeze.FreezeError, match="directly in predictions"):
        freeze.register_plan("co-002", elsewhere)
    named = data_root / "predictions" / "co-002" / "Plan Fiktivbolaget.md"
    named.write_text("x")
    with pytest.raises(freeze.FreezeError, match="lower-case"):
        freeze.register_plan("co-002", named)


def test_a_paid_exam_freezes_each_run_with_its_input_and_stays_within_the_budget(data_root: Path) -> None:
    sealed_store(data_root)
    with pytest.raises(exam.ExamError, match="scoring plan"):
        exam.run_exam(
            "co-002", "prospective", paid=True, budget_usd=40.0, runner=FakeEngines({}), now=NOW, say=lambda _: None
        )
    assert not (data_root / "sealed").exists()  # refused before anything was unpacked
    plan = register(data_root)
    with pytest.raises(exam.ExamError, match="budget"):
        exam.run_exam(
            "co-002", "prospective", paid=True, budget_usd=1.0, runner=FakeEngines({}), now=NOW, say=lambda _: None
        )

    runner = FakeEngines({"granskning": 8.0, "kalkyl": None, "baseline": 2.0})
    lines: list[str] = []
    report = exam.run_exam(
        "co-002",
        "prospective",
        paid=True,
        budget_usd=40.0,
        runner=runner,
        now=NOW,
        local_now=LOCAL_NOW,
        say=lines.append,
    )
    # Per tender: Granskning ($20 stop), Kalkyl ($3 stop; no cost reported, so counted at its stop), the old engine
    # on each input ($10 stop). The estimate fits $40, but a run that could pass it by its hard stop isn't started:
    # the last tender's Granskning and old-engine runs.
    assert report.spent_usd <= 40.0 and report.runs_skipped_budget == 2
    assert report.prefix_tokens == {
        "runs": 3,
        "median": 10_000,
        "max": 10_000,
        "over_context": 0,
        "over_large": 0,
        "context": 1_000_000,
        "large": 200_000,
    }
    assert sum(report.runs_ok.values()) == report.frozen == len(runner.calls)
    records = [json.loads(line) for line in freeze.ledger_file("co-002", sealed=True).read_text().splitlines()]
    assert len(records) == report.frozen and all(r["kind"] == "freeze" for r in records)
    for r in records:
        assert r["exam"]["variant"] in exam.VARIANTS and r["exam"]["kind"] == "prospective"
        assert r["exam"]["plan"] == {"file": "scoring-plan-v1.md", "sha256": plan["sha256"], "line": 1}
        assert r["exam"]["variant"] == "kalkyl" or r["exam"]["engine"] != "kalkyl"
        assert r["input_manifest_sha256"]  # hashed from the input the run read, not a dev folder
    after = {r["tender"]: r["exam"]["after_deadline"] for r in records}
    assert after.get("co-002/t-0003") is True and after.get("co-002/t-0002", False) is False
    plans = freeze.verify(freeze.ledger_file("co-002")).kinds
    assert plans == {"preregistration": 1}  # the plan only: no run is frozen in the plain ledger
    assert "frozen runs came after their tender's deadline" in "\n".join(lines)


def test_prep_stops_are_counted_by_category_and_prefix_sizes_summarised(data_root: Path) -> None:
    sealed_store(data_root)
    runner = FakeEngines(
        {"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0},
        stops={"t-0003": "security_marking"},
        tokens={"t-0002": 250_000, "t-0004": 1_200_000},
    )
    lines: list[str] = []
    report = exam.run_exam("co-002", "prospective", runner=runner, now=NOW, local_now=LOCAL_NOW, say=lines.append)
    printed = "\n".join(lines)
    assert report.prepared == 2 and report.prep_failed == 1 and report.prep_stops == {"security_marking": 1}
    assert "prep: 2 prepared, 1 stopped (security_marking 1); " in printed
    assert report.prefix_tokens["median"] == 725_000 and report.prefix_tokens["max"] == 1_200_000
    assert report.prefix_tokens["over_context"] == 1 and report.prefix_tokens["over_large"] == 2
    assert (
        "granskning prefix (dry-run estimate, before page cards): median 725,000 tokens, max 1,200,000; "
        "of 2 runs 1 over the context (1,000,000), 2 over 200,000"
    ) in printed
    stopped = paths.dev_tender_dir("co-002", "t-0003", exam.variant_root("co-002", "prospective", "kalkyl"))
    assert not (stopped / "prep").exists()  # a tender whose first input stopped isn't prepared a second time


@pytest.mark.parametrize(
    ("code", "stderr", "category"),
    [
        (0, "", None),
        (3, "anbud prep: refused: a held-out tender\n", "refused"),
        (
            3,
            "anbud prep: stopped: 2 security marking(s) (hemlig), the first in document x, block y.\n",
            "security_marking",
        ),
        (3, "anbud prep: stopped: two files share a block-id prefix; block ids would collide\n", "id_collision"),
        (3, "anbud prep: stopped: an image is larger than the pixel or page limit\n", "image_limit"),
        (
            3,
            "anbud prep: stopped: the inbox, manifest or output folder is in held-out or sealed storage",
            "sealed_storage",
        ),
        (3, "anbud prep: stopped: the inbox doesn't match its intake manifest (1 unexpected, 0 missing)\n", "manifest"),
        (3, "anbud prep: stopped: the tender has no inbox folder, or it is a link\n", "manifest"),
        (3, "anbud prep: stopped: a file in the inbox can't be read\n", "unreadable_file"),
        (3, "anbud prep: stopped: MemoryError while reading the tender\n", "read_error"),
        (3, "anbud prep: stopped: a write outside the company's folder\n", "other_stop"),
        (1, "Traceback (most recent call last):\nKeyError: 'x'\n", "crash"),
        (-9, "", "crash"),
    ],
)
def test_a_prep_stop_is_categorised_from_its_one_stderr_line(code: int, stderr: str, category: str | None) -> None:
    assert exam.prep_stop(code, stderr) == category


def test_every_prep_stop_fragment_is_one_prep_still_writes() -> None:
    """The categories match prep's own messages; a reworded message fails here, not silently as `other_stop`."""
    src = Path(__file__).resolve().parents[1] / "src" / "anbud"
    text = "".join(p.read_text() for p in [*sorted((src / "prep").glob("*.py")), src / "cli.py", src / "store.py"])
    for fragment, _ in exam.PREP_STOPS:
        assert fragment in text


def _dev_tender(vroot: Path, tender_id: str, inbox_files: dict[str, bytes], *, dev: bool = True) -> Path:
    tender = paths.dev_tender_dir("co-002", tender_id, vroot)
    for rel, data in inbox_files.items():
        (tender / "inbox" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tender / "inbox" / rel).write_bytes(data)
    (tender / "inbox.sha256").write_text(intake._manifest(tender / "inbox"))
    if dev:
        exam._ensure_dev(vroot, "co-002", tender_id)
    return tender


def test_real_prep_stops_on_synthetic_files_get_their_category(data_root: Path, tmp_path: Path) -> None:
    """`anbud prep` in a child process, as the exam runs it, on invented files that make it stop."""
    vroot = data_root / "variant"
    runner = exam.Engines(data_root / "logs", tmp_path, "HEAD", None)
    text = b"Teknisk beskrivning.\n\nForty sockets in the hall.\n"
    _dev_tender(vroot, "t-0001", {"beskrivning.txt": text})
    _dev_tender(vroot, "t-0002", {"beskrivning.txt": text, "pm.txt": b"HEMLIG\n\nNot for the bidders.\n"})
    late = _dev_tender(vroot, "t-0003", {"beskrivning.txt": text})
    (late / "inbox" / "added-after-intake.txt").write_bytes(b"A file the manifest doesn't list.\n")
    _dev_tender(vroot, "t-0005", {"beskrivning.txt": text}, dev=False)  # not a dev tender in this data root
    expected = {"t-0001": None, "t-0002": "security_marking", "t-0003": "manifest", "t-0005": "refused"}
    if os.geteuid() != 0:  # root reads a file without read permission
        locked = _dev_tender(vroot, "t-0004", {"beskrivning.txt": text, "låst.txt": b"Locked.\n"})
        (locked / "inbox" / "låst.txt").chmod(0)
        expected["t-0004"] = "unreadable_file"
    try:
        got = {t: runner.prep(vroot, "co-002", t, f"prep-{t}.log") for t in expected}
    finally:
        for path in vroot.rglob("låst.txt"):
            path.chmod(0o600)
    assert got == expected
    assert (paths.dev_tender_dir("co-002", "t-0001", vroot) / "prep" / "summary.json").is_file()
    assert sorted(p.name for p in (data_root / "logs").iterdir()) == sorted(f"prep-{t}.log" for t in expected)
