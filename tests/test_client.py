"""Live end-to-end test of hpc_gui.client against the real HPC cluster.

Run with: python -m tests.test_client
Requires env vars HPC_USER, HPC_PASS and HPC_PROJECT (HPC_EMAIL optional).
"""
from __future__ import annotations

import os
import sys

# Allow running as a script from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hpc_gui.client import HpcClient
from hpc_gui import parsers


def banner(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def main() -> int:
    host = "hpc.iitd.ac.in"
    user = os.environ.get("HPC_USER", "")
    password = os.environ.get("HPC_PASS", "")
    project = os.environ.get("HPC_PROJECT", "")

    c = HpcClient()
    banner("connect")
    c.connect(host, user, password)
    print(f"connected={c.connected}, home={c.home}")
    assert c.connected, "SSH session should be active"
    assert c.home.endswith(user)

    banner("amgr login + projects + balance")
    res = c.amgr_login(password)
    print(f"amgr_login ok={res.ok}, amgr_logged_in={c.amgr_logged_in}")
    assert c.amgr_logged_in, res.output
    projects = c.list_projects()
    print(f"projects={projects}")
    assert project in projects
    balance = c.check_balance(project)
    print(f"balance(₹)={balance}")
    assert balance is not None

    banner("qstat -u (live)")
    jobs, raw = c.list_my_jobs()
    print(raw)
    print(f"parsed {len(jobs)} jobs:")
    for j in jobs:
        print(f"  {j.job_id:28s} {j.queue:10s} {j.name:15s} state={j.state} elapsed={j.elapsed}")

    banner("qstat -x -u (history)")
    jobs_x, _ = c.list_my_jobs(include_history=True)
    print(f"parsed {len(jobs_x)} jobs (including F)")
    for j in jobs_x[:10]:
        print(f"  {j.job_id:28s} state={j.state} {j.name}")

    banner("qstat -Q")
    qs, _ = c.list_queues()
    print(f"parsed {len(qs)} queues:")
    for q in qs:
        print(f"  {q.name:12s} run={q.running:4s} q={q.queued:4s} held={q.held:4s} ena={q.enabled} type={q.qtype}")
    assert any(q.name == "standard" for q in qs)
    assert any(q.name == "high" for q in qs)

    if jobs:
        banner(f"qstat -f {jobs[0].job_id}")
        full = c.job_full_parsed(jobs[0].job_id)
        for k in ["Job Id", "job_state", "queue", "Resource_List.select",
                  "Resource_List.walltime", "Output_Path"]:
            print(f"  {k} = {full.get(k, '<missing>')}")

    banner("listdir ~/scratch")
    entries = c.listdir("~/scratch")
    for n, s, d, m in entries[:20]:
        print(f"  {'D' if d else 'F'} {n}  size={s}")

    banner("read_text ~/scratch/hpc_gui_test/logs/test.out (tail)")
    try:
        txt = c.read_text("~/scratch/hpc_gui_test/logs/test.out", max_bytes=4000, tail=True)
        print(txt[-500:])
    except Exception as e:
        print(f"(skip) {e}")

    banner("parsers: unit smoke")
    sample_qstat = """
pbshpc: 
                                                            Req'd  Req'd   Elap
Job ID          Username Queue    Jobname    SessID NDS TSK Memory Time  S Time
--------------- -------- -------- ---------- ------ --- --- ------ ----- - -----
883831.pbshpc   mt12307* standard clip_test   20774   1   8    --  01:00 R 00:04
""".strip()
    got = parsers.parse_qstat_u(sample_qstat)
    print(f"parsed sample: {got}")
    assert len(got) == 1 and got[0].state == "R" and got[0].queue == "standard"

    b = parsers.parse_balance("{'Cost': 999.2}")
    assert b == 999.2

    c.close()
    print("\nALL TESTS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
