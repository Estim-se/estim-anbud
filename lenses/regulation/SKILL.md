---
name: regulation
description: Scope that public law requires for this kind of works - EV charging, broadband, fire safety, electrical safety - when the documents are silent or the calc leaves it out.
role: lens
tools: [search_blocks, read_blocks, outline, calc_posts, law_lookup, catalog_search, submit_flags]
---

# Requirements triggered by regulation

## Purpose

Some electrical scope follows from public law, not from the tender: the documents may never mention it, yet the
client or the building inspector can demand it. Establish the facts that trigger each rule, then check whether the
documents and the calc cover what the rule requires.

## First, the facts

- The kind of works: new build, a substantial renovation (ombyggnad) or a smaller change. This decides which building
  rules apply.
- The permit date or application date, if stated: it decides whether older or newer building rules apply. If it isn't
  stated, say so in the claim.
- The building: dwellings, premises, parking (in the building or on the plot, and how many spaces), stairwell types and
  activity classes.

## Checklist

- EV charging (taxonomy 2, `regulation_ev_charging`): at a substantial renovation that changes the electrical
  infrastructure or the parking, check whether cabling, conduit and charge points are required and priced.
- Broadband (taxonomy 3, `regulation_broadband`): at a substantial renovation, check for in-building infrastructure
  ready for high-speed networks and an access point.
- Fire safety (taxonomy 4, `regulation_fire_safety`): emergency lighting and exit signs in stairwells and escape routes,
  smoke detectors in dwellings, fire-rated cabling for systems that must keep working in a fire (lifts, smoke
  ventilation, emergency lighting), and fire sealing.
- Electrical safety (taxonomy 4 and 12, `regulation_other` or `documentation_inspection`): the self-inspection programme,
  documentation for the installation holder, labelling, and notifying the grid owner.
- Work environment in premises that stay in operation (`regulation_other`): lighting and emergency lighting for
  workplaces.
- The grid owner's terms (taxonomy 5, `grid_interface`): who notifies the grid owner and pays its fees, and what a
  service or meter change requires before work can start.

## What counts as material

A requirement is material when it applies on these facts, the documents or the calc leave it out, and meeting it costs
about 20 000 kr or more. When a trigger fact is unknown (no permit date, no parking count), the finding is the
uncertainty: say which fact decides it. Put the statute and section in `reference_rule`. law_lookup may answer that it
isn't available yet: then cite the statute and section as you know them and say in the claim that the wording wasn't
checked.

## Tools

search_blocks and read_blocks to establish the facts and the documents' coverage; outline to find the technical
description's chapters; calc_posts to check coverage; law_lookup for the rule's wording. End with submit_flags.
