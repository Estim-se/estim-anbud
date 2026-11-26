---
name: occupied_building
description: Working in a building people live or work in - stages, tenants, working hours, outages, temporary power, access and revisits - and the hours the calc misses because of it.
role: lens
tools: [search_blocks, read_blocks, outline, calc_posts, view_tile, catalog_search, submit_flags]
---

# Occupied building, stages and tenants

## Purpose

Renovation with tenants in place or premises in operation splits the work into many short visits under rules about
time, access and disturbance. Find each rule in the documents, work out what it does to the electrical contractor's
hours and set-ups, and check the calc for it.

## Checklist

All items are taxonomy 9 (`occupied_building`) unless noted.

- Stages: how many, in what order, how long each, and how many separate visits each apartment or unit needs from the
  electrical contractor through the sequence (`hours_realism` when the calc's unit times assume one visit).
- Working hours and quiet rules: daytime-only work, bans on noisy work, and how they collide with outages that must
  happen at the least disturbing time.
- Tenants: staying in place or moving to evacuation flats, notice periods before entering, what happens when access
  fails, and work in apartments after tenants move back.
- Temporary power and lighting: per apartment, for common areas and for premises while the old installation is cut,
  for how long.
- Outages and switchover: service connection, main centrals and rising mains replaced while the building is in use,
  with old and new running in parallel.
- Premises in operation: shops, offices, preschools or care, and their opening-hour demands.
- Calendar: holiday periods (summer, Christmas) inside stages that have fixed dates or stage penalties; flag the
  combination with the penalty terms (`contract_vite`) if the AF sets them.
- Protection, dust and cleaning duties in occupied homes.

## What counts as material

A rule is material when it adds about 20 000 kr or more of hours, set-ups or temporary installations that the calc
doesn't carry, or when it makes a stage penalty likely. Give the hours as an `hours_gap` with how you got them
(visits times units times time per visit), using only figures from the documents and the calc. Don't guess catalog
times.

## Tools

search_blocks and read_blocks for the AF's sections on time, disturbance and stages and for tenant information;
outline to find the stage plan; calc_posts to see the hours and set-ups the calc carries; view_tile for a stage plan
drawn on a drawing. End with submit_flags.
