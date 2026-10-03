"""Search coverage (2026-10-03): the gaps a review of the daily scan and /crawl
found, each pinned by the case that exposed it.

  * the apparel filter matched substrings and dropped real buttons;
  * plural titles ("BUTTONS/PINS") were reachable only if eBay folds plurals;
  * a title naming only Mellon Bank matched no query;
  * /crawl read one page per query, so `/crawl 1000` could not reach past it;
  * nothing logged eBay's total, so no one could tell a window overflowed.

Pure apart from mocked HTTP.  main.py imports the heavy stack, so its wiring is
read via ast.  Run: python tests/run_search_coverage_tests.py
"""

import ast
import contextlib
import io
import os
import sys
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

from ebayscout import config, ebay_client                       # noqa: E402
from ebayscout.utils import title_has_excluded_keyword          # noqa: E402

_MAIN = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_MAIN)


def _fn(name):
    f = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_MAIN, f) or ""


def _excluded(title):
    return title_has_excluded_keyword(title, config.EXCLUDED_KEYWORDS,
                                      config.EXCLUDED_KEYWORD_EXCEPTIONS)


# --- the apparel filter: whole words -------------------------------------------

def test_a_keyword_inside_another_word_no_longer_drops_the_listing():
    assert not _excluded("Penn State Harvest Festival button")         # vest
    assert not _excluded("Penn State Mapleton Bank pinback")           # map
    assert not _excluded("1990 Penn State investment club pin")        # vest


def test_buttons_whose_slogan_holds_a_keyword_are_kept():
    assert not _excluded("1979 Penn State basketball button Sting the Yellow Jackets CCB")
    assert not _excluded("Penn State vs Rutgers New Jersey Mellon Bank button")


def test_apparel_is_still_dropped_singular_plural_or_hyphenated():
    for title in ("Penn State Hoodie 2001", "PSU hoodies lot", "Penn State T-Shirt pin",
                  "Penn State tshirt", "Penn State shirts", "Penn State Quarter-Zip",
                  "PSU Quarter Zip Pullover pin", "Penn State Antigua polo",
                  "Nittany Lions jersey", "Penn State jerseys", "Penn State denim jacket pin",
                  "PSU EMBROIDERED Button Lot", "Penn State enamel pin", "PSU stickers"):
        assert _excluded(title), title


def test_a_plain_button_title_passes():
    assert not _excluded("Penn State Football Button 1987 Fiesta Bowl")
    assert not _excluded("")


def test_exceptions_only_remove_their_own_phrase():
    """A title naming the Yellow Jackets AND a jacket is still apparel."""
    assert _excluded("Penn State denim jacket, Yellow Jackets patch")


# --- the query sets -----------------------------------------------------------

def test_the_daily_scan_and_crawl_search_the_plurals():
    for btn in ("buttons", "pins"):
        assert f"Penn State {btn}" in config.EBAY_SEARCH_QUERIES
        assert f"Nittany Lions {btn}" in config.EBAY_SEARCH_QUERIES
        assert f"PSU {btn}" in config.PSU_SEARCH_QUERIES
        for bank in config.CRAWL500_BANKS:
            assert f"Penn State {bank} {btn}" in config.CRAWL500_QUERIES


def test_the_daily_scan_reaches_a_title_that_names_only_mellon_bank():
    for q in ("Mellon Bank button", "Mellon Bank pin", "Mellon Bank buttons",
              "Mellon Bank pins"):
        assert q in config.EBAY_SEARCH_QUERIES
    # Citizens Bank alone is Citizens Bank Park; it stays paired with Penn State
    assert not any(q.startswith("Citizens Bank") for q in config.EBAY_SEARCH_QUERIES)


def test_the_frozen_year_and_era_crawls_keep_their_singular_terms():
    assert config.BUTTON_TYPES == ["button", "pin", "badge", "pinback"]
    assert config.YEAR_CRAWL_PSU_TERMS == [f"PSU {b}" for b in config.BUTTON_TYPES]
    assert not any("buttons" in q or "pins" in q for q in config.YEAR_CRAWL_TERMS)
    assert not any("buttons" in q or "pins" in q
                   for q, _era in config.MELLON_CITIZENS_ERA_QUERIES)


def test_daily_and_crawl_stay_separate_searches():
    """SEARCH_TERMS_AUTO_VS_CRAWL.md: decided, do not re-unify."""
    assert not set(config.EBAY_SEARCH_QUERIES) & set(config.CRAWL500_QUERIES)


# --- paging and eBay's total ----------------------------------------------------

def _summary(n, title="Penn State Button"):
    return {"itemId": f"v1|{n}|0", "title": title, "seller": {"username": "s"},
            "price": {"value": "5.00", "currency": "USD"}, "itemWebUrl": f"https://e/{n}"}


def _resp(items, total):
    r = MagicMock()
    r.json.return_value = {"itemSummaries": items, "total": total}
    return r


def _run(pages, max_pages, limit=2):
    """find_listings against `pages` (a list of responses or exceptions)."""
    calls = []

    def fake(url, params, headers):
        calls.append(dict(params))
        p = pages[len(calls) - 1]
        if isinstance(p, Exception):
            raise p
        return p

    with patch.object(ebay_client, "_get_app_token", return_value="tok"), \
         patch.object(ebay_client, "_get_with_retry", side_effect=fake):
        out = ebay_client.find_listings("id", "sec", "Penn State button", [],
                                        max_results=limit, max_pages=max_pages)
    return out, calls


def test_one_page_by_default_so_the_daily_scan_is_unchanged():
    out, calls = _run([_resp([_summary(1), _summary(2)], 50)], max_pages=1)
    assert len(out) == 2 and len(calls) == 1 and "offset" not in calls[0]


def test_pages_step_by_the_page_size():
    pages = [_resp([_summary(1), _summary(2)], 50), _resp([_summary(3), _summary(4)], 50),
             _resp([_summary(5), _summary(6)], 50)]
    out, calls = _run(pages, max_pages=3)
    assert [c.get("offset") for c in calls] == [None, "2", "4"]
    assert len(out) == 6


def test_paging_stops_on_a_short_page():
    out, calls = _run([_resp([_summary(1), _summary(2)], 50), _resp([_summary(3)], 50)],
                      max_pages=5)
    assert len(calls) == 2 and len(out) == 3


def test_paging_stops_once_ebays_total_is_covered():
    out, calls = _run([_resp([_summary(1), _summary(2)], 2)], max_pages=5)
    assert len(calls) == 1 and len(out) == 2


def test_a_later_page_that_fails_keeps_the_pages_already_read():
    out, calls = _run([_resp([_summary(1), _summary(2)], 50), RuntimeError("400 offset")],
                      max_pages=3)
    assert len(calls) == 2 and len(out) == 2


def test_a_failed_first_page_still_returns_nothing():
    out, _ = _run([RuntimeError("500")], max_pages=3)
    assert out == []


def test_every_query_logs_ebays_total():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _run([_resp([_summary(1), _summary(2)], 4321)], max_pages=1)
    assert "eBay total 4321" in buf.getvalue()


def test_the_filter_runs_on_every_page():
    pages = [_resp([_summary(1), _summary(2)], 50),
             _resp([_summary(3, "Penn State hoodie"), _summary(4)], 50)]
    out, _ = _run(pages, max_pages=2)
    assert {o["item_id"] for o in out} == {"v1|1|0", "v1|2|0", "v1|4|0"}


# --- wiring ---------------------------------------------------------------------

def test_crawl_reads_several_pages_and_the_daily_scan_one():
    run = _fn("_run_crawl")
    assert "max_pages=config.CRAWL_MAX_PAGES" in run
    assert "all_listings = _collect_ebay_listings(ebay_app_id, ebay_cert_id)\n" in run
    assert config.CRAWL_MAX_PAGES == 5


def test_every_title_filter_uses_the_exceptions():
    for path in ("ebay_client.py", "etsy_client.py"):
        src = open(os.path.join(PKG, path)).read()
        n_calls = src.count("title_has_excluded_keyword(")
        assert n_calls and n_calls == src.count("config.EXCLUDED_KEYWORD_EXCEPTIONS"), path
