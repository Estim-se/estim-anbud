"""The claim types a flag may carry, each tied to an item in Estim's tender-risk taxonomy.

The numbers are the taxonomy items in Estim's tender research (1-14); 15-17 are the calc-side items the lenses add.
Lens files cite these codes. The descriptions are Estim's own paraphrase and name no tender.
"""

from __future__ import annotations

# claim_type -> (taxonomy item, one-line description)
CLAIM_TYPES: dict[str, tuple[int, str]] = {
    "contract_layering": (1, "Which general terms apply, their order, and deviations that move risk to the contractor"),
    "contract_vite": (1, "Delay or stage penalties: size, cap, how they stack"),
    "contract_security": (1, "Bank guarantees, securities, retentions and advances"),
    "contract_payment": (1, "Payment terms: timing, pay-when-paid, invoicing limits, settlement deadlines"),
    "contract_liability": (1, "Warranty, liability, defect thresholds and insurance demands"),
    "regulation_ev_charging": (2, "EV-charging infrastructure triggered by the rules for the type of works"),
    "regulation_broadband": (3, "Broadband and telecom infrastructure triggered by the rules for the type of works"),
    "regulation_fire_safety": (4, "Emergency lighting, detectors, fire-rated cabling and other fire-safety scope"),
    "regulation_other": (4, "Another public-law requirement the scope must meet"),
    "grid_interface": (5, "Service connection, grid-owner fees, meters and switchover timing"),
    "metering": (6, "Individual or collective metering and the scope it brings"),
    "unreliable_information": (7, "Hedged quantities or existing conditions whose risk stays with the contractor"),
    "trade_boundary": (8, "Electrical work assigned in another trade's documents or left between trades"),
    "occupied_building": (9, "Staging, tenants in place, working hours, temporary power, access and revisits"),
    "contract_index": (10, "Fixed price without index over a long period; material price risk"),
    "contract_ata_terms": (10, "Terms for changes and extra work: rates, mark-ups, notice and settlement"),
    "design_responsibility": (11, "Design, calculations and as-built documentation the contractor must deliver"),
    "documentation_inspection": (12, "Documentation, self-inspection, inspections and handover hours"),
    "contract_deadline": (13, "Deadlines for notices and claims that forfeit money if missed"),
    "missing_document": (14, "A document the tender names or relies on that is not in the package"),
    "document_conflict": (14, "Documents that contradict each other on scope, quantity or terms"),
    "unpriced_requirement": (15, "A requirement in the documents with no matching post, or a post at zero"),
    "quantity_realism": (16, "A calc quantity that the documents' own counts or areas don't support"),
    "hours_realism": (16, "Calc hours that ignore conditions the documents set (mounting, access, visits)"),
    "outlet_recipe": (16, "The documents call up a standard for outlets or points per room type; cite that passage"),
    "generic_scope": (17, "Normal scope for this kind of job that the documents never mention"),
}
