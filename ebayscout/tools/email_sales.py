"""
ebayscout/tools/email_sales.py

Reads a Gmail Takeout ``.mbox`` of eBay emails and prints the operator's sales
and purchases as JSON, ready to merge into the Logger workbook's ``sales``,
``purchases`` and ``price_log`` tabs (2026-11-11). Pure stdlib; nothing here
touches a sheet, eBay or GCP.

The monthly routine (the operator's 1st-of-the-month reminder): search Gmail
for eBay's sale, order, refund and shipping-label emails since the last pull,
label them, export that label with Takeout, upload the ``.mbox``, run this, and
check every figure before anything is written. The Gmail search the reminder
gives is::

    from:ebay.com after:START before:END (subject:"Order confirmed" OR
    subject:"Your order is confirmed" OR subject:"Your order is in" OR
    subject:"Refund issued for order" OR subject:"You made the sale" OR
    subject:"Your item sold" OR subject:"shipping label is ready" OR
    subject:"buyer opened" OR subject:"received your refund")

Three kinds of email are read:

- **"You made the sale for …"**: one per item sold. Sale price (after any
  accepted offer), the shipping the buyer paid for that line, order number,
  date sold and quantity. The title is cut at about 50 characters and there is
  no item number.
- **"Your shipping label is ready"**: one per label bought. Every item in the
  package with its full title, item number and order number, and what the
  label cost. Forwarded copies repeat a label and are counted once.
- **"Order confirmed: …"** (purchases): item number, price, quantity, order
  number, seller, the order's subtotal, shipping, tax and total, and the item's
  photo, rewritten to eBay's 1600 px address.

A sale is joined to its label by order number and title prefix, which supplies
its item number and full title; the label's cost goes on the first item of the
package, as on the ``sales`` tab. Two sales in one order with the same cut-off
title can't be told apart: they are paired in label order and reported under
``ambiguous`` so the split is checked by hand.

Buyer names and addresses are in these emails. Nothing here reads them, and
the output carries buyer usernames only where eBay's own reports already do
(it carries none). Never commit an ``.mbox``.

Usage:
    python -m ebayscout.tools.email_sales ebay-claude-2026-10.mbox > oct.json
    python -m ebayscout.tools.email_sales a.mbox b.mbox --pretty
"""

import argparse
import email.header
import email.utils
import html
import json
import mailbox
import re
import sys
import urllib.parse
from datetime import datetime


# --- email to lines ---------------------------------------------------------------

_INVISIBLE = re.compile(r"[ \t ​‌͏]+")


def decode_header(value):
    """A header as one line of text, encoded words decoded."""
    text = str(email.header.make_header(email.header.decode_header(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _html_part(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            raw = part.get_payload(decode=True) or b""
            return raw.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def html_lines(source):
    """The visible text of an email's HTML, one non-empty line per block,
    consecutive duplicates dropped (eBay repeats some blocks for mobile)."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", source)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|h\d|li|table)>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    out = []
    for line in text.splitlines():
        line = _INVISIBLE.sub(" ", line).strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    return out


def money(text):
    """The first amount in ``text`` ("$4.00 (2 x $2.00)" -> 4.0), or None."""
    m = re.search(r"(\d[\d,]*\.?\d*)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def value_after(lines, label):
    """The line after the one reading ``label`` (a trailing colon and a
    parenthesised count such as "(2 items)" are ignored), or None."""
    want = label.lower()
    for i, line in enumerate(lines[:-1]):
        key = re.sub(r"\s*\(.*\)$", "", line.rstrip(":").strip()).lower()
        if key == want:
            return lines[i + 1]
    return None


def photo_url(src):
    """eBay's full-size address for an item photo found in an email, or ""."""
    src = urllib.parse.unquote(urllib.parse.unquote(src or ""))
    m = re.search(r"i\.ebayimg\.com/(?:00/s/[^/]+/z|images/g)/([A-Za-z0-9~_-]+)/", src)
    return f"https://i.ebayimg.com/images/g/{m.group(1)}/s-l1600.jpg" if m else ""


def _item_photo(source):
    """The order email's item photo: the first eBay image that is not one of
    the logo / badge PNGs every email carries."""
    for raw in re.findall(r'imageUrl=([^"&\s]+)|(https://i\.ebayimg\.com/[^"\s)]+)', source):
        src = urllib.parse.unquote(raw[0] or raw[1])
        if ".PNG" in src.upper().split("?")[0] and "$_57" in src:
            continue
        url = photo_url(src)
        if url:
            return url
    return ""


# --- the three kinds of email ------------------------------------------------------

def parse_sale(lines):
    """A "You made the sale" email, or None if its fields are missing."""
    if "Sold:" not in lines:
        return None
    i = lines.index("Sold:")
    sold_on = value_after(lines, "Date sold")
    try:
        date = datetime.strptime(sold_on, "%b %d, %Y").date().isoformat()
    except (TypeError, ValueError):
        return None
    qty = value_after(lines, "Quantity sold")
    return {
        "title": lines[i - 1].rstrip(".").rstrip(),
        "sold": money(value_after(lines, "Sold")),
        "shipping": money(value_after(lines, "Shipping")),
        "order": value_after(lines, "Order"),
        "date": date,
        "quantity": int(qty) if qty and qty.isdigit() else 1,
    }


def parse_label(lines):
    """A "Your shipping label is ready" email: its items and what it cost."""
    items = []
    for i, line in enumerate(lines):
        if line == "Item Number" and i >= 3 and i + 1 < len(lines):
            qty = lines[i + 5] if i + 5 < len(lines) and lines[i + 4] == "Qty" else "1"
            items.append({
                "item": lines[i + 1],
                "title": lines[i - 3],
                "order": lines[i - 1],
                "quantity": int(qty) if qty.isdigit() else 1,
            })
    return {"items": items, "postage": money(value_after(lines, "Total charged to"))}


def parse_order(lines, source=""):
    """An "Order confirmed" email (a purchase)."""
    items = []
    for i, line in enumerate(lines):
        if line == "Item ID:" and i >= 3 and i + 5 < len(lines):
            q = re.search(r"\((\d+) x", lines[i - 1])
            items.append({
                "item": lines[i + 1],
                "title": lines[i - 3],
                "price": money(lines[i - 1]),
                "quantity": int(q.group(1)) if q else 1,
                "order": lines[i + 3],
                "seller": lines[i + 5],
            })
    totals = {}
    for key, label in (("subtotal", "Subtotal"), ("shipping", "Shipping"),
                       ("tax", "Sales tax"), ("total", "Total charged to")):
        v = value_after(lines, label)
        if v is not None and re.search(r"\d", v):
            totals[key] = money(v)
    return {"items": items, "photo_url": _item_photo(source), **totals}


# --- joining ----------------------------------------------------------------------

def join_sales(sales, labels):
    """One row per item sold: the sale's money with the label's item number,
    full title and postage. Returns (rows, ambiguous)."""
    by_order = {}
    for label in labels:
        for n, item in enumerate(label["items"]):
            row = dict(item, postage=label["postage"] if n == 0 else None)
            by_order.setdefault(item["order"], []).append(row)
    rows, ambiguous, used = [], [], set()
    for sale in sorted(sales, key=lambda s: (s["date"], s["order"] or "")):
        prefix = sale["title"][:40]
        candidates = [r for r in by_order.get(sale["order"], [])
                      if r["title"].startswith(prefix) and id(r) not in used]
        same = [s for s in sales if s["order"] == sale["order"] and s["title"] == sale["title"]]
        match = candidates[0] if candidates else None
        if match:
            used.add(id(match))
        if len(same) > 1:
            ambiguous.append({"order": sale["order"], "title": sale["title"]})
        rows.append({
            "date": sale["date"],
            "order": sale["order"],
            "item": match["item"] if match else "",
            "title": match["title"] if match else sale["title"],
            "quantity": sale["quantity"],
            "sold": sale["sold"],
            "shipping": sale["shipping"],
            "postage": match["postage"] if match else None,
            "label": bool(match),
        })
    rows.sort(key=lambda r: (r["date"], r["order"] or "", r["item"]), reverse=True)
    seen, unique = set(), []
    for a in ambiguous:
        key = (a["order"], a["title"])
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return rows, unique


def read_mboxes(paths):
    """Sales, labels (forwarded copies dropped) and purchase orders from mboxes."""
    sales, labels, orders, seen_sales, seen_labels = [], [], [], set(), set()
    for path in paths:
        for msg in mailbox.mbox(path):
            subject = decode_header(msg["Subject"])
            source = _html_part(msg)
            lines = html_lines(source)
            if subject.startswith("You made the sale"):
                sale = parse_sale(lines)
                ref = msg["Message-ID"]
                if sale and ref not in seen_sales:
                    seen_sales.add(ref)
                    sales.append(sale)
            elif "shipping label is ready" in subject:
                label = parse_label(lines)
                key = tuple(sorted(i["item"] for i in label["items"]))
                if label["items"] and key not in seen_labels:
                    seen_labels.add(key)
                    labels.append(label)
            elif subject.startswith("Order confirmed"):
                order = parse_order(lines, source)
                if order["items"]:
                    order["date"] = email.utils.parsedate_to_datetime(msg["Date"]).date().isoformat()
                    orders.append(order)
    return sales, labels, orders


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("mbox", nargs="+", help="Gmail Takeout .mbox file(s)")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON")
    args = ap.parse_args(argv)
    sales, labels, orders = read_mboxes(args.mbox)
    rows, ambiguous = join_sales(sales, labels)
    print(f"{len(sales)} sale(s), {len(labels)} label(s), {len(orders)} purchase order(s); "
          f"{sum(1 for r in rows if not r['label'])} sale(s) without a label, "
          f"{len(ambiguous)} ambiguous", file=sys.stderr)
    json.dump({"sales": rows, "ambiguous": ambiguous, "purchases": orders},
              sys.stdout, indent=1 if args.pretty else None, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
