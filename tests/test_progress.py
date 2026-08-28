

def test_advice_is_not_counted_among_the_things_to_fix():
    """A column-width note is never repaired, so it is not work outstanding.

    Counting it with the errors promises a fix that the pipeline deliberately
    does not attempt, and the line gives the reader no way to tell which kind
    of issue it saw.
    """
    from app.progress import describe
    from app.schema import ValidationIssue

    def issue(severity: str) -> ValidationIssue:
        return ValidationIssue(
            field="Key_Success", message="…", severity=severity, repairable=True
        )

    both = describe("validate", {"issues": [issue("error"), issue("advice")]}, {})
    assert both["detail"] == "1 to fix, 1 note"

    only_advice = describe("validate", {"issues": [issue("advice")]}, {})
    assert only_advice["detail"] == "1 note", "nothing here is outstanding"

    assert describe("validate", {"issues": []}, {})["detail"] == "no problems"
