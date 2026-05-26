"""Streamlit operator UI for the investor outreach pipeline.

The CLI scripts in scripts/ are the source of truth; this UI calls
them via subprocess so the lock + audit + backup story stays
identical to running the commands by hand. Read paths query the
SQLite DB directly via SQLAlchemy (read-only).

Auth is a single shared password from APP_PASSWORD env var. The
workspace path is pinned via INVESTOR_WORKSPACE so the UI always
operates on the configured workspace; the operator never picks the
DB at runtime.

Launch locally:
    APP_PASSWORD=dev INVESTOR_WORKSPACE=clients/test_workspace \\
        uv run --extra web streamlit run web/app.py

On Fly: see web/README.md for the deploy story.
"""
from __future__ import annotations

import os
import subprocess
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
from sqlalchemy import desc, select

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.approval.gate import can_approve_draft, split_blockers  # noqa: E402
from core.approval.persistence import (  # noqa: E402
    approved_for_send,
    pending_review,
)
from core.config_loader import load_workspace  # noqa: E402
from core.db import (  # noqa: E402
    draft_approvals,
    email_drafts,
    get_engine,
    partners,
    runs,
)

st.set_page_config(
    page_title="Investor Outreach Operator",
    layout="wide",
    page_icon=":envelope:",
)


# --- auth --------------------------------------------------------------

def _require_auth() -> bool:
    """Single-password gate. Reads APP_PASSWORD from env. Returns True
    when the user is authenticated; renders the login form + returns
    False otherwise so the caller can short-circuit."""
    expected = os.environ.get("APP_PASSWORD")
    if not expected:
        st.error(
            "APP_PASSWORD is not set on the server. Refusing to serve "
            "the UI without auth. Set the env var and restart."
        )
        return False
    if st.session_state.get("authed"):
        return True
    st.title("Investor Outreach Operator")
    pwd = st.text_input("Password", type="password")
    if st.button("Sign in"):
        if pwd == expected:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    return False


# --- workspace helpers -------------------------------------------------

def _ws_path() -> str:
    ws = os.environ.get("INVESTOR_WORKSPACE")
    if not ws:
        st.error(
            "INVESTOR_WORKSPACE env var is not set. The UI needs a "
            "pinned workspace path; set it on the server and restart."
        )
        st.stop()
    return ws


@st.cache_resource(show_spinner=False)
def _engine_for(ws_path: str):
    ws = load_workspace(ws_path)
    return get_engine(ws.db_url), ws


def _run_cli(*args: str, env_extra: dict | None = None,
             timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a script under uv. The shared lock + audit happens inside
    operator_command_run / stage_run; we only care about the exit
    code + stdout for UI feedback."""
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / args[0]), *args[1:]]
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=timeout,
        cwd=str(REPO_ROOT),
    )


# --- pages -------------------------------------------------------------

def _render_review_queue(ws_path: str) -> None:
    engine, ws = _engine_for(ws_path)
    pending = pending_review(engine)
    st.subheader(f"Pending review ({len(pending)})")
    if not pending:
        st.info(
            "Nothing in the review queue. Run Stage 7 (Generate emails) "
            "from the Pipeline page to produce drafts."
        )
        return
    # Pre-load partner email map for the inline set-email widget.
    with engine.begin() as conn:
        email_by_pid = {
            r.partner_id: (r.email or "")
            for r in conn.execute(
                select(partners.c.partner_id, partners.c.email),
            )
        }
    for draft in pending:
        with st.expander(
            f"draft_id={draft.draft_id}  partner={draft.partner_id}  "
            f"strategy={draft.email_strategy_used or '?'}  "
            f"status={draft.approval_status}",
        ):
            st.markdown(f"**Subject:** {draft.subject}")
            st.text_area(
                "Body", value=draft.body or "", height=200,
                key=f"body_{draft.draft_id}", disabled=True,
            )
            # Live gate read so the operator sees exactly what
            # approve_draft.py would see right now.
            gate = can_approve_draft(
                ws, engine, draft.draft_id,
                allow_example_domains=True,
            )
            if gate.blockers:
                hard, soft = split_blockers(gate.blockers)
                if hard:
                    st.error("HARD blockers (cannot be overridden):")
                    for b in hard:
                        st.write(f"- {b}")
                if soft:
                    st.warning("Soft blockers (operator may override):")
                    for b in soft:
                        st.write(f"- {b}")
            else:
                st.success("Gate: clean. Safe to approve.")

            cur_email = email_by_pid.get(draft.partner_id, "")
            new_email = st.text_input(
                "Partner email", value=cur_email,
                key=f"email_{draft.draft_id}",
                help=(
                    "Setting a new email here calls set_partner_email "
                    "with the audit/lock wrapper. Changing it AFTER "
                    "approval will auto-stale the approval."
                ),
            )
            cols = st.columns([1, 1, 1, 2])
            if cols[0].button("Save email", key=f"saveemail_{draft.draft_id}"):
                res = _run_cli(
                    "set_partner_email.py", "--workspace", ws_path,
                    "--partner-id", draft.partner_id,
                    "--email", new_email,
                )
                if res.returncode == 0:
                    st.success("Email saved.")
                    st.rerun()
                else:
                    st.error(f"set_partner_email failed:\n{res.stdout}\n{res.stderr}")
            notes = st.text_input(
                "Approval / rejection notes",
                key=f"notes_{draft.draft_id}",
                placeholder="why is this draft good to send? (recorded for audit)",
            )
            override = st.checkbox(
                "Override soft blockers",
                key=f"ovr_{draft.draft_id}",
                help=(
                    "Forwards --override-blockers to approve_draft. "
                    "Hard blockers can never be overridden."
                ),
            )
            if cols[1].button(
                "Approve", key=f"approve_{draft.draft_id}",
                type="primary", disabled=not notes.strip(),
            ):
                cli_args = [
                    "approve_draft.py", "--workspace", ws_path,
                    "--draft-id", str(draft.draft_id),
                    "--notes", notes,
                    "--allow-example-domains",
                ]
                if override:
                    cli_args.append("--override-blockers")
                res = _run_cli(*cli_args, env_extra={"USER": _actor()})
                if res.returncode == 0:
                    st.success(res.stdout.splitlines()[-2] if res.stdout else "Approved.")
                    st.rerun()
                else:
                    st.error(f"Approve refused:\n{res.stdout}\n{res.stderr}")
            if cols[2].button(
                "Reject", key=f"reject_{draft.draft_id}",
                disabled=not notes.strip(),
            ):
                res = _run_cli(
                    "reject_draft.py", "--workspace", ws_path,
                    "--draft-id", str(draft.draft_id), "--notes", notes,
                    env_extra={"USER": _actor()},
                )
                if res.returncode == 0:
                    st.success("Rejected.")
                    st.rerun()
                else:
                    st.error(f"Reject failed:\n{res.stdout}\n{res.stderr}")


def _render_approved(ws_path: str) -> None:
    engine, _ = _engine_for(ws_path)
    approved = approved_for_send(engine)
    st.subheader(f"Approved to send ({len(approved)})")
    if not approved:
        st.info(
            "Nothing approved. Use the Review tab to approve drafts."
        )
        return
    with engine.begin() as conn:
        email_by_pid = {
            r.partner_id: (r.email or "")
            for r in conn.execute(
                select(partners.c.partner_id, partners.c.email),
            )
        }
    rows = []
    for d in approved:
        rows.append({
            "draft_id": d.draft_id,
            "partner_id": d.partner_id,
            "to": email_by_pid.get(d.partner_id, ""),
            "subject": d.subject,
        })
    st.dataframe(rows, use_container_width=True)


def _render_check_ready(ws_path: str) -> None:
    st.subheader("Pre-send check (check_ready --for send)")
    if st.button("Run check_ready --for send"):
        res = _run_cli(
            "check_ready.py", "--workspace", ws_path,
            "--for", "send", "--allow-example-domains",
        )
        if "BLOCKED" in res.stdout:
            st.error("Some gates are BLOCKED. Resolve before sending.")
        elif res.returncode == 0:
            st.success("All gates green.")
        st.code(res.stdout or "(no output)")
        if res.stderr:
            st.text(res.stderr)


def _render_export(ws_path: str) -> None:
    st.subheader("Export send queue (CSV)")
    st.caption(
        "Builds clients/<workspace>/exports/send_queue.csv from the "
        "approved-to-send list. Downloads as an attachment when ready."
    )
    if st.button("Build send_queue.csv"):
        res = _run_cli(
            "export_send_queue.py", "--workspace", ws_path,
            "--allow-example-domains",
        )
        if res.returncode != 0:
            st.error(f"Export failed:\n{res.stdout}\n{res.stderr}")
            return
        # Discover the path from the script's stdout.
        out_path = None
        for line in res.stdout.splitlines():
            if "send_queue.csv" in line:
                # Lines look like: "[send_queue] N approved -> /path/to/.csv"
                for tok in line.split():
                    if tok.endswith("send_queue.csv"):
                        out_path = Path(tok)
                        break
        if out_path and out_path.exists():
            st.success(res.stdout.strip())
            st.download_button(
                "Download send_queue.csv",
                data=out_path.read_bytes(),
                file_name="send_queue.csv",
                mime="text/csv",
            )
        else:
            st.warning("Built, but couldn't locate the file path.")
            st.code(res.stdout)


def _render_runs(ws_path: str) -> None:
    engine, _ = _engine_for(ws_path)
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(
                runs.c.run_id, runs.c.stage, runs.c.started_at,
                runs.c.completed_at, runs.c.processed, runs.c.succeeded,
                runs.c.failed, runs.c.skipped, runs.c.error_summary,
            ).order_by(desc(runs.c.run_id)).limit(50)
        ))
    st.subheader(f"Recent runs ({len(rows)})")
    st.dataframe(
        [dict(r._mapping) for r in rows],
        use_container_width=True,
    )


def _actor() -> str:
    """Identity stamped on approval / reject events. For a single-user
    deployment this is fine; multi-user UI later swaps to the
    session's authenticated identity."""
    return os.environ.get("APP_OPERATOR", "web-operator")


# --- main --------------------------------------------------------------

def main() -> None:
    if not _require_auth():
        return
    ws_path = _ws_path()
    st.sidebar.markdown(f"**Workspace:** `{ws_path}`")
    st.sidebar.caption(f"Actor: `{_actor()}`")
    if st.sidebar.button("Sign out"):
        st.session_state.pop("authed", None)
        st.rerun()
    tabs = st.tabs(["Review", "Approved", "Check ready", "Export", "Runs"])
    with tabs[0]:
        _render_review_queue(ws_path)
    with tabs[1]:
        _render_approved(ws_path)
    with tabs[2]:
        _render_check_ready(ws_path)
    with tabs[3]:
        _render_export(ws_path)
    with tabs[4]:
        _render_runs(ws_path)


if __name__ == "__main__":
    main()
