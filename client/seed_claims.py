"""Hand-authored broader claims, each citing the specific facts backing it.
Run last (needs facts to exist).

A claim may also carry a `warrant` (why the cited grounds support it) and a `qualifier` (how far it holds), #44.
`fact_match_prefixes` entries are either a statement prefix (a plain string: the fact is `grounds`) or a dict
{"prefix": ..., "role": "grounds" | "backing" | "rebuttal", "note": "why this fact is linked"}. Blank warrant,
qualifier and note are stored as NULL; an unknown role fails the build. Normalization and the prefix-matching policy are
`claims_rules` (pure); the INSERTs are the repository adapter `claims_store`, reached through `ctx.stores`."""
import json

import claims_rules
import locations


def load_claims(inputs, data_dir):
    """Read optional claim data (claims.json) from the private data dir `data_dir`."""
    path = locations.join_path(data_dir, "claims.json")
    if not inputs.exists(path):
        return []
    try:
        payload = inputs.read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{path}: cannot read claims: {e}") from e
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("claims"), list):
        raise ValueError(f"{path}: expected version 1 with a claims list")
    return payload["claims"]


def run(con, ctx, claims=None):
    raw = load_claims(ctx.inputs, ctx.locations.private_data_dir) if claims is None else claims
    normalized = [claims_rules.normalize_claim(c) for c in raw]  # validate everything before the first write
    cur = con.cursor()
    for claim in normalized:
        claim_id = ctx.stores.insert_claim(cur, claim)
        for premise in claim.premises:
            ctx.stores.link_claim_premise(cur, claim_id, premise)
    con.commit()
    print(f"[seed_claims] inserted {len(normalized)} claims")
