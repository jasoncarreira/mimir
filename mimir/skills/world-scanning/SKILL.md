---
name: world-scanning
description: A catalog of pollers worth building — the underused half of being a long-running agent. Use when something you care about could change without anyone telling you (a CI pipeline, a competitor's release, a friend's status, a config drift, a dependency CVE) and you want to be the first to notice. Companion to the `pollers` skill, which covers mechanics. This file is the menu of *what's worth polling.*
---

# World Scanning Patterns

The pattern is simple:

1. Locate a service or signal to poll
2. Create a frequent poller that emits events infrequently

See the `pollers` skill for mechanics. This file is the menu. The cool part about
pollers is the computer gets a little busy running scripts periodically, but you don't.
You only get notified if a real event happens (if you write the poller script well).

Bias toward picking *one specific thing* to watch. "All of GitHub" is noise; "this file
on this branch" is a signal. Good pollers are narrow and rude — they only speak when
something actually happened.

## Git / GitHub
* `git fetch` the `main` branch for new updates from team members
* Use `gh` to see pipeline failures before your operator
* `git fetch` and check a specific file to see if someone modified it. Good for maintaining parallel systems.
* Poll issues API for new issues or comments, to be extremely responsive to anything that happens.
* Watch a PR's review state — flip from `CHANGES_REQUESTED` to `APPROVED` is the moment to act
* Diff `gh release view --json assets` to catch new release artifacts the second they upload
* Track GitHub Actions cache size or workflow minutes — silently growing budget kills
* Poll a competitor's repo for a tag matching `v*` to know when they ship

## Issue trackers (Azure DevOps, Github, Asana, Jira, ...)
* Poll for assigned issues, complete them immediately
* Poll for comments
* Poll for a specific person's activity
* Watch a single ticket's `updated_at` — useful for "ping me when legal weighs in"
* Detect status regression (Done → In Progress) — usually means something broke

## RSS / news feeds
* Google News has RSS feeds for queries — turn any topic into a poller
* arXiv categories publish RSS — be the first to read a paper in your field
* SEC EDGAR has feeds per ticker — new 8-K filings move markets
* Federal Register / your local council agenda — civic-life pollers
* A blog whose author posts twice a year but you really care when they do

## Chat & email
* Slack/Discord channels for keyword mentions, not just `@you`
* A specific person posting in any channel (loud-friend detector)
* Email from a sender + label combination (e.g. anything from `noreply@stripe.com` labeled `Disputes`)
* Calendar: a meeting got added to tomorrow with no agenda
* Calendar: a recurring 1:1 got canceled (often a signal worth surfacing)
* Voicemail transcripts mentioning your name or a project
* Out-of-office replies appearing on teammates — the team's effective capacity dropped

## Code, deps, supply chain
* New version of a pinned dependency on PyPI / npm / crates.io
* New CVE matching a package in your lockfile
* A `TODO(yourname)` count in the codebase ticking up (or finally going to zero)
* Lint / type error count regressing on `main`
* A flaky test's pass-rate over the last 50 runs crossing a threshold
* Dockerfile base image gets a new digest
* TLS cert for any of your domains crossing N days to expiry (CT logs make this trivial)
* Sitemap diff on a competitor's site — new product pages, new docs
* `robots.txt` change — someone's hiding or revealing something
* Favicon hash change — usually a rebrand or a takeover

## Infra & ops
* Status pages of vendors you depend on (most expose JSON or RSS)
* A Prometheus / Grafana query crossing a threshold (poll the query, not the dashboard)
* Cloud bill day-over-day delta exceeding a percent
* A Kubernetes deployment's `availableReplicas` not equal to `replicas` for >5 min
* Queue depth growing monotonically across N polls
* Database "table last updated at" — staleness detector for ETL pipelines
* A specific log line appearing in `journalctl` or CloudWatch
* DNS records for your domain changing (catches both attacks and forgotten Terraform runs)

## Local machine
* New file appearing in `~/Downloads` or `~/Desktop`
* A screenshot was just taken (notice + auto-name + auto-file)
* USB device plugged in (could trigger a backup, or just log it)
* Battery health crossing a threshold
* Time Machine / backup hasn't run in N hours
* `launchd` job exit codes
* A specific port becoming open or closed

## Money & commerce
* New transaction over $X on a card (via Plaid or bank export)
* Subscription about to auto-renew this week
* A Steam / Epic wishlist item going on sale
* CamelCamelCamel-style Amazon price drop
* Out-of-stock product becoming in-stock
* Domain becoming available (drop-catching)
* eBay saved search has a new listing under $Y

## Civic, government, neighborhood
* Building permits filed for an address near you
* 311 reports in a radius around your home
* New planning agenda items mentioning your block
* Bill text in your state legislature mentioning a keyword
* Court docket new filings for a case number

## Science, nature, weather
* Aurora Kp index crossing observable threshold + cloud cover at your site clear
* USGS earthquake feed above magnitude N within a region
* AirNow / PurpleAir AQI crossing "open the windows" or "close the windows"
* Pollen count for the species you're allergic to
* Tide approaching slack water at a specific dive site
* Wildfire perimeter from NIFC growing toward a polygon you care about
* Wastewater virus levels in your county
* ISS overhead pass tonight + clear sky

## Personal / quantified self
* Smart-home leak sensor, door, motion (Home Assistant exposes everything as JSON)
* Trail cam / bird feeder cam — image diff plus a species classifier
* Package out for delivery, or "exception" status (the actual interesting one)
* Fitness ring sync produced a new sleep score
* USPS Informed Delivery — what's in the mailbox today
* A friend's Strava upload — useful for "did Dad finish the ride safely"
* Your own writing: word count of a WIP doc going *down* unexpectedly

## Long-running jobs & ML
* Training run loss curve plateauing (slope over last N steps near zero)
* Eval suite finished and the metric crossed a goal
* HuggingFace leaderboard position changed
* A Kaggle competition deadline crossed a threshold
* Data freshness SLA on a dbt model violated

## Cultural / fun
* Letterboxd / Goodreads / Spotify activity from a specific friend
* New episode of a podcast where you only care about *one* show in *one* feed
* A speedrun.com world record in a single category
* A chess.com / lichess game finished by a specific player
* A subreddit's moderator log — reveals what's being silently removed
* Wordle/NYT puzzle of the day published (auto-fetch + queue)

## The web as a database
* Hash of any URL's body changing (the universal poller)
* A single CSS selector's text content changing on a page
* WHOIS, NS, MX records on a domain
* Wayback Machine getting a new snapshot of a URL (someone else cared enough to archive)
* Certificate Transparency logs for new certs on `*.yourcompany.com` (shadow IT detector)
* BGP route changes for an ASN you depend on

## Inversions — notice the *absence*
This is the underused half of polling. Most people poll for "X happened"; the dead-man
switch polls for "X *didn't* happen by now."

* A heartbeat from another agent or service stopped arriving
* A daily cron didn't produce its expected output file by 9am
* A friend who posts daily hasn't posted in three days
* A status page hasn't updated past its declared SLA
* An RSS feed that posts weekly went silent for a month
* The build hasn't been green in 6 hours (longer than the usual flake window)
* A file you expected to grow is the same size

## Meta — pollers watching the agent's own world
* Other agents' event logs — chain reactions across agents
* Your own context size approaching a limit
* A long-running task's status flipping to done
* Daily LLM spend crossing a budget
* A specific skill's success rate dropping over the last N invocations
* `events.jsonl` rates of specific algedonic events crossing a threshold
* `saga_<tool>_ctx_resolution` resolution_path histogram skewing toward `missing`

---

A useful exercise when you're stuck: pick a noun (a person, a file, a domain, a sensor, a
queue, a webpage, an account) and ask "what's the smallest change to this thing I'd
actually want to be told about?" That answer is usually a poller.

## Composing with other skills

* **`pollers`** — the mechanics. This file is the *menu*; pollers is the *how*.
* **`fallback-chains`** — push (webhook) → pull (poll API) → poll (HTML) is itself a
  chain shape you'll often want.
* **`circuit-breaker`** — a poller that fires too often is signaling either a real
  storm worth investigating or a poorly-tuned threshold worth fixing.
* **`async-tasks`** — when you only need to wait for *one* specific occurrence and
  preserve the parent conversation, use an async-block instead of a poller.
