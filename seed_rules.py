"""Hand-authored seed data (steps 05 and 06), domain: no db.

The broader claims with the fact statements that back them, and the shallow subject tree. The scripts insert
them; they are data, not behavior, so they sit here with the other rules the ingest applies.
"""

CLAIMS = [
    dict(
        statement="Do not use anabolic steroids at this time.",
        notes=("Vault position as of 2026-08-20 review, per Current Recommendations.md \"On steroids\" section: "
               "the risk case is weaker than previously stated, but so is the benefit, and two time-sensitive open "
               "questions (unexplained FSH, falling bone Z-score) would be permanently or additionally confounded "
               "by starting now."),
        fact_match_prefixes=[
            "Whole-body BMD Z-score fell from -0.6",
            "The FSH result in the most recent hormone panel",
            "In the best-matched prospective study available (Verdegaal",
        ],
    ),
    dict(
        statement=("Target training volume for hypertrophy should sit around 10-20 sets/muscle/week, not higher -- "
                    "current logged volume is well below this floor for every muscle group, so the binding "
                    "constraint is consistency, not the target dose itself."),
        notes="Vault position per Current Recommendations.md \"What changed\" table: 63 of 394 weeks ever hit 3+ sessions.",
        fact_match_prefixes=["Approximately 10 to 20 sets per muscle per week"],
    ),
]


AAS_CHILDREN = [
    'aas-cardiovascular-risk', 'aas-cycle-risk', 'aas-decision-framework', 'aas-emergency-red-flags',
    'aas-endocrine', 'aas-estrogen-management', 'aas-injection-safety', 'aas-kidney-toxicity', 'aas-legal',
    'aas-liver-toxicity', 'aas-mental-health', 'aas-post-cycle-therapy', 'aas-side-effects', 'aas-supply-testing',
]
TRAINING_CHILDREN = [
    'training-volume-hypertrophy', 'training-consistency', 'training-detraining', 'training-mental-health',
    'training-mortality-health', 'training-recovery', 'training-scheduling', 'strength-progression-norms',
    'program-design', 'ankle-mobility',
]
