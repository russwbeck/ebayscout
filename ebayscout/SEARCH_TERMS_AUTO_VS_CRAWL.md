# Search term sets: daily/auto scan vs `/crawl` — for later consideration

> **Status: notes only. No change made.** This documents how the eBay query
> *terms* differ between the always-on daily scan and the on-demand `/crawl <N>`
> so we can decide later whether to reconcile them.
>
> Separate from **safeguards** (excluded sellers / keywords / categories), which
> are now shared from `config` across both paths — see the seller-safeguard PR.
> This note is purely about *which search phrases each path sends to eBay*.

## Daily / auto scan — runs every day (`main.py`, general pass)

`EBAY_SEARCH_QUERIES` (9, unrestricted):
- `Penn State {button, pin, badge, pinback}` (4)
- `Nittany Lions {button, pin, badge, pinback}` (4)
- `Central Counties Bank` (1, standalone — no button-type suffix)

`PSU_SEARCH_QUERIES` (4, **restricted to Sports-Mem category 64482** to drop
"Power Supply Unit" electronics noise):
- `PSU {button, pin, badge, pinback}`

→ **13 queries per daily run.**

### On-demand only (separate flags, NOT part of the daily run or `/crawl`)
- `?era_crawl=1` → `MELLON_CITIZENS_ERA_QUERIES`: prefixes
  `["Penn State", "PSU", "Nittany Lions"]` × button-types × {Mellon, Citizens},
  era-tagged and **year-restricted** at match time.
- `?year_crawl=1` → year-augmented `YEAR_CRAWL_TERMS` / `YEAR_CRAWL_PSU_TERMS`
  across a set of years.

## `/crawl <N>` — `CRAWL500_QUERIES` (12, unrestricted)
- `Penn State Citizens {button, pin, badge, pinback}` (4)
- `Penn State Mellon {button, pin, badge, pinback}` (4)
- `Penn State Central Counties {button, pin, badge, pinback}` (4)

## Differences

| | Daily scan | `/crawl` |
|---|---|---|
| **Prefixes** | Penn State, Nittany Lions, PSU | **Penn State only** |
| **Bank-explicit?** | No — generic, except standalone `Central Counties Bank` | **Yes — every query names a bank** |
| **Mellon / Citizens** | not in the daily pass (only via broad "Penn State button", or the on-demand `era_crawl`) | explicit |
| **Central Counties phrasing** | `Central Counties Bank` (word "Bank", no type) | `Penn State Central Counties {type}` |
| **Category restriction** | PSU → Sports-Mem (64482) | none (bank terms are precise) |
| **Match scope** | broad (full slogan/reference set) | broad (full set; no era/year narrowing) |

## Bottom line

They are **complementary scopes**, not the same search:

- **`/crawl`** is a **deep, bank-name-explicit sweep** of the three banks — it
  targets listings like "Penn State Citizens 1985 button" that eBay relevance for
  a generic "Penn State button" can bury.
- **The daily scan** is a **broad generic sweep** (Penn State / Nittany Lions /
  PSU) plus the standalone `Central Counties Bank` — it catches Nittany Lions /
  PSU / generic buttons that `/crawl` never searches for.

`/crawl` is closest to the on-demand `era_crawl` (Mellon + Citizens), but with
only the "Penn State" prefix, plus Central Counties, and no era/year match
restriction.

## Levers if we later want to reconcile them

- Add `Nittany Lions` / `PSU` prefixes to the crawl bank queries (PSU would want
  the Sports-Mem category restriction, as in the daily PSU path).
- Add explicit per-bank queries (Citizens/Mellon) to the daily set, or fold the
  daily generic set into a shared builder both paths consume.
- Decide whether Central Counties should use one consistent phrasing
  (`Central Counties Bank` vs `Penn State Central Counties {type}`) in both.
- Cost note: each added (prefix × bank × type) phrase is another ≤200-result eBay
  window per run — reconciling upward multiplies `/crawl`'s per-run query count
  (and its paid eBay + CLIP cost) accordingly. Weigh coverage vs. cost before
  unifying.
