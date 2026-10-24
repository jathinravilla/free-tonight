"""Split a bill fairly, to the cent."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Share:
    name: str
    subtotal: float
    tax: float
    tip: float
    total: float


def split_bill(items: dict[str, float], *, tax: float = 0.0, tip_pct: float = 0.0,
               shared: float = 0.0) -> list[Share]:
    """Split by what each person ordered.

    items: person -> what they ordered (before tax and tip). shared: items everyone split evenly (apps, pitchers).
    Tax is spread in proportion to each subtotal; tip is a percent of each subtotal. Leftover cents go to the
    largest shares so the totals add up exactly.
    """
    if not items:
        raise ValueError("Nobody to split between.")
    if any(v < 0 for v in items.values()) or tax < 0 or tip_pct < 0 or shared < 0:
        raise ValueError("Amounts can't be negative.")
    n = len(items)
    subtotals = {k: v + shared / n for k, v in items.items()}
    food = sum(subtotals.values())
    grand = round(food + tax + food * tip_pct / 100, 2)
    raw = {}
    for k, sub in subtotals.items():
        t = tax * (sub / food) if food else tax / n
        raw[k] = (sub, t, sub * tip_pct / 100)
    cents = {k: int(sum(v) * 100 + 1e-6) for k, v in raw.items()}  # floor; leftover cents handed out below
    leftover = int(round(grand * 100)) - sum(cents.values())
    for k in sorted(cents, key=lambda k: -sum(raw[k]))[:max(leftover, 0)]:
        cents[k] += 1
    return [Share(k, round(raw[k][0], 2), round(raw[k][1], 2), round(raw[k][2], 2), cents[k] / 100) for k in items]


def split_even(total: float, people: list[str], *, tip_pct: float = 0.0) -> list[Share]:
    """Even split of a total that already includes tax."""
    if not people:
        raise ValueError("Nobody to split between.")
    per = total / len(people)
    return split_bill({p: per for p in people}, tip_pct=tip_pct)


def format_split(shares: list[Share]) -> str:
    width = max(len(s.name) for s in shares)
    lines = [f"{s.name.ljust(width)}  ${s.total:,.2f}" for s in shares]
    lines.append(f"{'Total'.ljust(width)}  ${sum(s.total for s in shares):,.2f}")
    return "\n".join(lines)
