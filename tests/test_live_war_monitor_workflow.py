from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "live-war-monitor.yml"
WEEKHISTORY_WORKFLOW = ROOT / ".github" / "workflows" / "snapshot-clan-history.yml"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_live_workflow_has_five_minute_schedule_and_manual_dispatch():
    text = workflow_text()

    assert re.search(
        r'(?m)^\s*-\s*cron:\s*["\']\*/5 \* \* \* \*["\']\s*$',
        text,
    )
    assert re.search(r"(?m)^\s*workflow_dispatch:\s*$", text)
    assert "schedules are best-effort" in text
    assert "Vercel Hobby is not suitable" in text


def test_live_workflow_serializes_scheduled_and_manual_runs():
    text = workflow_text()

    assert re.search(r"(?m)^concurrency:\s*$", text)
    assert re.search(r"(?m)^\s+group:\s+live-war-monitor\s*$", text)
    assert re.search(r"(?m)^\s+cancel-in-progress:\s+false\s*$", text)
    assert "matrix:" not in text


def test_live_workflow_posts_the_t08_secret_only_as_the_monitor_header():
    text = workflow_text()

    assert "--request POST" in text
    assert "--url \"${WAR_MONITOR_URL}\"" in text
    assert "WAR_MONITOR_SECRET: ${{ secrets.WAR_MONITOR_SECRET }}" in text
    assert '--header "X-War-Monitor-Secret: ${WAR_MONITOR_SECRET}"' in text
    assert text.count("secrets.WAR_MONITOR_SECRET") == 1
    assert "--data-raw '{}'" in text


def test_live_workflow_has_bounded_timeout_retry_and_explicit_http_failure():
    text = workflow_text()

    assert re.search(r"(?m)^\s+timeout-minutes:\s+5\s*$", text)
    assert "--connect-timeout 10" in text
    assert "--max-time 60" in text
    assert "--retry 2" in text
    assert "--retry-connrefused" in text
    assert "--retry-delay 5" in text
    assert "--retry-max-time 120" in text
    status_block = text[text.index('case "${http_status}" in') :]
    assert "2[0-9][0-9])" in status_block
    assert "::error::War monitor returned HTTP" in status_block
    assert "exit 1" in status_block
    assert '--output "${response_file}"' in text
    assert "jq -c" in text


def test_existing_weekhistory_scheduler_remains_present_and_monday_scheduled():
    text = WEEKHISTORY_WORKFLOW.read_text(encoding="utf-8")

    assert WEEKHISTORY_WORKFLOW.exists()
    assert "name: Snapshot clan analytics history" in text
    assert 'cron: "0 13 * * 1"' in text
    assert "workflow_dispatch:" in text
    assert "SUPABASE_INGEST_TOKEN" in text
    assert "snapshot_history" in text


def test_monitor_secret_and_endpoint_documentation_are_non_secret():
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "WAR_MONITOR_SECRET=" in env_text
    assert "WAR_MONITOR_SECRET" in readme_text
    assert "WAR_MONITOR_URL" in readme_text
    assert "GitHub Actions" in readme_text
    assert "Vercel Hobby" in readme_text
    assert "WAR_MONITOR_SECRET=" not in readme_text
