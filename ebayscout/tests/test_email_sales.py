"""tools/email_sales.py: eBay sale, label and order emails from a Takeout mbox.

The emails here are made up, laid out the way eBay's were in October 2026.
"""

import mailbox
import os
import sys
import tempfile
from email.message import EmailMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.tools import email_sales as es   # noqa: E402


def _html(*blocks):
    return "<html><body>" + "".join(f"<p>{b}</p>" for b in blocks) + "</body></html>"


def _msg(subject, body, msg_id):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "eBay <ebay@ebay.com>"
    m["To"] = "seller@example.com"
    m["Date"] = "Fri, 9 Oct 2026 17:24:00 -0400"
    m["Message-ID"] = msg_id
    m.set_content("plain part")
    m.add_alternative(body, subtype="html")
    return m


def _sale(title, sold, ship, order, date="Oct 09, 2026", msg_id="<s1@x>"):
    body = _html("Great news&mdash;your item has sold!", "Buyer Name", "1 Main St",
                 title[:50] + "...", "Sold:", f"${sold:.2f}", "Shipping:", f"${ship:.2f}",
                 "Order:", order, "Date sold:", date, "Buyer:", "buyer1",
                 "Quantity sold:", "1")
    return _msg(f"You made the sale for {title[:50]}...", body, msg_id)


def _label(items, postage, msg_id="<l1@x>"):
    blocks = ["Your shipping label is ready", "Ship to", "Buyer Name"]
    for item, title, order in items:
        blocks += [title, "Order number", order, "Item Number", item, "Buyer", "buyer1", "Qty", "1"]
    blocks += ["Order total:", "USPS Ground Advantage", f"USD {postage:.2f}",
               "Total charged to", f"USD {postage:.2f}"]
    return _msg("Your shipping label is ready", _html(*blocks), msg_id)


def _order():
    img = ('<img src="https://svcs.ebay.com/rover?imageUrl=https%3A%2F%2Fi.ebayimg.com'
           '%2F00%2Fs%2FMTAw%2Fz%2FABCdefGHI~jk%2F%24_1.JPG%3Fset_id%3D1">')
    body = _html("Your offer is accepted and your order is confirmed!",
                 "Your order details", "Your payment is complete.",
                 "Penn State Bank Buttons - Many to choose from", "Price:",
                 "$4.00 (2 x $2.00)", "Item ID:", "190862978180", "Order number:",
                 "07-15314-07115", "Seller:", "someseller", img,
                 "Order total:", "Subtotal (2 items)", "$4.00", "Shipping", "$5.00",
                 "Total charged to", "$9.00")
    return _msg("Order confirmed: Penn State Bank Butt...", body, "<o1@x>")


def _mbox(*messages):
    path = os.path.join(tempfile.mkdtemp(), "ebay.mbox")
    box = mailbox.mbox(path)
    for m in messages:
        box.add(m)
    box.flush()
    box.close()
    return path


PINSTRIPE = "2025 Penn State Football Pinstripe Bowl Button Citizens Bank"
FIESTA = "2024 Penn State Football CFP Fiesta Bowl Button Citizens Bank Boise State"


def test_a_sale_joins_its_label_for_item_number_title_and_postage():
    path = _mbox(_sale(PINSTRIPE, 8.5, 7.0, "07-1", msg_id="<a@x>"),
                 _sale(FIESTA, 5.1, 0.5, "07-1", msg_id="<b@x>"),
                 _label([("267802628584", PINSTRIPE, "07-1"),
                         ("267802628588", FIESTA, "07-1")], 5.72))
    sales, labels, orders = es.read_mboxes([path])
    rows, ambiguous = es.join_sales(sales, labels)
    by_item = {r["item"]: r for r in rows}
    assert set(by_item) == {"267802628584", "267802628588"}
    assert by_item["267802628584"]["title"] == PINSTRIPE
    assert (by_item["267802628584"]["sold"], by_item["267802628584"]["shipping"]) == (8.5, 7.0)
    assert by_item["267802628584"]["date"] == "2026-10-09"
    # the label's cost goes on the package's first item only
    assert by_item["267802628584"]["postage"] == 5.72
    assert by_item["267802628588"]["postage"] is None
    assert ambiguous == [] and orders == []


def test_a_forwarded_label_counts_once():
    label = [("267802628584", PINSTRIPE, "07-1")]
    path = _mbox(_label(label, 5.72, "<l1@x>"), _label(label, 5.72, "<l2@x>"))
    _, labels, _ = es.read_mboxes([path])
    assert len(labels) == 1


def test_a_sale_with_no_label_keeps_its_cut_title_and_no_item_number():
    path = _mbox(_sale(PINSTRIPE, 8.5, 7.0, "07-1"))
    rows, _ = es.join_sales(*es.read_mboxes([path])[:2])
    assert rows[0]["item"] == "" and rows[0]["label"] is False
    assert PINSTRIPE.startswith(rows[0]["title"])


def test_two_sales_with_the_same_cut_title_are_reported_ambiguous():
    a = "1978 Penn State Football Button Central Counties Bank Pin Annihilate Ohio State"
    b = "1978 Penn State Football Button Central Counties Bank Pin Pounce On Pitt"
    path = _mbox(_sale(a, 4.25, 6.0, "20-1", msg_id="<a@x>"),
                 _sale(b, 4.25, 0.5, "20-1", msg_id="<b@x>"),
                 _label([("1", a, "20-1"), ("2", b, "20-1")], 5.34))
    rows, ambiguous = es.join_sales(*es.read_mboxes([path])[:2])
    assert {r["item"] for r in rows} == {"1", "2"}       # both still get an item
    assert ambiguous == [{"order": "20-1", "title": a[:50]}]


def test_an_order_email_gives_quantity_totals_and_a_full_size_photo():
    _, _, orders = es.read_mboxes([_mbox(_order())])
    o = orders[0]
    assert o["items"] == [{"item": "190862978180",
                           "title": "Penn State Bank Buttons - Many to choose from",
                           "price": 4.0, "quantity": 2, "order": "07-15314-07115",
                           "seller": "someseller"}]
    assert (o["subtotal"], o["shipping"], o["total"]) == (4.0, 5.0, 9.0)
    assert o["photo_url"] == "https://i.ebayimg.com/images/g/ABCdefGHI~jk/s-l1600.jpg"
    assert o["date"] == "2026-10-09"


def test_the_output_never_carries_buyer_names_or_addresses():
    path = _mbox(_sale(PINSTRIPE, 8.5, 7.0, "07-1"),
                 _label([("267802628584", PINSTRIPE, "07-1")], 5.72))
    rows, _ = es.join_sales(*es.read_mboxes([path])[:2])
    text = repr(rows)
    assert "Buyer Name" not in text and "Main St" not in text and "buyer1" not in text


def test_money_and_photo_helpers():
    assert es.money("$4.00 (2 x $2.00)") == 4.0
    assert es.money("USD 1,234.50") == 1234.5
    assert es.money("") is None
    assert es.photo_url("https://i.ebayimg.com/00/s/NDhYNDg=/z/bnsAAOSwZp5jgIak/$_1.JPG") == \
        "https://i.ebayimg.com/images/g/bnsAAOSwZp5jgIak/s-l1600.jpg"
    assert es.photo_url("https://example.com/x.jpg") == ""
