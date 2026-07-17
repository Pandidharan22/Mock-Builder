"""Is a storefront usable as MockBuilder's demo site? (Step 10c-vet)

Run:
    python tools/vet_storefront.py <url> [<url> ...]

WHY THIS EXISTS. scrapingcourse was vetted BY EYE on 2026-07-10 and recorded as
having a real cart (item rows, quantity control, proceed-to-checkout). By
2026-07-17 its cart page rendered "Your cart is currently empty!" regardless of
what you added — while the header badge still read "$7.00 1 item". A human
looking at that badge would say the cart works. The pipeline cannot see it:
`normalize_dom` strips every text node before hashing, so a text-only badge is
invisible to every downstream stage. A site can therefore pass any visual check
and still be useless.

So THE decisive criterion is a hash delta, computed with the crawler's own
`normalize_dom` and hash — not a screenshot, not a page-text grep:

    h_bare = hash(cart reached via [cart])          # never added
    h_add  = hash(cart reached via [add, cart])     # added first
    PASS iff BOTH paths landed on the cart AND h_add != h_bare

The landing check is not a formality — it is the other half of the gate, learned
by being fooled. demowebshop's first "Add to cart" is a $25 gift card that needs
configuring, so clicking it NAVIGATES to the product page instead of adding. The
probe then hashed a PRODUCT page against the CART page, found them different, and
reported PASS. A hash delta alone only proves two pages differ — it does not prove
an add mutated a cart. Both paths must end on the same page for the delta to mean
anything.

Everything else this probe prints is corroborating detail. If the delta fails,
the site is unusable no matter how populated it looks, because the add produces
no change any later stage can observe — which also means the P-state verifier
property ("a declared mutateState causes an asserted DOM change") can never pass
against it.

Detection reuses the crawler's real rules (label for actions, label-or-target for
navigation, clickability filtering). The probe must NEVER bend the pipeline to
make a site pass: if a site fails, it fails, and we pick another.

NOTE ON THE CONFIRMATION LEG. Reaching an order-received screen requires actually
placing an order. That is only acceptable on a demo/sandbox store with fake
payment, and it needs site-specific form filling, so this probe does not automate
it — it reports whether a real checkout FORM is reachable and leaves the
confirmation leg to a deliberate, per-candidate check.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from mockbuilder.crawler.crawler import (  # noqa: E402
    Crawler,
    _is_add_to_cart,
    _is_cart_link,
    _normalize_label,
)
from mockbuilder.crawler.dom import discover_elements, normalize_dom  # noqa: E402
from mockbuilder.crawler.records import extract_records_async  # noqa: E402

# A checkout link, by label or target. `cart` is excluded from the target rule
# because real carts live UNDER /checkout/ on some platforms (Magento's cart is
# /checkout/cart/), and that link is the cart, not the checkout.
_CHECKOUT_LABEL_RE = re.compile(
    r"(?:proceed to |go to |continue to )?(?:checkout|check out|place order)"
)


def _is_checkout_link(el: dict[str, Any]) -> bool:
    if _CHECKOUT_LABEL_RE.fullmatch(_normalize_label(el.get("text"))):
        return True
    href = el.get("href")
    if not href:
        return False
    segments = [s.lower() for s in urlparse(href).path.split("/") if s]
    return "checkout" in segments and "cart" not in segments


async def _hash_of(page: Any) -> str:
    """The crawler's own state identity — not a screenshot, not page text."""
    return hashlib.sha256((await normalize_dom(page)).encode("utf-8")).hexdigest()


# How long to let an action's side effect actually happen before moving on.
_SETTLE_MS = 3000


async def _settle(page: Any) -> None:
    """Wait for an ACTION's effect, not merely for the page to be quiet.

    `wait_for_load_state("networkidle")` alone is a TRAP after clicking an AJAX
    control: it resolves the moment the page is idle, and right after a click the
    page is STILL idle because the XHR has not been issued yet. It therefore
    returns instantly and we navigate away before the request is even sent — the
    add is silently lost and the cart looks untouched.

    Measured on demowebshop: badge reads "Shopping cart (0)" immediately after
    click+networkidle, and "Shopping cart (1)" ~3s later. scrapingcourse hid this
    because its add is `href="?add-to-cart=..."` — a real navigation, which
    networkidle does wait for. Only pure-AJAX adds expose it.

    So: give the effect a fixed window to start and finish, THEN confirm idle.
    Fixed rather than clever — the goal is a measurement that never under-waits,
    and the same wait every run keeps the result reproducible.
    """
    await page.wait_for_timeout(_SETTLE_MS)
    try:
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass  # already idle, or still chattering — the fixed window is the floor


async def _reach(browser: Any, url: str, clicks: list[str]) -> Any:
    """Navigate to `url` and replay `clicks` on ONE page, as the crawler does.

    A fresh page per call, so each measurement starts from a clean session — the
    add must be what carries state, not a leaked cookie.
    """
    page = await browser.new_page()
    await page.goto(url, wait_until="networkidle")
    for selector in clicks:
        await page.click(selector, timeout=8000)
        await _settle(page)
    return page


def _fmt(ok: bool | None) -> str:
    return {True: "PASS", False: "FAIL", None: "  ? "}[ok]


async def vet(browser: Any, url: str) -> dict[str, Any]:
    result: dict[str, Any] = {"url": url}
    print("=" * 78)
    print(f"CANDIDATE: {url}")
    print("=" * 78)

    # --- 1. Landing grid + affordances ---------------------------------------
    page = await _reach(browser, url, [])
    elements = await discover_elements(page)
    clickable = await Crawler._filter_clickable(page, elements)
    extraction = await extract_records_async(page)
    grid = max((c.count for c in extraction.collections), default=0)

    adds = [e for e in clickable if _is_add_to_cart(e)]
    carts = [e for e in clickable if _is_cart_link(e)]
    result["grid"] = grid
    result["adds"] = len(adds)
    print(f"  landing: {len(elements)} elements, {len(clickable)} clickable")
    print(f"  largest repeating collection : {grid}")
    print(f"  clickable add-to-cart        : {len(adds)}")
    print(f"  clickable cart links         : {len(carts)}")
    if carts:
        print(f"    cart link -> {carts[0]['text']!r}  {carts[0].get('href')}")
    await page.close()

    if not adds or not carts:
        # No affordance on the landing grid -> 10-pre synthesizes nothing here.
        # An honest structural fact about the site, not a probe failure.
        result["delta_pass"] = False
        result["reason"] = (
            "no clickable add-to-cart on landing" if not adds else "no cart link"
        )
        print(f"\n  DECISIVE (cart hash delta): {_fmt(False)} — {result['reason']}")
        return result

    add, cart = adds[0], carts[0]

    # --- 2. THE DECISIVE CRITERION -------------------------------------------
    bare = await _reach(browser, url, [cart["selector"]])
    h_bare, u_bare = await _hash_of(bare), bare.url
    await bare.close()

    added = await _reach(browser, url, [add["selector"], cart["selector"]])
    h_add, u_add = await _hash_of(added), added.url

    # Both paths must END ON THE SAME PAGE, or the delta compares two different
    # screens and means nothing (see the module docstring: a configurable product
    # navigates instead of adding, and the delta then "passes" against a product
    # page). Compare paths, not full URLs — a query string is not a different page.
    landed = urlparse(u_add).path.rstrip("/") == urlparse(u_bare).path.rstrip("/")
    delta = h_add != h_bare
    passed = landed and delta
    result.update({"h_bare": h_bare, "h_add": h_add, "u_bare": u_bare,
                   "u_add": u_add, "landed_same": landed, "delta_pass": passed})
    print(f"\n  cart via [cart]      : {u_bare}")
    print(f"                         {h_bare[:32]}")
    print(f"  cart via [add, cart] : {u_add}")
    print(f"                         {h_add[:32]}")
    print(f"  both landed on cart  : {_fmt(landed)}"
          f"{'' if landed else '  <- the add NAVIGATED instead of adding'}")
    print(f"  hashes differ        : {_fmt(delta)}"
          f"{'' if delta else '  <- add is INVISIBLE to the pipeline'}")
    print(f"  DECISIVE             : {_fmt(passed)}")
    if not passed:
        result["reason"] = (
            "add navigates, not adds" if not landed else "cart hash identical"
        )

    # --- 3. Checkout leg (from the POPULATED cart) ---------------------------
    cart_elements = await discover_elements(added)
    cart_clickable = await Crawler._filter_clickable(added, cart_elements)
    checkouts = [e for e in cart_clickable if _is_checkout_link(e)]
    result["checkout_affordance"] = len(checkouts)
    print(f"\n  checkout affordance on cart   : {len(checkouts)}")

    if checkouts and passed:
        try:
            await added.click(checkouts[0]["selector"], timeout=8000)
            await added.wait_for_load_state("networkidle")
            final = added.url
            inputs = await added.locator(
                "input:visible, select:visible, textarea:visible"
            ).count()
            redirected = "checkout" not in urlparse(final).path.lower()
            result.update(
                {"checkout_url": final, "checkout_inputs": inputs,
                 "checkout_real": bool(inputs >= 3 and not redirected)}
            )
            print(f"    landed        : {final}")
            print(f"    visible fields: {inputs}")
            print(f"    checkout form : {_fmt(result['checkout_real'])}"
                  f"{'  <- redirected away' if redirected else ''}")
        except Exception as exc:
            result["checkout_real"] = False
            print(f"    checkout click FAILED: {type(exc).__name__}")
    await added.close()

    # --- 4. Confirmation leg -------------------------------------------------
    # Deliberately not automated: placing an order needs site-specific form fill
    # and is only acceptable on a sandbox with fake payment. Reported, not guessed.
    result["confirmation"] = "not automated — see report"
    return result


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("urls", nargs="+")
    args = ap.parse_args()

    stamp = datetime.datetime.now(datetime.UTC).isoformat()
    print(f"vet_storefront — run at {stamp}\n")

    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for url in args.urls:
                try:
                    results.append(await vet(browser, url))
                except Exception as exc:
                    print(f"  PROBE ERROR: {type(exc).__name__}: {exc}")
                    results.append({"url": url, "delta_pass": False,
                                    "reason": f"probe error: {type(exc).__name__}"})
                print()
        finally:
            await browser.close()

    print("=" * 78)
    print(f"SUMMARY (run {stamp})")
    print("=" * 78)
    for r in results:
        print(f"  {_fmt(r.get('delta_pass'))}  {r['url']}")
        print(f"        grid={r.get('grid')} adds={r.get('adds')} "
              f"checkout_form={r.get('checkout_real')} "
              f"{r.get('reason', '')}")
    winners = [r for r in results if r.get("delta_pass")]
    print(f"\n  {len(winners)} of {len(results)} candidate(s) pass the decisive criterion.")
    return 0 if winners else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
