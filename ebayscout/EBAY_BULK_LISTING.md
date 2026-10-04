# eBay bulk listing upload: what worked (2026-10-03)

On 2026-10-03 the operator created **73 listings (308 buttons, $3,171 asking)**
with one Seller Hub bulk upload. That exact file is in the repo at
`listing_uploads/2026-10-03_penn-state-button-listings.csv`. It took six uploads
to get right. Three more uploads then put a photo on 70 of them (§9). This note
records what each failure taught, so the next batch works on the first real
upload.

Afterwards the operator ended `SET-1982` and `BOWL-1982` because too few 1982
buttons were in stock.

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
for all 73. **The real `Add` charged $0.00** on every row, including
`SchedulingFee`. The free monthly listings covered them: the operator had 183
of 250 in use. Expect the `VerifyAdd` estimate to read high when free listings
remain.

**Where the results file is:** Seller Hub → Reports → **Uploads**. The list of
past uploads below the upload box has a **Download results** link on each row
once eBay has processed it. The `Add` results file is the only place the new
listings' **item numbers** (`ItemID`) come back, keyed by `CustomLabel`. Keep
it: any later bulk revise needs those numbers.

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
| `BestOfferEnabled` | **`1`** | **Allow offers on every listing.** The 2026-10-03 file left this blank and the operator turned offers on by hand afterwards; set it in the file next time. `1` = on, `0` = off, per an eBay community answer. Not yet run through `VerifyAdd`. |
| `BestOfferAutoAcceptPrice`, `MinimumBestOfferPrice` | blank | Optional thresholds. The operator hasn't set any. |
| `*Quantity` | units in the listing | |
| `PicURL` | image URLs joined with `|`, first one is the main photo | The `Add` file used only the operator's logo, `https://i.ebayimg.com/images/g/c~oAAeSw4QFpPckz/s-l1600.jpg` (eBay-hosted). **All URLs in one cell must come from the same kind of host**: eBay-hosted and self-hosted together fail with `20004` (§9). Next time put the Drive photo links here directly (§9). |
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

**Offers:** allowed on every listing (`BestOfferEnabled` = `1`).

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
- **Photos:** the 2026-10-03 `Add` file carried only the logo, so the photos
  went on afterwards with a bulk `Revise`, before the `ScheduleTime` (§9).
- **Older 1972 listing:** a 1972 Central Counties set listing ($100,
  quantity 2) was already live and draws on the same 1972 stock as the new
  singles. Watch for double-selling.

## 8. Next time, in order

1. Get stock from the Inventory sheet and live listings from eBay's "all active
   listings" report. Available = stock − what's already listed.
2. Run `/crawl seller kling24toys` for current prices.
3. **Photos first** (§9):
   - Crop the single buttons and name every photo by its SKU.
   - Put them, with `LOGO.jpg`, in the shared Drive folder.
4. Build the rows with the values in §3–§6:
   - Include **`BestOfferEnabled` = `1`**.
   - Fill `PicURL` with the Drive links: `<photo>|<logo>`.
5. Upload with `VerifyAdd`. Repeat until every row reads `Success`, and read
   `InsertionFee`.
6. Change `*Action` to `Add`, set `ScheduleTime`, and upload.
7. Download and keep the `Add` results file (the item numbers).
8. Do the §7 steps.

Putting Drive links in an `Add` file instead of a `Revise` hasn't been tried
yet. It uses the same `PicURL` column, and `VerifyAdd` will show whether eBay
accepts it. If it doesn't, fall back to the §9 `Revise` route, which is proven.

**What not to commit:**
- **Never commit** eBay's **orders report**. It carries buyer names and
  addresses, and this repo is public.
- The listing upload file is listing text and prices only: what the public sees
  on eBay. That's why it's here.

## 9. Photos: cropped from the season photos, hosted on Drive (2026-10-03)

After the `Add`, 70 listings got photos in **one bulk `Revise` upload**. The
photos were 16 season-set photos and 54 single-button crops, each followed by
the logo. All 70 rows returned `Success` with $0.00 fees.

**Listings left without a photo:**
- `BOWL-2025` ("Lions De-Stripe Tigers"): no photo of that button was found.
- The two 1982 listings: the operator ended them.

### Where the photos came from

- **Season photos:** the operator's Drive folder of season photos, one per
  year, 1973–2024, each named by its year.
  - Each set listing uses its year's photo as is.
  - Check that each photo shows exactly the listing's buttons. The 1982 photo
    held a 13th button, "National Champions 1982", that wasn't in the set.
- **Single buttons:** cropped out of the season photos (below).
- **1972 variants:**
  - Most came from the photos posted to Slack `#general-sorting` on
    2026-09-29, one photo per variant and count.
  - "Trip the Terrapins" no-logo came from the Drive photo
    `1972 Trip No Logo Front` in the shared `1972 - 1996+` folder.
- **When a crop is spoiled, use an older photo.** In the 2026 season photo,
  "Pitt Isn't It" (1980) is overlapped by its neighbor. The older
  `1980 front` photo in the same `1972 - 1996+` folder has it clear.
- **Joe Paterno set:** a 4×3 grid of the 12 single crops. It is not a photo of
  the physical set.

### Cropping (done in the session, not in the repo)

1. **Find each button:** OpenCV Hough-circle detection on a 1000px-wide copy,
   drawn as a numbered overlay.
2. **Match buttons to listings by eye:** read each overlay and match the slogan
   to the SKU.
3. **Refine and crop:** refine the circle at full size with
   `HOUGH_GRADIENT_ALT`. Crop a square of side **2 × 1.2 × r**, where r is the
   larger of the detected and refined radius.
   - At 1.13 × r, rims were clipped.
   - The 1993 bowl button's circle was wrong and was placed by hand.
4. **Review** every crop on a contact sheet before shipping.
   - Crops came out 590–1600px, above eBay's 500px minimum.

### Getting them to Drive

- **The session can download** Drive and Slack images. Large tool results land
  on disk, not in the conversation.
- **The session can't upload** images to Drive. The connector takes file
  contents as text, which is impractical for 54 photos.
  - It can **copy** Drive files server-side, which is how the set photos got
    into the folder.
  - The crops went to the operator as a zip, named `<SKU>.jpg`, and the
    operator dragged them into the folder.
- **Check the copies:** each Drive copy of a crop was compared with the local
  file's size to make sure the right photo went to the right listing.
- **Folder:** a dedicated `eBay listing photos` folder holding only listing
  photos, set to **Anyone with the link → Viewer**. eBay can't fetch a private
  file.
- **Keep the folder shared** until a listing's photos show `i.ebayimg.com`
  addresses. Whether eBay keeps its own copy of self-hosted photos is
  unverified.

### The `Revise` file

```
*Action(SiteID=US|Country=US|Currency=USD|Version=1193|CC=UTF-8),ItemID,PicURL
Revise,<item number>,https://lh3.googleusercontent.com/d/<photo file id>|https://lh3.googleusercontent.com/d/<LOGO.jpg file id>
```

- **Item numbers:** from the `Add` results file.
- **`Revise` replaces every photo on the listing,** so each row re-sends the
  logo.

### Uploads and what each taught

| Upload | Result | Lesson |
|---|---|---|
| Test 1: Drive photo + the eBay-hosted logo | Both rows failed with `20004`, "A mixture of Self Hosted and EPS pictures are not allowed." | **All photos on a listing must come from the same kind of host.** The logo went into the Drive folder as `LOGO.jpg`. |
| Test 2: Drive photo + Drive logo, two link formats | Both rows `Success`. | `https://lh3.googleusercontent.com/d/<id>` was confirmed on the live listing: the 1975 set showed the photo, then the logo. `https://drive.google.com/uc?export=view&id=<id>` also returned `Success`, but nobody looked at the listing. |
| All 70 rows, `lh3` links | 70/70 `Success`, $0.00. | The route works. |

**Two-row test first.** Pick two real listings, one per link style, and look
at them in Seller Hub before sending the full file.

**eBay's own pages are blocked from the session container.** The `20004` fix
came from reading the error itself.
