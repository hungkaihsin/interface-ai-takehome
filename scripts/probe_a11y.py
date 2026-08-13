"""Throwaway probe: can we perceive and act on this app purely via the a11y tree?

Answers three questions before we commit to the perception design:
  1. Does aria_snapshot() reach inside the iframes at all?
  2. Do the *labelled* controls (search form) expose usable accessible names?
  3. What do the *unlabelled* controls (sub-account form) look like -- i.e. how
     bad is the degraded case we'll need a fallback for?
"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5001"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()

    page.goto(f"{BASE}/login")
    print("=== LOGIN PAGE a11y ===")
    print(page.locator("body").aria_snapshot())

    page.get_by_label("Operator ID").fill("operator")
    page.get_by_label("Password").fill("letmein")
    page.get_by_role("button", name="Sign On").click()
    page.wait_for_url(f"{BASE}/shell")

    print("\n=== SHELL: frame inventory ===")
    for i, fr in enumerate(page.frames):
        print(f"  [{i}] name={fr.name!r:8} url={fr.url}")

    print("\n=== TOP DOCUMENT a11y (expect: no app controls) ===")
    print(page.locator("body").aria_snapshot())

    main = page.frame(name="main")
    print("\n=== 'main' FRAME a11y (search screen) ===")
    print(main.locator("body").aria_snapshot())

    # Act inside the frame using role + accessible name only.
    main.get_by_label("Member Number").fill("10001")
    main.get_by_role("button", name="Search").click()
    page.wait_for_timeout(600)

    main = page.frame(name="main")
    print("\n=== MEMBER RECORD a11y (truncated) ===")
    snap = main.locator("body").aria_snapshot()
    print("\n".join(snap.splitlines()[:40]))

    # Can we read the savings balance structurally, with no ids to lean on?
    row = main.locator("tr", has=main.locator("td", has_text="SAVINGS")).first
    print("\nsavings row cells:", row.locator("td").all_inner_texts())

    # The degraded form.
    main.get_by_role("button", name="Open Sub-Account").click()
    page.wait_for_timeout(600)
    main = page.frame(name="main")
    print("\n=== SUB-ACCOUNT FORM a11y (the UNLABELLED case) ===")
    print(main.locator("body").aria_snapshot())

    browser.close()
