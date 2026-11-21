"""CI workflow guards: least-privilege token, bounded job time."""

from pathlib import Path

import yaml

CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def test_ci_token_is_read_only_and_jobs_are_bounded():
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        assert job.get("timeout-minutes") == 20, name
