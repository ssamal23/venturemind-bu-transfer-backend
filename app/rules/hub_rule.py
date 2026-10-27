from app.rules.types import Check


def hub_and_gpa_notes() -> list[Check]:
    """Always-true facts about transfer credit. Shown for information; they never block."""
    return [
        Check("hub", "Transfer courses earn no BU Hub units", True, note=True),
        Check("gpa", "Posts as grade T and doesn't count toward the BU GPA", True, note=True),
    ]
