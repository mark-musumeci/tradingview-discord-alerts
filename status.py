"""Let any part of the bot record a problem for the status report at the end of a run."""
import json
from pathlib import Path

ISSUES_FILE = Path(__file__).with_name(".run_issues")


def note(key, message):
    """Record a problem. The key identifies it across runs, so it isn't reported twice."""
    try:
        with ISSUES_FILE.open("a") as issues:
            issues.write(json.dumps({"key": key, "message": message}) + "\n")
    except OSError as error:
        print(f"Couldn't record a status note: {error}")  # Never stop a run over this.
