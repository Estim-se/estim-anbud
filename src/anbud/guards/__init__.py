"""Repo guards: customer and project names, document files, and licensed text.

They run in the local git hooks and in CI. A finding names the file and line, never the matched text.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    guard: str
    where: str
    reason: str

    def __str__(self) -> str:
        return f"{self.guard}: {self.where}: {self.reason}"
