"""Golden eval set for the enrichment agent.

Well-known companies with a few high-confidence expected values. Deterministic
checks use these; the LLM judge scores accuracy holistically from its own
knowledge, so the expectations here stay small and stable (things that rarely
move). Add rows to broaden coverage.
"""

DATASET = [
    {
        "name": "Anthropic",
        "website": "anthropic.com",
        "topic": "AI-native engineering",
        "expected": {
            "year_founded": 2021,
            "hq_contains": "San Francisco",
            "leader_contains": "Amodei",
            "non_empty": ["about", "hq_location", "year_founded", "leadership"],
        },
    },
    {
        "name": "Hugging Face",
        "website": "huggingface.co",
        "topic": "AI-native engineering",
        "expected": {
            "year_founded": 2016,
            "hq_contains": "New York",
            "leader_contains": "Delangue",
            "non_empty": ["about", "hq_location", "leadership"],
        },
    },
    {
        "name": "Replit",
        "website": "replit.com",
        "topic": "AI-native engineering",
        "expected": {
            "year_founded": 2016,
            "hq_contains": "San Francisco",
            "leader_contains": "Masad",
            "non_empty": ["about", "hq_location", "leadership"],
        },
    },
]
