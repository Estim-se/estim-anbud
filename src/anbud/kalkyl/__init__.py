"""Kalkyl in shadow (#73): the model reads blocks and cites; code checks the citations, accounts for every quantity
line, builds the object model, groups records into posts, and prices them on a catalog.

Flow: `lines` (quantity lines, heading paths, chunks) -> `extract` (R2 per chunk, through `client`) -> `verify` ->
`price` (object model, posts, catalog mapping, kronor; R3 in `match` for posts R2 left unmapped) -> `xlsx`. `run` ties
them together for `anbud kalkyl`.
"""
