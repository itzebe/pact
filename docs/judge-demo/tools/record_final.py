"""Record the final PACT demo footage (silent, credential-free) and log beat times.

One continuous Chromium video of the live production app. The PayPal Sandbox
approval runs in a SEPARATE, UNRECORDED context, so the PayPal login page never
appears in the video; the PACT approval panel's Sandbox buyer hint is redacted in
the DOM before any frame shows it.

Writes:
  /tmp/final_rec/*.webm      the walkthrough
  /tmp/final_rec/beats.json  wall-clock start of each beat, seconds from t0
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import time
from playwright.sync_api import sync_playwright

APP = "https://pact-flame.vercel.app"
OUT = "/tmp/final_rec"
W, H = 1600, 900
APPROVE = "Waiting for your approval in PayPal"
SETTLED = "Paid — captured from escrow"
REFUNDED = "Payment refunded"
BRIEF = ("Write the hero headline, three feature blocks, and a call to action for the "
         "launch page of a small business payments product.")
CRITERIA = "Hero headline is under 12 words\nExactly three feature blocks\nA single clear call to action"

TITLE_HTML = """<!doctype html><meta charset=utf-8>
<style>html,body{margin:0;height:100%;background:#0b0f14;color:#e8edf2;
font-family:'DejaVu Sans',system-ui,sans-serif;display:flex;align-items:center;justify-content:center}
div{max-width:1100px;text-align:center}h1{font-size:64px;margin:0 0 22px;font-weight:600;letter-spacing:-1px}
p{font-size:30px;color:#9fb0c0;margin:0}span{color:#7cc4ff}</style>
<div><h1>Two strangers. One agreement. Two fears.</h1>
<p>One is afraid to pay and get nothing. The other is afraid to deliver and never get paid.</p></div>"""

CLOSE_HTML = """<!doctype html><meta charset=utf-8>
<style>html,body{margin:0;height:100%;background:#0b0f14;color:#e8edf2;
font-family:'DejaVu Sans',system-ui,sans-serif;display:flex;align-items:center;justify-content:center}
div{max-width:1150px;text-align:center}h1{font-size:52px;margin:0 0 26px;font-weight:600;line-height:1.25;letter-spacing:-.5px}
p{font-size:26px;color:#9fb0c0;margin:0 0 8px}code{font-size:24px;color:#7cc4ff}</style>
<div><h1>PACT doesn't ask strangers to trust each other blindly.<br>It gives their agreement a payment lifecycle they can verify.</h1>
<p><code>pact-flame.vercel.app</code> &nbsp;·&nbsp; <code>github.com/itzebe/pact</code></p>
<p>PayPal Sandbox · live model · durable Postgres</p></div>"""


def read_buyer(page):
    return page.evaluate(
        """() => {
            const n=[...document.querySelectorAll('div')]
              .find(d=>d.textContent.trim()==='Approve with this PayPal Sandbox test buyer');
            if(!n) return null;
            const row=n.parentElement.querySelector('div.font-mono'); if(!row) return null;
            const s=row.querySelectorAll('span');
            return {email:s[0].textContent.trim(), password:s[1].textContent.trim()};
        }"""
    )


def redact(page):
    page.evaluate(
        """() => {
            const n=[...document.querySelectorAll('div')]
              .find(d=>d.textContent.trim()==='Approve with this PayPal Sandbox test buyer');
            if(!n) return;
            const row=n.parentElement.querySelector('div.font-mono'); if(!row) return;
            row.querySelectorAll('span').forEach((s,i)=>{
              s.textContent = i===0 ? 'sandbox-buyer@example.com' : '\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022';
            });
        }"""
    )


def approve_in_paypal(browser, approval_url, buyer):
    ctx = browser.new_context()          # NOT recorded
    try:
        pay = ctx.new_page()
        pay.goto(approval_url, wait_until="domcontentloaded", timeout=60000)
        pay.wait_for_timeout(2500)
        pay.fill("input#email", buyer["email"])
        pay.click("button#btnNext")
        pay.wait_for_timeout(3500)
        pay.fill("input#password", buyer["password"])
        pay.click("button#btnLogin")
        pay.wait_for_timeout(5000)
        pay.click('button:has-text("Complete Purchase")')
        try:
            pay.wait_for_url(lambda u: "pact-flame.vercel.app" in u, timeout=60000)
        except Exception:
            pass
        return True
    except Exception as e:
        return f"error:{str(e)[:100]}"
    finally:
        ctx.close()


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    beats, log = {}, {}
    t0 = time.monotonic()
    mark = lambda k: beats.__setitem__(k, round(time.monotonic() - t0, 2))

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": W, "height": H},
                            record_video_dir=OUT, record_video_size={"width": W, "height": H})
        page = ctx.new_page()
        # Beat 1 — title card
        mark("b1")
        page.set_content(TITLE_HTML, wait_until="load")
        page.wait_for_timeout(8000)
        # Beat 2 — product opens (guest workspace)
        page.goto(APP, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        mark("b2")
        page.wait_for_timeout(15000)
        # Beat 3 — coordination
        page.click('button:has-text("Launch copy")')
        page.fill('input.field', "Landing page copy for a fintech launch")
        page.fill('textarea.field >> nth=0', BRIEF)
        page.fill('textarea.field >> nth=1', CRITERIA)
        page.wait_for_timeout(2000)
        page.click('button:has-text("Post job and settle")')
        for _ in range(70):
            page.wait_for_timeout(1000)
            if APPROVE in page.inner_text("body"):
                break
        buyer = read_buyer(page)
        redact(page)
        log["panel"] = buyer is not None
        mark("b3")
        page.locator('button:has-text("Negotiation")').first.click()
        page.wait_for_timeout(2000)
        page.wait_for_timeout(17000)          # beat 3 window
        # Beat 4 — payment approval
        mark("b4")
        approval_url = None
        try:
            approval_url = page.get_attribute('a:has-text("Open PayPal approval")', "href")
        except Exception:
            pass
        page.wait_for_timeout(15000)          # hold on the approval panel
        if buyer and approval_url:
            log["paypal"] = approve_in_paypal(b, approval_url, buyer)
            page.bring_to_front()
            page.wait_for_timeout(1500)
            try:
                page.click('button:has-text("I approved")', timeout=20000)
            except Exception:
                pass
            terminal = None
            for _ in range(70):
                page.wait_for_timeout(2000)
                t = page.inner_text("body")
                if SETTLED in t:
                    terminal = "settled"; break
                if "Nothing charged" in t:
                    terminal = "voided"; break
            log["terminal"] = terminal
            # Beat 5 — proof (settled + ledger)
            mark("b5")
            page.wait_for_timeout(9000)
            page.locator('button:has-text("Ledger")').first.click()
            page.wait_for_timeout(9000)
            page.locator('button:has-text("Negotiation")').first.click()
            page.wait_for_timeout(3000)
            # Beat 6 — protection (refund)
            mark("b6")
            if terminal == "settled":
                try:
                    page.click('button:has-text("Refund this payment")', timeout=10000)
                    page.wait_for_timeout(800)
                    page.click('button:has-text("Confirm refund")', timeout=10000)
                    for _ in range(40):
                        page.wait_for_timeout(1500)
                        if REFUNDED in page.inner_text("body"):
                            break
                    log["refunded"] = REFUNDED in page.inner_text("body")
                except Exception as e:
                    log["refund_error"] = str(e)[:120]
            page.wait_for_timeout(12000)
        # Beat 7 — closing card
        mark("b7")
        page.set_content(CLOSE_HTML, wait_until="load")
        page.wait_for_timeout(9000)
        ctx.close()
        b.close()

    json.dump({"beats": beats, "log": log}, open(f"{OUT}/beats.json", "w"), indent=1)
    vids = sorted(glob.glob(f"{OUT}/*.webm"), key=os.path.getsize, reverse=True)
    print("LOG:", json.dumps(log))
    print("BEATS:", json.dumps(beats))
    print("VIDEOS:", [(os.path.basename(v), os.path.getsize(v)) for v in vids])


if __name__ == "__main__":
    main()
