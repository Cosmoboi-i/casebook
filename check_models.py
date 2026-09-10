"""Validate both models against saved real responses: python check_models.py EP1.json EP2.json"""
import json
import sys

from models import CompetitionDetail, ListingCandidate

ep1, ep2 = (json.load(open(p)) for p in sys.argv[1:3])

rows = ep1["data"]["data"]
ok = [ListingCandidate.model_validate(r) for r in rows]
print(f"ENDPOINT 1: {len(ok)}/{len(rows)} records validated")
c = ok[0]
print(f"  e.g. {c.id} {c.type} tier={c.organisation.tier} deadline={c.regnRequirements.end_regn_dt} "
      f"elig_keys={list(c.regnRequirements.eligibility_dict())[:4]}")

d = ep2["data"]
where = "data.competition" if "competition" in d else "data"
rec = d.get("competition", d)
det = CompetitionDetail.model_validate(rec)
print(f"ENDPOINT 2: validated (record at {where}) -> {det.enrichment_fields()}")
