# ics-calendar-filter

Strips cancelled, **Stebima** (Following) and declined meetings out of an
Outlook / Microsoft 365 published calendar feed before Google Calendar sees it.

```
Outlook .ics  →  GitHub Actions (hourly)  →  secret Gist  →  Google Calendar
                      filter_ics.py
```

## Why a proxy is needed

Google Calendar treats a subscribed ICS feed as **read-only** — you cannot
delete individual events from it, and they come back on every refresh. The only
reliable fix is to clean the feed *before* Google reads it.

## How events are classified

Outlook publishes with `METHOD:PUBLISH`, which **strips `ATTENDEE` and
`ORGANIZER`**, so there is no `PARTSTAT` to read. Response status has to be
inferred instead:

| Signal | Meaning | Default |
|---|---|---|
| `SUMMARY` starts with `Canceled:` / `Atšaukta:` | Cancelled (Outlook stamps the subject; it does *not* set `STATUS:CANCELLED`) | **dropped** |
| `SUMMARY` starts with `Stebima:` / `Following:` | Following | **dropped** |
| `BUSYSTATUS:FREE` + `INTENDEDSTATUS:BUSY` | Declined or Following — the organiser wanted you busy, your copy went free | **dropped** |
| `BUSYSTATUS:BUSY` | Accepted | kept |
| `BUSYSTATUS:TENTATIVE` | Not yet answered | kept (`--drop-tentative` to remove) |
| `BUSYSTATUS:OOF` | Vacation, sick leave, public holidays | always kept |

### Recurring meetings

Dropping a single cancelled occurrence is not enough: the master `RRULE` would
simply regenerate that slot. For every dropped occurrence whose series
survives, an `EXDATE` is injected into the master. If the master itself is
dropped, the whole series goes with it.

## Setup

### 1. Create the secret Gist

```bash
echo "placeholder" > calendar.ics
gh gist create calendar.ics --desc "Filtered work calendar"   # secret by default
```

Note the gist ID from the returned URL.

### 2. Create a token

A fine-grained PAT is not required; a classic token with only the **`gist`**
scope is enough: <https://github.com/settings/tokens>

Choose **"No expiration"**. A token that expires silently stops the sync — the
calendar freezes on its last good copy and every scheduled run fails until the
token is replaced. A `gist`-scoped token can only read and write your gists; it
cannot touch repositories or any other data.

To replace an expired token later:

```bash
gh secret set GIST_TOKEN --repo <owner>/ics-calendar-filter
gh workflow run "Sync filtered calendar" --repo <owner>/ics-calendar-filter
```

### 3. Push this repo and add secrets

```bash
gh repo create ics-calendar-filter --private --source=. --push
gh secret set ICS_URL      # your Outlook .ics URL
gh secret set GIST_ID      # the gist ID from step 1
gh secret set GIST_TOKEN   # the gist-scoped token from step 2
gh workflow run "Sync filtered calendar"
```

### 4. Subscribe in Google Calendar

Use the gist **raw** URL (it always serves the latest revision):

```
https://gist.githubusercontent.com/<user>/<GIST_ID>/raw/calendar.ics
```

Google Calendar → *Other calendars* → **+** → *From URL*.

Then **remove the old direct Outlook subscription**, or you will see every
event twice.

## Local use

```powershell
python filter_ics.py --url "<outlook ics url>" --output calendar.ics
python filter_ics.py --input sample.ics --output clean.ics --drop-tentative
python -m unittest -v
```

Options: `--keep-cancelled`, `--keep-following`, `--keep-declined`,
`--drop-tentative`, `--drop-free`, `--drop-allday`, `--calname`, `--min-events`.

`--min-events N` refuses to write the file if fewer than N events survive, so a
transient Outlook error page can never blank your calendar.

## Runs with every PC switched off

The whole pipeline is server-side: GitHub Actions fetches the Outlook feed,
filters it and updates the Gist on GitHub's own runners, and Google pulls from
the Gist. Nothing touches a local machine, so it keeps working while every PC
you own is off. A local machine is only needed to change the configuration.

The one catch is that **GitHub disables scheduled workflows after 60 days of
repository inactivity**. The `Keep the schedule alive` step prevents that by
pushing a small commit whenever the repo has been quiet for 45 days, so the
cron never gets switched off.

## Things worth knowing

- **Refresh lag is Google's, not ours.** Google re-polls an external ICS only
  every ~8–24 hours and this is not configurable. A faster cron will not help.
- **The gist is secret, not private.** Anyone with the URL can read it — the
  same security model as the Outlook ICS link itself. Do not publish it, and
  never use GitHub Pages for this, as that would expose meeting titles publicly.
- **Failures email you.** GitHub notifies the repo owner when a scheduled run
  fails, and the run refuses to publish an empty or leaking calendar rather
  than corrupting a working one.
- **An expired `GIST_TOKEN` is the most likely cause of a failing run.** The
  publish step reports that explicitly. Google keeps serving the last good
  calendar until the token is replaced, so nothing is lost — it just stops
  updating.
- **Scheduled runs are throttled.** GitHub does not honour an hourly cron
  exactly and may skip runs on a quiet repository; in practice this syncs every
  few hours. That is still far more often than Google re-polls, so it does not
  affect what you see.
- **Fix it at the source too.** In Outlook: *Settings → Calendar → Events and
  invitations → Save declined events* → off. That reduces what reaches the feed
  in the first place, though it does not cover Following/Stebima.
