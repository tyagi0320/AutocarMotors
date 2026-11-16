"""Classify automotive search intent and construct queries from a fixed vocabulary."""
import re
from itertools import combinations

CAR_TOPIC = re.compile(
    r"\b(cars?|automotive|vehicles?|suvs?|evs?|electric cars?|hatchbacks?|sedans?|hybrids?|"
    r"auto industry|automakers?|motor industry|test drives?|trailview|urban lite|horizon ev)\b", re.I,
)
OUTSIDE_TOPIC = re.compile(
    r"\b(weather|forecast|temperature|rain|raining|sunny|humidity|cricket|football|sports scores?|politics|"
    r"stock prices?|horoscopes?|recipes?|election results?)\b", re.I,
)
SEARCH_REQUEST = re.compile(r"\b(search|look up|look online|browse|google|online|find|on the web)\b", re.I)
RECENT = re.compile(r"\b(latest|recent|current|new|this year|today|upcoming)\b", re.I)

# Aliases resolve only to public, pre-approved names. User text never becomes query text.
MODELS = {
    "Hyundai Creta": r"\b(?:hyundai )?creta\b",
    "Kia Seltos": r"\b(?:kia )?seltos\b",
    "Tata Nexon EV": r"\b(?:tata )?nexon\s*ev\b",
    "Tata Nexon": r"\b(?:tata )?nexon\b(?!\s*ev\b)",
    "Tata Punch": r"\btata punch\b",
    "Mahindra XUV700": r"\b(?:mahindra )?xuv\s*700\b",
    "Mahindra Scorpio": r"\b(?:mahindra )?scorpio\b",
    "Toyota Fortuner": r"\b(?:toyota )?fortuner\b",
    "Toyota Innova Hycross": r"\b(?:toyota )?innova(?: hycross)?\b",
    "Maruti Suzuki Swift": r"\b(?:maruti(?: suzuki)? |suzuki )swift\b",
    "Maruti Suzuki Brezza": r"\b(?:maruti(?: suzuki)? )?brezza\b",
    "Honda City": r"\bhonda city\b",
    "Honda Elevate": r"\b(?:honda )?elevate\b",
    "Skoda Kushaq": r"\b(?:skoda )?kushaq\b",
    "Volkswagen Taigun": r"\b(?:volkswagen |vw )?taigun\b",
    "MG Windsor EV": r"\b(?:mg )?windsor ev\b",
    "Tesla Model 3": r"\b(?:tesla )?model 3\b",
}
CATEGORIES = {
    "electric car": r"\b(electric|evs?)\b",
    "hybrid car": r"\bhybrids?\b",
    "compact SUV": r"\bsuvs?\b",
    "hatchback": r"\bhatchbacks?\b",
    "sedan": r"\bsedans?\b",
}
TOPICS = {
    "recalls": r"\brecalls?\b",
    "safety ratings": r"\b(safety|crash tests?|ncap|airbags?)\b",
    "charging and range": r"\b(charging|chargers?|charge time|battery range|real.world range)\b",
    "maintenance": r"\b(maintenance|servicing|maintenance costs?|service intervals?|tyres?|tires?|oil change)\b",
    "reliability": r"\b(reliability|reliable|common (?:faults|problems|issues)|ownership costs?)\b",
    "used car buying": r"\b(used cars?|second.hand cars?|buying checklist)\b",
    "fuel economy": r"\b(mileage|fuel economy|fuel efficiency|kmpl)\b",
    "market prices": r"\b(prices?|costs?|on.road|ex.showroom)\b",
    "specifications": r"\b(specifications?|specs|features?|boot space|horsepower)\b",
    "comparisons": r"\b(compar(?:e|able|ison|isons|ing)|alternatives?|versus|vs)\b",
    "reviews": r"\breviews?\b",
    "news": r"\b(news|trends?|industry updates?|launch(?:es|ed|ing)?|release dates?)\b",
}

MODEL_PATTERN = re.compile("|".join(f"(?:{pattern})" for pattern in MODELS.values()), re.I)
PAIRS = {pair: " vs ".join(pair) + " comparison" for pair in combinations(MODELS, 2)}
SEARCH_QUERIES = {
    (subject, topic): f"{subject} {topic}"
    for subject in ("cars", *CATEGORIES, *MODELS)
    for topic in TOPICS
}
APPROVED_QUERIES = frozenset(SEARCH_QUERIES.values()) | frozenset(PAIRS.values())


def external_query(text, *, automotive_context=False):
    """Turn a qualifying automotive request into a pre-approved, PII-free query."""
    if OUTSIDE_TOPIC.search(text) or re.search(
        r"\b(?:don't|do not|no need to|without)\s+(?:search|look|browse|google)\b", text, re.I
    ):
        return None

    models = [name for name, pattern in MODELS.items() if re.search(pattern, text, re.I)]
    if not (models or automotive_context or CAR_TOPIC.search(text)):
        return None
    topic = next((name for name, pattern in TOPICS.items() if re.search(pattern, text, re.I)), None)
    if not topic:
        return None
    if not models and topic in {"market prices", "specifications"} and re.search(
        r"\b(your|autocare|trailview|urban lite|horizon ev)\b", text, re.I
    ):
        return None
    if not (SEARCH_REQUEST.search(text) or (RECENT.search(text) and topic in {"recalls", "market prices", "news"})
            or (models and topic in {"comparisons", "reviews", "safety ratings", "recalls"})):
        return None
    if topic == "comparisons" and len(models) >= 2:
        return PAIRS[tuple(models[:2])]
    subject = models[0] if models else next(
        (name for name, pattern in CATEGORIES.items() if re.search(pattern, text, re.I)), "cars"
    )
    return SEARCH_QUERIES[subject, topic]
