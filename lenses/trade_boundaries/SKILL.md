---
name: trade_boundaries
description: Electrical work placed on the electrical contractor by other trades' documents, the boundary list or the AF, or left between trades, that the calc doesn't price.
role: lens
tools: [search_blocks, read_blocks, outline, calc_posts, view_tile, catalog_search, submit_flags]
---

# Trade boundaries

## Purpose

An electrical estimator usually reads only the electrical description. This lens reads everything else: other trades'
descriptions, the boundary list (gränsdragningslista) if there is one, the AF's general provisions and the drawings.
Find electrical duties placed on the electrical contractor there, and work that no trade clearly owns, then check the
calc for each.

## Checklist

All items are taxonomy 8 (`trade_boundary`) unless noted; when the duty is clearly the contractor's and simply has no
post, use `unpriced_requirement` (taxonomy 15).

- Holes and penetrations: core drilling, chasing, and certified fire sealing of cable penetrations. Who does each, and
  does the calc have hours for it under the stated conditions?
- Demolition: removal of old installations, centrals, rising mains and disused cables, in apartments and common areas.
- Site power and temporary lighting for all trades, for the whole build time, not just a set-up.
- Electrical connection of other trades' equipment: fans, pumps, motors, controls, floor heating, towel rails, water
  metering, lifts, doors and gates, kitchen appliances.
- Low-voltage systems: door phones, access control, fire and smoke alarms, fibre, TV and data, EV charging.
- Earthing and bonding of new pipes and metal parts after other trades replace them.
- Making good after electrical work: filling, painting, tiling. Who restores surfaces?
- The boundary list against the descriptions: rows that put work on the electrical contractor that its own
  description never mentions, and rows that contradict a description.
- Lines in other trades' documents that hand a task to the electrical contractor (for example, connection by the
  electrical contractor). Search for them by trade and by equipment name.

## What counts as material

A boundary is material when the work it moves to the contractor is worth about 20 000 kr or more, or when it is open
enough that a later dispute is likely. Quote the block that assigns the work (or that leaves it open) and say what the
calc has for it. If the calc covers it, it is not a finding.

## Tools

search_blocks across all documents, using the map's roles to target other trades' documents; read_blocks for context;
outline to find boundary and general-provision chapters; calc_posts to check coverage; view_tile when a drawing shows
equipment to be connected. End with submit_flags.
