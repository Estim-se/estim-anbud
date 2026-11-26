"""`anbud schema-check`: prove the shared tool list stays inside strict mode's limits, and (paid) that it compiles.

- With no flag ($0): counts the list against the limits and checks that schemas/tools.json matches the code.
- `--write` ($0): rewrites schemas/tools.json from the code. Commit it with the change, so the PR shows what the
  model will see.
- `--compile` (paid, about $0.02 per model): sends the full list to each model a tool-using role runs on, with a tiny
  `max_tokens`, under the schema_check budget. A 200 means the grammar compiled. A free count_tokens call can't prove
  that: token counting doesn't compile schemas. Never runs in CI.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from anbud.config import load_config
from anbud.model_client import (
    Budget,
    BudgetExceeded,
    KeyMissing,
    ModelClient,
    ModelError,
    keychain_key,
    make_sdk_client,
)
from anbud.schema import complexity
from anbud.tools import ROLES, TOOL_NAMES, shared_tools, snapshot_text, tools_hash

SNAPSHOT = Path(__file__).resolve().parents[2] / "schemas" / "tools.json"
FAILED = 1
REFUSED = 3
NO_KEY = 4
_SYSTEM = [{"type": "text", "text": "Schema compile check for the Anbud harness. Reply with the word OK only."}]
_MESSAGES = [{"role": "user", "content": "OK?"}]


def _compile() -> int:
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        print("schema-check --compile: refused: paid calls never run in CI")
        return REFUSED
    config = load_config()
    budget = Budget(float(config.budgets["schema_check"]["hard_stop"]))
    try:
        sdk = make_sdk_client(config, keychain_key())
    except KeyMissing as exc:
        print(f"schema-check --compile: {exc}")
        return NO_KEY
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    client = ModelClient(config, sdk, budget, run_id=f"schema-check-{stamp}", company_id="synthetic")
    targets: dict[str, str] = {}  # model id -> the first tool-using role on it
    for role, spec in ROLES.items():
        if spec.tools:
            targets.setdefault(config.model_for(role), role)
    failed = False
    for model, role in sorted(targets.items()):
        try:
            result = client.call(
                role=role, step="schema_check", system=_SYSTEM, messages=_MESSAGES, max_tokens=64, effort="low"
            )
        except BudgetExceeded as exc:
            print(f"  {model}: not sent: {exc}")
            return FAILED
        except ModelError as exc:
            failed = True
            print(f"  {model}: {exc}")
            print(f"    {exc.message[:1000]}")  # the request holds only the tool list, so the message is safe to show
            continue
        usage = result.message.usage
        print(
            f"  {model}: accepted (stop {result.stop_reason}, {usage.input_tokens} in, {usage.output_tokens} out, "
            f"${result.cost_usd:.4f}, request {result.request_id or 'unknown'})"
        )
    print(f"schema-check --compile: spent ${budget.spent:.4f} of ${budget.hard_stop:.2f}")
    return FAILED if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anbud schema-check", description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite schemas/tools.json from the code ($0)")
    mode.add_argument("--compile", action="store_true", help="send the list to each model to prove it compiles (paid)")
    args = parser.parse_args(argv)

    report = complexity(shared_tools())
    print(f"schema-check: {len(TOOL_NAMES)} tools; {report.summary()}; tools sha256 {tools_hash()[:12]}")
    for problem in report.problems:
        print(f"  problem: {problem}")
    if not report.ok:
        return FAILED
    if args.write:
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(snapshot_text())
        print("schema-check: wrote schemas/tools.json")
        return 0
    if not SNAPSHOT.exists() or SNAPSHOT.read_text() != snapshot_text():
        print("schema-check: schemas/tools.json doesn't match the code; run `uv run anbud schema-check --write`")
        return FAILED
    return _compile() if args.compile else 0
