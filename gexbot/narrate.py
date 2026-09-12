"""Narration of a stored map (spec §13).

A readable paragraph built from numbers already in the store. The value is
legibility, not insight — and the constraint below is the whole design.

**Why the forbidden-term list exists**, restated here so a future session does
not quietly relax it: this system's entry logic was tested against four null
models across two rounds and did not beat random out of sample (CLAUDE.md §2).
A language model handed the same numbers will produce a fluent, confident
directional read regardless, because that is what its training distribution
contains. A prediction generated from *your own* data feels more credible than
a stranger's while having exactly the same (absent) validity. The model also
cannot see what it does not know: that tick-rule error is unquantified here,
that a given poll's flow overlay may have been off, that the 2022 control test
failed.

So the lint is not a style filter. It is the part of this feature that keeps
it from becoming the thing the project spent two rounds proving it should not
trust.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass

from rich.console import Console

console = Console()

# Cheapest suitable tier per §13.3. Input is a handful of stored numbers and
# the output is a short paragraph, so the small model is genuinely sufficient.
MODEL = os.getenv("GEX_NARRATE_MODEL", "claude-haiku-4-5")
MAX_TOKENS = 400

FORBIDDEN = {
    "direction": ["bullish", "bearish", "upside", "downside", "rally",
                  "rallies", "selloff", "sell-off", "breakdown", "breakdowns",
                  "breakout", "breakouts", "target", "targets", "downtrend",
                  "uptrend", "bounce", "bounces", "reversal", "dip", "dips"],
    "probability": ["probability", "probable", "likely", "unlikely", "odds",
                    "chance", "chances", "expect", "expects", "expected",
                    "anticipate", "forecast", "predict", "predicts",
                    "prediction"],
    # Phrases, not bare words. "dealers are short gamma" and "classified
    # trades" are the vocabulary of the domain; banning the words themselves
    # rejected a correct first narration and would have pushed the prompt
    # toward vaguer, less accurate prose to get past its own filter.
    "advice": ["go long", "go short", "get long", "get short",
               "long position", "short position", "buy the", "sell the",
               "entry level", "exit level", "stop-loss", "stop loss",
               "position size", "sizing", "place a trade", "take profit"],
    # Future tense in any form: the narration describes a state that exists,
    # and a sentence about what happens next is a forecast however it is
    # hedged.
    "assertion": ["will ", "will,", "will.", "would", "should", "ought to",
                  "going to", "about to", "poised", "set up for", "may ",
                  "might ", "could ", "suggests that price",
                  "implies that price", "tends to lead", "means price"],
}

SYSTEM = """You describe an options gamma map from stored numbers. You are \
writing a factual readout, not analysis.

Describe only: where spot sits, net gamma and its sign, the nearest structure \
above and below with distances, what changed since the previous poll, and the \
expiry concentration.

Do NOT describe what dealer hedging does. That sentence is written for you \
and prepended to your text; writing your own would duplicate it or contradict \
it.

Absolute constraints:
- Never state or imply a direction for price. No bullish, bearish, upside, \
downside, rally, selloff, breakout, breakdown, target.
- Never give a probability, likelihood, or expectation of any price outcome.
- Never suggest a trade, entry, exit, or size.
- Describe only the current state. Use the present tense throughout. Never \
use the future tense, and never use the words "will", "would", "should", \
"could", or "may".
- Describe dealer hedging mechanically ("hedging dampens moves", "hedging \
amplifies moves"), never as a consequence for price direction.

Write two or three sentences of plain prose. State conditions. Do not \
editorialise, and do not conclude."""


@dataclass
class Narration:
    text: str | None
    model: str
    violations: list[str]
    ok: bool
    source: str = "model"        # "model" | "template"


def mechanism_sentence(net_gex: float) -> str:
    """The one sentence whose correctness is already known.

    Templated, not generated. A live run stated that negative dealer gamma
    "reduces the velocity of that move" — fluent, with the numbers correct
    around it, and exactly inverted. There is no reason to let a model compose
    a sentence we can write from a sign, and the lint cannot catch a wrong
    one because the error is factual, not stylistic.
    """
    if net_gex > 0:
        return ("Net dealer gamma is positive, so dealer hedging dampens "
                "moves.")
    return ("Net dealer gamma is negative, so dealer hedging amplifies "
            "moves.")


MECHANISM_WORDS = re.compile(
    r"\b(hedg\w*|dampen\w*|amplif\w*|suppress\w*|pin\w*|cushion\w*)\b",
    re.IGNORECASE)


def strip_mechanism_claims(text: str) -> str:
    """Remove any sentence in which the model describes the mechanism itself.

    The prompt tells it not to, and it does anyway about one run in three —
    once producing "hedging dampens moves away from nearby strikes and
    amplifies reversals toward them" directly beneath the templated sentence
    saying the opposite. An instruction is a request; this is the guarantee.
    Markdown headings and bold are dropped in the same pass: the output is a
    paragraph, not a document.
    """
    text = re.sub(r"^#{1,6}\s*.*$", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "")
    kept = [snt for snt in re.split(r"(?<=[.!?])\s+", text)
            if snt.strip() and not MECHANISM_WORDS.search(snt)]
    return " ".join(kept).strip()


def template_summary(poll: dict, context: dict,
                     expiries: list[dict] | None = None) -> str:
    """A complete summary from numbers alone, no model involved.

    #daily is never empty: when the model is unavailable or its output trips
    the lint, this goes out instead. It is less readable and entirely
    trustworthy, which is the right way round for a fallback.
    """
    parts = [
        f"SPX {poll['spot']:,.0f}. Net gamma {poll['net_gex'] / 1e6:+,.1f}M "
        f"per point.",
        mechanism_sentence(poll["net_gex"]),
    ]
    if not context["regime"]["called"]:
        parts.append("Net gamma is inside the dead zone, too small to call a "
                     "regime.")
    if poll.get("oi_net") is None:
        parts.append("The flow overlay was off for this poll, so the map is "
                     "open interest only.")
    elif poll.get("flow_net") is not None:
        parts.append(f"Open interest contributes {poll['oi_net'] / 1e6:+,.1f}M "
                     f"and today's classified flow {poll['flow_net'] / 1e6:+,.1f}M.")

    levels = context.get("levels") or []
    above = sorted((l for l in levels if l["distance_pts"] > 0),
                   key=lambda l: l["distance_pts"])
    below = sorted((l for l in levels if l["distance_pts"] < 0),
                   key=lambda l: -l["distance_pts"])
    if above:
        parts.append(f"Nearest structure above is {above[0]['strike']:,.0f} "
                     f"({above[0]['label']}), {above[0]['distance_pts']:+,.0f} "
                     f"points away.")
    if below:
        parts.append(f"Nearest below is {below[0]['strike']:,.0f} "
                     f"({below[0]['label']}), {below[0]['distance_pts']:+,.0f} "
                     f"points away.")
    if expiries:
        total = sum(e["gamma"] for e in expiries)
        if total:
            top = max(expiries, key=lambda e: abs(e["gamma"]))
            parts.append(f"{abs(top['gamma'] / total):.0%} of the next 30 "
                         f"days' gamma expires {top['expiry']}.")
    parts.append("Flow is signed by the tick rule; its error rate on this "
                 "data is unmeasured.")
    return " ".join(parts)


def lint(text: str) -> list[str]:
    """Forbidden terms present in the output, as "category:term".

    Word-boundary matched so "downside" is caught but "down 12 points" is not,
    and so "target" does not fire on "targeting" inside a quoted field.
    """
    found: list[str] = []
    low = text.lower()
    for category, terms in FORBIDDEN.items():
        for term in terms:
            pattern = (re.escape(term) if term.endswith(" ")
                       else rf"\b{re.escape(term)}\b")
            if re.search(pattern, low):
                found.append(f"{category}:{term.strip()}")
    return found


def facts(poll: dict, context: dict, previous: dict | None,
          expiries: list[dict] | None) -> str:
    """The numbers the model is allowed to see — stored fields only.

    The canonical regime sentence is deliberately NOT passed through: it
    contains the word "breakouts", which the lint would then drop the whole
    narration for. The model is given the sign and told to phrase the
    mechanism itself.
    """
    lines = [
        f"underlying: {poll.get('underlying')}",
        f"spot: {poll['spot']:,.2f}",
        f"net gamma: {poll['net_gex']:,.0f} dollars per point "
        f"({'positive' if poll['net_gex'] > 0 else 'negative'})",
        # The mechanism is STATED, not left to the model to derive. On the
        # first live run it inverted this — "net gamma is negative, meaning
        # dealer hedging reduces the velocity of the move" — fluently and
        # with the numbers correct around it. The lint governs form, not
        # truth, and would never have caught it. Anything the model could get
        # backwards is supplied rather than inferred.
        f"regime call: {context['regime']['state']}"
        f"{' (inside the dead zone — too small to call)' if not context['regime']['called'] else ''}",
    ]
    if poll.get("oi_net") is None:
        lines.append("flow overlay: OFF for this poll — the map is open "
                     "interest only and cannot see today's 0DTE trading")
    else:
        lines.append(f"open-interest component: {poll['oi_net']:,.0f}; "
                     f"today's classified flow: {poll['flow_net']:,.0f}")
    for lv in context.get("levels", []):
        lines.append(f"level {lv['kind']}: {lv['strike']:,.0f} "
                     f"({lv['label']}, {lv['distance_pts']:+,.0f} points away, "
                     f"gamma {lv['gex']:,.0f})")
    if previous:
        lines.append(f"previous poll {previous.get('minute_of_day')} minutes: "
                     f"spot {previous['spot']:,.2f}, "
                     f"net gamma {previous['net_gex']:,.0f}")
    if expiries:
        total = sum(e["gamma"] for e in expiries) or 1.0
        top = max(expiries, key=lambda e: abs(e["gamma"]))
        lines.append(f"largest single expiry: {top['expiry']} holds "
                     f"{abs(top['gamma'] / total):.0%} of gamma in the next 30 days")
    lines.append("classification note: flow is signed by the tick rule; its "
                 "error rate on this data is unmeasured")
    return "\n".join(lines)


def narrate(poll: dict, context: dict, previous: dict | None = None,
            expiries: list[dict] | None = None, *,
            model: str = MODEL) -> Narration:
    """Generate and lint. A failure returns text=None; it never raises.

    §13.3: this runs off the poll path entirely. An Anthropic outage, a bad
    key, or a linted-out paragraph costs the UI its prose and nothing else —
    it must never block a poll or an alert.
    """
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return Narration(template_summary(poll, context, expiries), model,
                         ["no ANTHROPIC_API_KEY"], False, source="template")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user",
                       "content": facts(poll, context, previous, expiries)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:                        # never reaches the caller
        console.log(f"[yellow]narration failed: {e}")
        return Narration(template_summary(poll, context, expiries), model,
                         [f"api error: {e}"], False, source="template")

    violations = lint(text)
    if violations:
        # Dropped, not shown and not stored as usable. Logged so a drifting
        # prompt is visible rather than silently degrading into advice — and
        # the templated summary goes out in its place, so a rejection costs
        # readability, never the daily post.
        console.log(f"[yellow]narration rejected by lint: {violations}")
        return Narration(template_summary(poll, context, expiries), model,
                         violations, False, source="template")

    # The mechanism is prepended and any model-authored version removed, so
    # the paragraph cannot contain two accounts of it.
    body = strip_mechanism_claims(text)
    if not body:
        return Narration(template_summary(poll, context, expiries), model,
                         ["model wrote only mechanism claims"], False,
                         source="template")
    return Narration(f"{mechanism_sentence(poll['net_gex'])} {body}",
                     model, [], True, source="model")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
