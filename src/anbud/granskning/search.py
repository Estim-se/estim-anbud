"""Full-text search over a corpus: SQLite FTS5 in memory, ranked by BM25, with snippets.

Two indexes over the same rows: words (unicode61, diacritics folded, prefix matching per term) and trigrams, so a
query word inside a Swedish compound still finds the block. Word hits rank first; trigram hits fill up to the limit.
Rows are block texts and outline titles; an outline title that points at a block returns that block's id.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass

from anbud.granskning.corpus import Corpus

_TERM = re.compile(r"\w+", re.UNICODE)
SNIPPET_TOKENS = 16


@dataclass(frozen=True)
class Hit:
    block_id: str | None  # None for an outline title that points at no block
    doc_id: str
    source: str  # "block" or "outline"
    snippet: str
    score: float


class SearchIndex:
    def __init__(self, corpus: Corpus) -> None:
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()  # lenses search from parallel threads
        self._db.execute(
            "CREATE VIRTUAL TABLE words USING fts5(block_id UNINDEXED, doc_id UNINDEXED, source UNINDEXED, text, "
            "tokenize = 'unicode61 remove_diacritics 2')"
        )
        self._db.execute(
            "CREATE VIRTUAL TABLE grams USING fts5(block_id UNINDEXED, doc_id UNINDEXED, source UNINDEXED, text, "
            "tokenize = 'trigram')"
        )
        rows: list[tuple[str | None, str, str, str]] = []
        for block_id in corpus.all_order:  # excluded documents too
            block = corpus.blocks[block_id]
            rows.append((block.id, block.doc_id, "block", block.text))
        for entry in corpus.outline:
            if entry.block_id is None or entry.block_id not in corpus.blocks:
                title = f"{entry.number} {entry.title}" if entry.number else entry.title
                rows.append((entry.block_id, entry.doc_id, "outline", title))
        for table in ("words", "grams"):
            self._db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?)", rows)  # noqa: S608 - fixed names

    def search(self, query: str, doc_ids: set[str] | None = None, limit: int = 20) -> list[Hit]:
        terms = [t for t in _TERM.findall(query) if t.strip("_")]
        if not terms:
            return []
        hits: list[Hit] = []
        seen: set[tuple[str | None, str, str]] = set()
        word_query = " OR ".join(f'"{t}"*' for t in terms)
        gram_terms = [t for t in terms if len(t) >= 3]
        queries = [("words", word_query)]
        if gram_terms:
            queries.append(("grams", " OR ".join(f'"{t}"' for t in gram_terms)))
        for table, fts in queries:
            if len(hits) >= limit:
                break
            for block_id, doc_id, source, snippet, score in self._run(table, fts, doc_ids, limit * 3):
                key = (block_id, doc_id, snippet if block_id is None else "")
                if key in seen:
                    continue
                seen.add(key)
                hits.append(Hit(block_id, doc_id, source, snippet, score))
                if len(hits) >= limit:
                    break
        return hits

    def _run(self, table: str, fts: str, doc_ids: set[str] | None, limit: int) -> list[tuple]:
        sql = (
            f"SELECT block_id, doc_id, source, snippet({table}, 3, '[', ']', '…', {SNIPPET_TOKENS}), bm25({table}) "  # noqa: S608
            f"FROM {table} WHERE {table} MATCH ?"
        )
        params: list[object] = [fts]
        if doc_ids is not None:
            if not doc_ids:
                return []
            sql += f" AND doc_id IN ({','.join('?' * len(doc_ids))})"
            params.extend(sorted(doc_ids))
        sql += " ORDER BY bm25(" + table + ") LIMIT ?"
        params.append(limit)
        try:
            with self._lock:
                return self._db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:  # a query FTS5 can't parse finds nothing rather than failing the tool
            return []
