"""Render the privacy ledger as one self-contained HTML page (no internet needed)."""
from __future__ import annotations

import json
from dataclasses import asdict
from html import escape

from .cards import when_line
from .negotiation import PlanResult

_CSS = """
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d1c1a;--muted:#6b6862;--line:#e6e3dc;--share:#0f7b4f;--share-bg:#e3f4ea;
--hold:#6b6862;--hold-bg:#efede8;--human:#7a4be0;--human-bg:#efe8ff;--accent:#e2572b}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1c1a;--ink:#f1efe9;--muted:#a19d94;--line:#2f2d2a;
--share:#5fd39a;--share-bg:#163527;--hold:#a19d94;--hold-bg:#272522;--human:#b69cff;--human-bg:#2a2240;--accent:#ff7a4d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Inter,Roboto,sans-serif}main{max-width:920px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.02em}.sub{color:var(--muted);margin:0 0 24px}
.plan{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-bottom:20px;
display:grid;gap:6px}.plan .big{font-size:20px;font-weight:650}.plan .row{color:var(--muted)}
.status{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
color:var(--share);background:var(--share-bg);padding:2px 8px;border-radius:99px;width:max-content}
.status.bad{color:var(--accent);background:var(--hold-bg)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:22px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.stat b{display:block;font-size:24px;line-height:1.1}.stat span{color:var(--muted);font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:14px}
.filter{border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:6px 12px;border-radius:99px;
cursor:pointer;font:inherit;font-size:13px}.filter.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.round{margin:22px 0 8px;font-size:13px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.e{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:8px 0;
display:grid;gap:8px}.e.human{border-color:var(--human)}
.who{font-size:13px;color:var(--muted)}.who b{color:var(--ink);font-weight:600}.arrow{margin:0 6px}
.sum{font-weight:550}.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:12.5px;padding:3px 9px;border-radius:8px;max-width:100%;overflow-wrap:anywhere}
.chip.s{background:var(--share-bg);color:var(--share)}.chip.h{background:var(--hold-bg);color:var(--hold)}
.chip.p{background:var(--human-bg);color:var(--human);font-weight:600}
.chip .k{opacity:.75;margin-right:4px}.private-tag{font-size:11px;font-weight:700;color:var(--human);
text-transform:uppercase;letter-spacing:.06em}
footer{margin-top:34px;color:var(--muted);font-size:13px}
@media (max-width:640px){.stats{grid-template-columns:repeat(2,1fr)}h1{font-size:23px}}
"""

_JS = """
const buttons=[...document.querySelectorAll('.filter')];
buttons.forEach(b=>b.addEventListener('click',()=>{buttons.forEach(x=>x.classList.remove('on'));b.classList.add('on');
const f=b.dataset.f;document.querySelectorAll('.e').forEach(e=>{const show=f==='all'||(f==='human'&&e.classList.contains('human'))
||(f==='private'&&e.dataset.private==='1')||(f==='table'&&e.dataset.to==='table');e.style.display=show?'':'none'});
document.querySelectorAll('.round').forEach(r=>{let n=r.nextElementSibling,any=false;while(n&&!n.classList.contains('round'))
{if(n.style.display!=='none')any=true;n=n.nextElementSibling}r.style.display=any?'':'none'})}));
"""


def _value(v) -> str:
    if isinstance(v, list):
        if len(v) > 4:
            return ", ".join(map(str, v[:4])) + f" +{len(v) - 4} more"
        return ", ".join(map(str, v))
    return str(v)


def render(result: PlanResult, title: str = "Free Tonight · Privacy ledger") -> str:
    led = result.ledger
    stats = led.stats()
    p = result.plan
    if result.status == "locked" and p:
        plan_html = (
            f'<span class="status">Locked</span><div class="big">{escape(p.label.split(",")[0])}</div>'
            f'<div class="row">{escape(when_line(p.start, p.end))}</div>'
            f'<div class="row">Going: {escape(", ".join(result.confirmed) or "nobody yet")}'
            + (f" · Maybe: {escape(', '.join(result.maybe))}" if result.maybe else "")
            + (f" · Can't: {escape(', '.join(result.declined))}" if result.declined else "")
            + "</div>"
            + (f'<div class="row">About ${result.cost_pp:.0f} per person</div>' if result.cost_pp else "")
        )
    else:
        msg = "No overlap" if result.status == "no_overlap" else "Fell through"
        plan_html = f'<span class="status bad">{msg}</span><div class="big">{escape(" ".join(result.notes) or msg)}</div>'

    body: list[str] = []
    current = None
    for e in led.entries:
        if e.round != current:
            current = e.round
            body.append(f'<div class="round">{escape(e.round)}</div>')
        private = e.to not in ("table",) and e.sender != "all agents"
        chips = [f'<span class="chip s"><span class="k">shared</span>{escape(k.replace("_", " "))}: {escape(_value(v))}</span>'
                 for k, v in e.shared.items()]
        chips += [f'<span class="chip h">🔒 kept private: {escape(w)}</span>' for w in e.withheld]
        if e.human:
            chips.insert(0, '<span class="chip p">👤 human decided</span>')
        to = "everyone (shared table)" if e.to == "table" else e.to
        tag = '<span class="private-tag">private channel</span>' if e.kind in ("relax_owner",) or (
            private and e.sender == e.to) else ""
        body.append(
            f'<div class="e{" human" if e.human else ""}" data-to="{escape(e.to)}" data-private="{1 if private else 0}">'
            f'<div class="who"><b>{escape(e.sender)}</b><span class="arrow">→</span><b>{escape(to)}</b> {tag}</div>'
            f'<div class="sum">{escape(e.summary)}</div>'
            + (f'<div class="chips">{"".join(chips)}</div>' if chips else "")
            + "</div>"
        )

    data = json.dumps([asdict(e) for e in led.entries], default=str).replace("</", "<\\/")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>
<style>{_CSS}</style></head><body><main>
<h1>Privacy ledger</h1>
<p class="sub">Every message the agents exchanged while planning. Green left an agent. Gray never did.</p>
<section class="plan">{plan_html}</section>
<section class="stats">
<div class="stat"><b>{stats['messages']}</b><span>messages</span></div>
<div class="stat"><b>{stats['fields_shared']}</b><span>fields shared</span></div>
<div class="stat"><b>{stats['fields_withheld']}</b><span>things kept private</span></div>
<div class="stat"><b>{stats['human_decisions']}</b><span>human decisions</span></div>
</section>
<div class="legend">
<button class="filter on" data-f="all">Everything</button>
<button class="filter" data-f="table">Shared table only</button>
<button class="filter" data-f="private">Private asks</button>
<button class="filter" data-f="human">Human decisions</button>
</div>
{''.join(body)}
<footer>Generated by Free Tonight. Each agent keeps its owner's schedule, budget and food needs; only free/busy
windows, pooled filters and yes/no answers ever reach the table.</footer>
<script type="application/json" id="ledger-data">{data}</script>
</main><script>{_JS}</script></body></html>"""
