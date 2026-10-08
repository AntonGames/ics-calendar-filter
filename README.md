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

## Things worth knowing

- **Refresh lag is Google's, not ours.** Google re-polls an external ICS only
  every ~8–24 hours and this is not configurable. A faster cron will not help.
- **Scheduled workflows are disabled after 60 days of repo inactivity.**
  Re-enable from the Actions tab, or push a commit occasionally.
- **The gist is secret, not private.** Anyone with the URL can read it — the
  same security model as the Outlook ICS link itself. Do not publish it, and
  never use GitHub Pages for this, as that would expose meeting titles publicly.
- **Fix it at the source too.** In Outlook: *Settings → Calendar → Events and
  invitations → Save declined events* → off. That reduces what reaches the feed
  in the first place, though it does not cover Following/Stebima.
