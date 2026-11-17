---
name: verify
description: "Use before committing any change a person could see or a client could be told: a change to the field panel, the cards, a route they read, or the numbers and words they show. Also when the user says \"verify\", \"did you run it\", \"open it in a browser\", or \"prove it works\". Starts a no-sign-in copy of the app on its own database, runs the browser tests against it, and has you read the screenshot."
---

# Verify: run the change the way a person would

## Principle

Tests passing is not the evidence. The evidence is the change running: the page opened, the field picked, the
words read. A unit test can pass while the feature is broken, because neither the test nor its writer saw the
whole task.

## Why a copy of the app

The app on `:8000` is built with WorkOS sign-in (fixed when the frontend is built) and reads the live local
database `mundidb`. A browser test can use neither, and no test may sign in for a person. `scripts/e2e_app.sh`
starts a second copy on `:8011`: no sign-in, database `mundidb_e2e`, the field switch on, no model key (so no
paid call), serving this checkout's `src/` and this checkout's frontend.

## Steps

1. Start the copy and seed the test farm (about 2 minutes; the frontend is built each time):

   ```bash
   scripts/e2e_app.sh up
   ```

   Backend-only change with the copy already up: `docker restart mundi-app-e2e`, then `scripts/e2e_app.sh seed`.

2. Run the browser tests:

   ```bash
   scripts/e2e_app.sh test
   ```

   If Playwright says its browser is missing, do not download one unasked. Point it at one already here:
   `E2E_CHROMIUM=$(ls -d ~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell | tail -1)`.

3. Read the screenshot each test keeps under `frontendts/test-results/` (Read tool). Look at the whole picture,
   not only the words the test asserts: something covered, cut off or missing is a finding even when the test
   is green. Say what you saw.

4. If your change has no browser test yet, add its flow to `frontendts/e2e/` first: what a person opens, what
   they click, what they read. Data it needs goes into `scripts/e2e_seed.py`, made so the right answer is
   known beforehand (the two test flights are uniform pictures: NDVI 0.50 and 0.80 by arithmetic).

5. Check the test can fail: change the expected value once, see it fail, put it back. A test that cannot fail
   proves nothing.

6. Stop the copy when done: `scripts/e2e_app.sh down`.

## Rules

- A failing check is fixed in the code under test. Never weaken, skip, retry or add a wait to a test to get
  green. If the same failure is still there after three rounds, stop and bring it to Roger with the screenshot.
- Never point the copy or the seed at `mundidb`; both refuse it.
- What the browser run could not cover (a real drone photo, the tap model, Sage's answers) is said plainly in
  the report, not left out.
- Say in the commit message how the change was verified: `Verified: browser (scripts/e2e_app.sh test)`,
  `Verified: tests only`, or `Verified: none`, with the reason for the last two.
