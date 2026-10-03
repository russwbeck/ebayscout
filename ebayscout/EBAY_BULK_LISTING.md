# eBay bulk listing upload: what worked (2026-10-03)

On 2026-10-03 the operator created **73 listings (308 buttons, $3,171 asking)**
with one Seller Hub bulk upload. That exact file is in the repo at
`listing_uploads/2026-10-03_penn-state-button-listings.csv`. It took six uploads
to get right. This note records what each failure taught, so the next batch
works on the first real upload.

This is operator tooling, not part of the service: nothing in ebayscout reads
these files. The file was generated in a session by a one-off script that is
**not** in the repo. Rebuild from the rules below, or copy the committed file
and edit its rows.

---

## 1. The template

- **Starting file:** the category listing template eBay hands out for bulk
  listing, `fx_category_template_EBAY_US`, downloaded for category **24541**.
  The help link inside it is
  <https://pages.ebay.com/sh/reports/help/create-listings-bulk/>.
- **Row layout:**
  - Row 1: `Info,Version=1.0.0,Template=fx_category_template_EBAY_US`.
  - Row 2: the header. Its first cell is
    `*Action(SiteID=US|Country=US|Currency=USD|Version=1193|CC=UTF-8)`.
    A leading `*` marks a required column.
  - Then one row per listing.
  - Then the template's own trailing `Info` rows (the help link and the
    recommended aspect values). We left them in and eBay ignored them.
- **Columns we added:** two columns not in the downloaded header,
  `C:Modified Item` and `ShippingService-1:AdditionalCost`. eBay accepted both.
  The file has 78 columns.
- **File format:** UTF-8 with a BOM and CRLF line endings, matching the
  template.

## 2. The six uploads and what each one taught

| # | `*Action` | Result | Lesson |
|---|---|---|---|
| 1 | `Draft` | All 73 failed with `BAF.Error.5`, "Unable to find Task Action Id for task Draft". | `Draft` is not an action this template accepts. Nothing was created. |
| 2 | `VerifyAdd` | All 73 failed with `21915469`, "Please add at least one valid shipping service option". | **The account's shipping policy is not applied to uploaded rows.** Shipping has to be spelled out in the file (§3). |
| 3–4 | `VerifyAdd` | All 73 failed with `37`, "Input data for tag \<Item.ShippingDetails\> is invalid or missing". Upload 4 split the rows across shipping variants, and every variant failed. | Ground Advantage's code is **`USPSParcel`**, not `USPSGroundAdvantage`. The fix came from an eBay community thread; eBay's own pages are blocked from the session container. |
| 5 | `VerifyAdd` | Rows with `ShippingDiscountProfileID = Fifty Cent` failed (37 rows) with `17460 … Fifty Cent|SHIPPING_DISCOUNT|Input data … is invalid`. The other 36 rows passed. | The combined-shipping **rule name** is not accepted there. Leave the column blank and apply the rule in Seller Hub once the listings exist. |
| 6 | `Add` | Worked. | The committed file is this one. |

**Always run `VerifyAdd` first.** It validates every row, returns per-row fee
estimates, and creates nothing. Then change only the `*Action` column to `Add`.

**Reading eBay's results file:**
- Columns: `Line Number, Action, Status, ErrorCode, ErrorMessage, …, InsertionFee,
  ListingFee, SchedulingFee, …, CustomLabel`.
- Match results back to your rows by `CustomLabel` (the SKU). Every row needs a
  unique one.
- To find which field is failing, split the rows A/B on the suspect field
  within one `VerifyAdd` upload. Upload 5 did this: every A row failed and
  every B row passed.

**Fees:** `VerifyAdd` estimated **$0.35 InsertionFee per listing**, about $25.55
for all 73, and $0.00 SchedulingFee. Unverified: whether the real `Add` charged
that, or whether the free monthly listings covered it. The operator had 183 of
250 in use. Check the `Add` results file or the invoice.

## 3. Field values that worked

| Column | Value | Notes |
|---|---|---|
| `*Action` | `VerifyAdd`, then `Add` | |
| `CustomLabel` | SKU, unique per row | Patterns in §6. |
| `*Category` | `24541` | |
| `*Title` | ≤ 80 characters | The generator asserted the limit; the longest title used all 80. |
| `ScheduleTime` | `2026-10-12 00:00:00` | **GMT.** This one is Sun Oct 11, 8 PM ET. It's when the listing goes live, nothing to do with auctions. |
| `*ConditionID` | `3000` | Used. |
| `*Format` | `FixedPrice` | **This is Buy It Now.** An auction would be `Auction`. |
| `*Duration` | `GTC` | Good 'Til Cancelled. Fixed price only. |
| `*StartPrice` | the price | For `FixedPrice` the price goes here. |
| `BuyItNowPrice` | blank | Only used on auctions. |
| `*Quantity` | units in the listing | |
| `PicURL` | eBay-hosted image URL | Every listing led with the operator's logo, `https://i.ebayimg.com/images/g/c~oAAeSw4QFpPckz/s-l1600.jpg`. The link was supplied as `.webp`; it was used as `.jpg`. |
| `*Description` | HTML, one `<p>…</p>` per paragraph | Plain newlines don't render. |
| `*Location` | `16686` | |
| `ShippingType` | `Flat` | |
| `ShippingService-1:Option` | `USPSParcel` | USPS Ground Advantage. |
| `ShippingService-1:Cost` | `7.00` for sets (incl. `JOE-SET`), `6.00` for singles | |
| `ShippingService-1:AdditionalCost` | `0.50` | Each additional unit. |
| `ShippingService-2:Option` / `:Cost` | `Pickup` / `0.00` | Free local pickup. |
| `ShippingDiscountProfileID` | **blank** | Upload 5 above. |
| (international) | — | The template has no international shipping columns; with none added, the listings ship domestic only. |
| `*DispatchTimeMax` | `3` | Handling time in days. |
| `*ReturnsAcceptedOption` | `ReturnsAccepted` | Returns were spelled out too, rather than tested against the account policy. |
| `ReturnsWithinOption` | `Days_30` | |
| `RefundOption` | `MoneyBack` | |
| `ShippingCostPaidByOption` | `Buyer` | Return shipping. |

### Item specifics (`C:` columns)

| Column | Value |
|---|---|
| `C:Product` | `Pin, Button` |
| `C:Team` | `Penn State Nittany Lions` |
| `C:Sport` | `Football` |
| `C:Brand` | the bank, by year; see below |
| `C:Player` | the head coach, by year; see below |
| `C:Pre & Post Season` | `Regular Season`, `Bowl Game`, or `Playoffs` (the CFP first-round button) |
| `C:Size` | `2.25in` |
| `C:Color` | `Blue and white` |
| `C:Gender` | `Unisex Adult` |
| `C:Country of Origin` | `United States` |
| `C:Modified Item` | `No` |

- **Brand** follows `config.ERAS`: Central Counties Bank 1972–1983, Mellon Bank
  1984–2001, Citizens Bank 2001–2026.
  - 2001 sits in both ranges. Its bank is decided per button, never guessed.
    None of this batch was from 2001.
  - A multi-bank listing takes a comma list. The Joe Paterno set used
    `Central Counties Bank, Mellon Bank, Citizens Bank`, which passed.
- **Player** follows the operator's rule: Joe Paterno 1972–2010, Bill O'Brien
  2012–2013, James Franklin 2014–2025. The rule doesn't cover 2011, so 2011
  rows were left blank.

## 4. Titles and descriptions

**Title patterns:**
- Sets: `1974 Penn State Central Counties Bank Buttons Season Set Complete w/ Cotton Bowl`
- Bowls: `1972 Penn State Football Sugar Bowl Button Central Counties Bank Oklahoma`
- Variants: `1972 Penn State Central Counties Bank "Hammer the Hawkeyes" No Logo Green Back`

**Description:** in the operator's own format, one `<p>` per line:
1. An opener:
   - Sets: "Commemorate the `YEAR` Penn State Football season with the official
     `BANK` GameDay Buttons / Pins / Pinbacks." If the set includes its bowl
     button, add "This set is complete with a button for the `YEAR` `BOWL`
     against the `OPPONENT`."
   - Single buttons: the same sentence, naming the one button's bowl or game.
2. One `<p>` per slogan, exactly as printed on the button.
3. The closing line, word for word: "The buttons were picked up from the bank
   and some worn to games. They have various imperfections that make them
   unique. Please inspect the photos and bid with confidence that I will take
   the utmost care of these items."

**Left out:** game lines (opponent, date, score per slogan). The schedule sites
were blocked from the session container.

## 5. The operator's listing rules (2026-10-03)

**What to list:**
- Full season sets first, then bowl buttons.
- No 1972 or 1973 sets.
- No 2021–2023 season sets. Their bowl buttons do get listed.

**How many:**
- **Sets:** if more than 3 complete sets can be made, list 2 fewer than that,
  so 2 stay in reserve.
- **Bowl buttons:** only the copies left over after the complete sets are
  counted.
- **Cap:** no listing goes above **5**.
- **Available stock** = the Inventory sheet's count minus what live listings
  already hold.

**Prices:**
- **Sets and bowl buttons:** match kling24toys **exactly** where he lists the
  same item. Don't undercut: "I don't want to start a race to the bottom."
  - Pull his prices with `/crawl seller kling24toys`, which writes the
    `seller_kling24toys` tab in the Logger workbook.
  - Anything he doesn't list is priced from the operator's own nearby years.
- **Joe Paterno:** the set of 12, unframed, is **$45** (quantity 1); singles
  are **$8** each.
- **Pitt singles:** $8. The session proposed it and the file was uploaded with it.

**1972 buttons:**
- Logo vs no logo, and green back vs metal back, are priced differently, so
  each variant is its own listing.
- Only variants with enough stock get listed. This time that was Hammer NL,
  Wallop NL, Wallop logo, Trip NL, Trip logo, plus the 1972 Sugar Bowl button.
- The operator's photos of the collection are in Slack `#general-sorting`.

**Shipping and returns:**
- Shipping: $7 for sets, $6 for singles, +$0.50 for each additional unit.
- Free local pickup at 16686. Domestic only.
- Handling time: 3 days.
- Returns: within 30 days; the buyer pays return shipping.

## 6. SKUs (`CustomLabel`)

| Pattern | Example |
|---|---|
| `SET-<year>` | `SET-1974` |
| `BOWL-<year>`, with a suffix when a season has two | `BOWL-2024-CFP1` |
| `JOE-SET` | `JOE-SET` |
| `JOE-<year>-<SLUG>` | `JOE-1980-JUICE-FOR-JOE` |
| `S72-1972-<NAME>-<NL|LOGO>` | `S72-1972-WALLOP-NL` |
| `PITT-<year>` | `PITT-1980` |

## 7. After the upload

- **Combined shipping:** apply the "Fifty Cent" rule to the new listings in
  Seller Hub. It can't go in the file.
- **Photos:** each listing has only the logo photo. Add the button photos
  before the `ScheduleTime`.
- **Older 1972 listing:** a 1972 Central Counties set listing ($100,
  quantity 2) was already live and draws on the same 1972 stock as the new
  singles. Watch for double-selling.

## 8. Next time, in order

1. Get stock from the Inventory sheet and live listings from eBay's "all active
   listings" report. Available = stock − what's already listed.
2. Run `/crawl seller kling24toys` for current prices.
3. Build the rows with the values in §3–§6.
4. Upload with `VerifyAdd`. Repeat until every row reads `Success`, and read
   `InsertionFee`.
5. Change `*Action` to `Add`, set `ScheduleTime`, and upload.
6. Do the §7 steps.

**What not to commit:**
- **Never commit** eBay's **orders report**. It carries buyer names and
  addresses, and this repo is public.
- The listing upload file is listing text and prices only: what the public sees
  on eBay. That's why it's here.
