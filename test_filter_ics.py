import unittest

from filter_ics import build_parser, filter_calendar, fold, split_prop


def cfg(**kw):
    return build_parser().parse_args([] if not kw else [])


def make(*events, extra_header=""):
    head = "BEGIN:VCALENDAR\r\nMETHOD:PUBLISH\r\nVERSION:2.0\r\n" + extra_header
    body = "".join("BEGIN:VEVENT\r\n" + e.strip("\r\n") + "\r\nEND:VEVENT\r\n" for e in events)
    return head + body + "END:VCALENDAR\r\n"


def uids(text):
    return [l.split(":", 1)[1] for l in text.split("\r\n") if l.startswith("UID:")]


def summaries(text):
    return [l.split(":", 1)[1] for l in text.split("\r\n") if l.startswith("SUMMARY:")]


class TestHelpers(unittest.TestCase):
    def test_split_prop_handles_quoted_colon(self):
        name, params, value = split_prop('ATTENDEE;CN="A:B":mailto:x@y.com')
        self.assertEqual(name, "ATTENDEE")
        self.assertEqual(params, 'CN="A:B"')
        self.assertEqual(value, "mailto:x@y.com")

    def test_fold_respects_75_octets(self):
        folded = fold("DESCRIPTION:" + "x" * 200)
        for line in folded.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75)

    def test_fold_never_splits_multibyte_char(self):
        folded = fold("SUMMARY:" + "ą" * 120)
        for line in folded.split("\r\n"):
            line.encode("utf-8").decode("utf-8")  # must not raise

    def test_short_line_untouched(self):
        self.assertEqual(fold("UID:abc"), "UID:abc")


class TestFiltering(unittest.TestCase):
    def test_drops_cancelled_subject_prefix(self):
        ics = make(
            "UID:1\r\nSUMMARY:Canceled: Standup\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
            "UID:2\r\nSUMMARY:Standup\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
        )
        out, stats = filter_calendar(ics, cfg())
        self.assertEqual(uids(out), ["2"])
        self.assertEqual(stats["cancelled"], 1)

    def test_drops_lithuanian_cancelled_prefix(self):
        ics = make(
            "UID:1\r\nSUMMARY:Atšaukta: Susitikimas\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        out, _ = filter_calendar(ics, cfg())
        self.assertEqual(uids(out), [])

    def test_drops_status_cancelled(self):
        ics = make(
            "UID:1\r\nSUMMARY:Standup\r\nSTATUS:CANCELLED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        out, _ = filter_calendar(ics, cfg())
        self.assertEqual(uids(out), [])

    def test_drops_stebima_prefix(self):
        ics = make(
            "UID:1\r\nSUMMARY:Stebima: Audit Alliance\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        out, stats = filter_calendar(ics, cfg())
        self.assertEqual(uids(out), [])
        self.assertEqual(stats["following"], 1)

    def test_drops_declined_free_without_prefix(self):
        """A declined meeting keeps INTENDEDSTATUS:BUSY but drops to FREE."""
        ics = make(
            "UID:1\r\nSUMMARY:1ES Meeting\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        out, stats = filter_calendar(ics, cfg())
        self.assertEqual(uids(out), [])
        self.assertEqual(stats["declined-or-following"], 1)

    def test_keeps_accepted(self):
        ics = make(
            "UID:1\r\nSUMMARY:Weekly DevTeam meeting\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        out, _ = filter_calendar(ics, cfg())
        self.assertEqual(summaries(out), ["Weekly DevTeam meeting"])

    def test_keeps_oof_vacation_even_though_not_busy(self):
        ics = make(
            "UID:1\r\nSUMMARY:Vacation\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:OOF\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY\r\n"
            "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE"
        )
        out, _ = filter_calendar(ics, cfg())
        self.assertEqual(summaries(out), ["Vacation"])

    def test_keeps_tentative_by_default(self):
        ics = make(
            "UID:1\r\nSUMMARY:Office breakfast\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        out, _ = filter_calendar(ics, cfg())
        self.assertEqual(uids(out), ["1"])

    def test_drop_tentative_when_requested(self):
        ics = make(
            "UID:1\r\nSUMMARY:Office breakfast\r\nSTATUS:CONFIRMED\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        opts = build_parser().parse_args(["--drop-tentative"])
        out, _ = filter_calendar(ics, opts)
        self.assertEqual(uids(out), [])


class TestRecurrence(unittest.TestCase):
    MASTER = (
        "UID:R1\r\nSUMMARY:Weekly Iteration\r\nSTATUS:CONFIRMED\r\n"
        "DTSTART;TZID=FLE Standard Time:20251001T160000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=WE\r\n"
        "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
    )

    def test_cancelled_occurrence_becomes_exdate(self):
        ics = make(
            self.MASTER,
            "UID:R1\r\nSUMMARY:Canceled: Weekly Iteration\r\nSTATUS:CONFIRMED\r\n"
            "RECURRENCE-ID;TZID=FLE Standard Time:20251015T160000\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
        )
        out, stats = filter_calendar(ics, cfg())
        self.assertIn("EXDATE;TZID=FLE Standard Time:20251015T160000", out)
        self.assertEqual(stats["exdates_added"], 1)
        self.assertEqual(summaries(out), ["Weekly Iteration"])

    def test_exdate_is_emitted_after_rrule(self):
        ics = make(
            self.MASTER,
            "UID:R1\r\nSUMMARY:Canceled: Weekly Iteration\r\n"
            "RECURRENCE-ID;TZID=FLE Standard Time:20251015T160000\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
        )
        out, _ = filter_calendar(ics, cfg())
        lines = out.split("\r\n")
        self.assertLess(
            lines.index("RRULE:FREQ=WEEKLY;BYDAY=WE"),
            lines.index("EXDATE;TZID=FLE Standard Time:20251015T160000"),
        )

    def test_multiple_exdates_same_tzid_are_merged(self):
        ics = make(
            self.MASTER,
            "UID:R1\r\nSUMMARY:Canceled: A\r\n"
            "RECURRENCE-ID;TZID=FLE Standard Time:20251015T160000\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
            "UID:R1\r\nSUMMARY:Stebima: A\r\n"
            "RECURRENCE-ID;TZID=FLE Standard Time:20251022T160000\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
        )
        out, stats = filter_calendar(ics, cfg())
        self.assertIn(
            "EXDATE;TZID=FLE Standard Time:20251015T160000,20251022T160000", out
        )
        self.assertEqual(stats["exdates_added"], 2)

    def test_range_param_stripped_from_exdate(self):
        ics = make(
            self.MASTER,
            "UID:R1\r\nSUMMARY:Canceled: A\r\n"
            "RECURRENCE-ID;TZID=FLE Standard Time;RANGE=THISANDFUTURE:20251015T160000\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
        )
        out, _ = filter_calendar(ics, cfg())
        self.assertIn("EXDATE;TZID=FLE Standard Time:20251015T160000", out)
        self.assertNotIn("RANGE=THISANDFUTURE", out)

    def test_dropping_master_drops_all_overrides(self):
        ics = make(
            "UID:R2\r\nSUMMARY:Stebima: Audit Alliance\r\n"
            "RRULE:FREQ=WEEKLY\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
            "UID:R2\r\nSUMMARY:Stebima: Audit Alliance\r\n"
            "RECURRENCE-ID;TZID=FLE Standard Time:20251015T160000\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
        )
        out, stats = filter_calendar(ics, cfg())
        self.assertEqual(uids(out), [])
        self.assertEqual(stats["exdates_added"], 0)
        self.assertEqual(stats["following"], 2)

    def test_kept_occurrence_adds_no_exdate(self):
        ics = make(
            self.MASTER,
            "UID:R1\r\nSUMMARY:Weekly Iteration (moved)\r\n"
            "RECURRENCE-ID;TZID=FLE Standard Time:20251015T160000\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
        )
        out, stats = filter_calendar(ics, cfg())
        self.assertNotIn("EXDATE", out)
        self.assertEqual(stats["kept"], 2)


class TestStructure(unittest.TestCase):
    def test_vtimezone_is_preserved(self):
        tz = (
            "BEGIN:VTIMEZONE\r\nTZID:FLE Standard Time\r\nBEGIN:STANDARD\r\n"
            "DTSTART:16010101T040000\r\nTZOFFSETFROM:+0300\r\nTZOFFSETTO:+0200\r\n"
            "END:STANDARD\r\nEND:VTIMEZONE\r\n"
        )
        ics = make(
            "UID:1\r\nSUMMARY:Standup\r\nX-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\n"
            "X-MICROSOFT-CDO-INTENDEDSTATUS:BUSY",
            extra_header=tz,
        )
        out, _ = filter_calendar(ics, cfg())
        self.assertIn("BEGIN:VTIMEZONE", out)
        self.assertIn("TZID:FLE Standard Time", out)
        self.assertIn("END:VTIMEZONE", out)

    def test_output_is_well_formed(self):
        ics = make(
            "UID:1\r\nSUMMARY:Standup\r\nX-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\n"
            "X-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        out, _ = filter_calendar(ics, cfg())
        self.assertTrue(out.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(out.rstrip().endswith("END:VCALENDAR"))
        self.assertEqual(out.count("BEGIN:VEVENT"), out.count("END:VEVENT"))

    def test_folded_input_is_unfolded_and_preserved(self):
        long_desc = "DESCRIPTION:" + "y" * 100
        ics = make(
            "UID:1\r\nSUMMARY:Standup\r\n" + fold(long_desc) + "\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:BUSY\r\nX-MICROSOFT-CDO-INTENDEDSTATUS:BUSY"
        )
        out, _ = filter_calendar(ics, cfg())
        rebuilt = out.replace("\r\n ", "")
        self.assertIn(long_desc, rebuilt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
