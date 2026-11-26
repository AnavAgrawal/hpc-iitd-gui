"""Comprehensive end-to-end test of the HPC-IITD GUI backend.

Runs read-only checks + a real submit/cancel lifecycle on the live cluster.
Any failure here will surface as an uncaught exception with a non-zero exit.

Usage: python -m tests.test_e2e
Requires env vars HPC_USER, HPC_PASS and HPC_PROJECT (HPC_EMAIL optional).
"""
from __future__ import annotations

import os
import posixpath
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hpc_gui.client import HpcClient, shell_quote
from hpc_gui.parsers import parse_qstat_f, parse_qstat_u, parse_qstat_Q, parse_balance
from hpc_gui.ui.submit_tab import JobForm, estimate_cost


HOST = "hpc.iitd.ac.in"
USER = os.environ.get("HPC_USER", "")
PASS = os.environ.get("HPC_PASS", "")
PROJECT = os.environ.get("HPC_PROJECT", "")


def banner(s: str) -> None:
    print("\n" + "#" * 70 + f"\n# {s}\n" + "#" * 70)


def assert_eq(a, b, msg: str = "") -> None:
    assert a == b, f"{msg}: {a!r} != {b!r}"


def test_parsers_offline() -> None:
    banner("unit: parsers (offline)")
    # Real row captured from `qstat -u ab1234567` on the live cluster.
    q = parse_qstat_u("""
pbshpc: 
                                                            Req'd  Req'd   Elap
Job ID          Username Queue    Jobname    SessID NDS TSK Memory Time  S Time
--------------- -------- -------- ---------- ------ --- --- ------ ----- - -----
883831.pbshpc   mt12307* standard clip_test   20774   1   8    --  01:00 R 00:08
""".strip())
    assert_eq(len(q), 1, "one job")
    assert_eq(q[0].queue, "standard")
    assert_eq(q[0].state, "R")
    assert_eq(q[0].job_id, "883831.pbshpc")

    qs = parse_qstat_Q("""
Queue              Max   Tot Ena Str   Que   Run   Hld   Wat   Trn   Ext Type
---------------- ----- ----- --- --- ----- ----- ----- ----- ----- ----- ----
standard             0   358 yes yes   134   220     3     0     0     1 Exe*
high                 0    53 yes yes    16    36     1     0     0     0 Exe*
""".strip())
    assert_eq(len(qs), 2)
    assert_eq(qs[0].name, "standard")
    assert_eq(qs[1].running, "36")

    full = parse_qstat_f("""
Job Id: 883831.pbshpc
    Job_Name = clip_test
    job_state = R
    Resource_List.select = 1:ncpus=8:centos=icelake
    Output_Path = login06.hpc.iitd.ac.in:/home/user/scratch/logs/test.out
""".strip())
    assert_eq(full["Job Id"], "883831.pbshpc")
    assert_eq(full["job_state"], "R")
    assert "/scratch/logs/test.out" in full["Output_Path"]

    assert_eq(parse_balance("{'Cost': 42.25}"), 42.25)
    print("  parsers OK")


def test_cost_estimate() -> None:
    banner("unit: submit cost estimate (offline)")
    # 1 GPU + 16 CPUs for 1h on standard = 16*0.10 + 1*1.00 = 2.60
    form = JobForm(name="t", project=PROJECT, queue="standard",
                   nodes=1, ncpus=16, ngpus=1, gpu_type="icelake", memory="",
                   walltime="01:00:00", email="", mail_events="bea",
                   workdir="$PBS_O_WORKDIR", out_path="logs/t.out",
                   err_path="logs/t.err", script_body="echo hi")
    cost = estimate_cost(form)
    assert abs(cost - 2.60) < 1e-6, cost
    # High queue: 16*0.50 + 1*5.00 = 13.00
    form.queue = "high"
    assert abs(estimate_cost(form) - 13.00) < 1e-6
    # Render check
    text = form.render()
    assert "#PBS -q high" in text
    assert "select=1:ncpus=16:ngpus=1:centos=icelake" in text
    assert "walltime=01:00:00" in text
    print("  cost + render OK")


def test_live_readonly(c: HpcClient) -> None:
    banner("live: readonly commands")
    # login + projects + balance
    res = c.amgr_login(PASS)
    assert res.ok, res.output
    projects = c.list_projects()
    assert PROJECT in projects
    bal = c.check_balance(PROJECT)
    assert bal is not None and bal > 0
    print(f"  balance = ₹{bal}")

    # queues
    queues, _ = c.list_queues()
    names = [q.name for q in queues]
    for must_have in ("standard", "high"):
        assert must_have in names
    print(f"  queues  = {names}")

    # my jobs
    jobs, _ = c.list_my_jobs()
    print(f"  current jobs ({len(jobs)}): {[j.job_id + ':' + j.state for j in jobs]}")


def test_sftp(c: HpcClient) -> None:
    banner("live: SFTP mkdir / upload / download / remove")
    # Work inside a scratch subdir we'll clean up
    remote_dir = f"scratch/hpc_gui_test_{int(time.time())}"
    c.mkdirs(remote_dir)
    assert c.is_dir(remote_dir)
    # Write a file and upload
    with tempfile.TemporaryDirectory() as td:
        lfile = os.path.join(td, "hello.txt")
        with open(lfile, "w") as f:
            f.write("hello from hpc_gui\n")
        rfile = remote_dir + "/hello.txt"
        c.put_file(lfile, rfile)
        entries = c.listdir(remote_dir)
        names = [n for n, *_ in entries]
        assert "hello.txt" in names
        text = c.read_text(rfile)
        assert "hello from hpc_gui" in text

        # Round-trip download
        downloaded = os.path.join(td, "round.txt")
        c.get_file(rfile, downloaded)
        assert open(downloaded).read() == "hello from hpc_gui\n"

        # Recursive upload of a small tree
        tree_root = os.path.join(td, "tree")
        os.makedirs(os.path.join(tree_root, "sub"))
        open(os.path.join(tree_root, "a.txt"), "w").write("A\n")
        open(os.path.join(tree_root, "sub", "b.txt"), "w").write("B\n")
        uploaded, skipped = c.upload_tree(tree_root, remote_dir + "/tree")
        assert uploaded >= 2 and skipped == 0, (uploaded, skipped)
        # Re-upload should skip same-size files
        uploaded2, skipped2 = c.upload_tree(tree_root, remote_dir + "/tree")
        assert skipped2 >= 2, (uploaded2, skipped2)

        # Recursive download
        dl_root = os.path.join(td, "dl")
        d, s = c.download_tree(remote_dir + "/tree", dl_root)
        assert d >= 2, (d, s)
        assert open(os.path.join(dl_root, "a.txt")).read() == "A\n"
        assert open(os.path.join(dl_root, "sub", "b.txt")).read() == "B\n"

    # Clean up
    c.remove(remote_dir)
    # Should be gone
    assert c.stat(remote_dir) is None
    print("  sftp OK")


def test_submit_lifecycle(c: HpcClient) -> None:
    banner("live: qsub → qstat → qdel lifecycle")
    remote_dir = f"scratch/hpc_gui_test_{int(time.time())}"
    c.mkdirs(remote_dir)
    c.mkdirs(remote_dir + "/logs")

    # Very small CPU job (no GPU) so it queues and runs fast + cheaply.
    form = JobForm(
        name="hpc_gui_smoke",
        project=PROJECT,
        queue="standard",
        nodes=1, ncpus=1, ngpus=0, gpu_type="", memory="",
        walltime="00:02:00",
        email="", mail_events="",
        workdir="$PBS_O_WORKDIR",
        out_path="logs/smoke.out",
        err_path="logs/smoke.err",
        script_body="echo 'hello from job' $(hostname); sleep 5; echo done",
    )
    script = form.render()
    script_path = remote_dir + "/smoke.pbs"
    with tempfile.NamedTemporaryFile("w", suffix=".pbs", delete=False, newline="\n") as f:
        f.write(script); local = f.name
    try:
        c.put_file(local, script_path)
    finally:
        os.unlink(local)

    jid, res = c.qsub(script_path, workdir=remote_dir)
    assert res.ok and jid, f"qsub failed: {res.output}"
    print(f"  submitted job_id={jid}")

    # Poll for it to appear in qstat
    deadline = time.time() + 60
    seen = False
    while time.time() < deadline:
        jobs, _ = c.list_my_jobs()
        for j in jobs:
            if j.job_id.split(".")[0] == jid.split(".")[0]:
                print(f"  qstat: {j.job_id} state={j.state}")
                seen = True
                break
        if seen:
            break
        time.sleep(2)
    assert seen, "Submitted job never appeared in qstat -u"

    # qstat -f
    full = c.job_full_parsed(jid)
    assert full.get("queue") == "standard"
    assert full.get("Job_Name") == "hpc_gui_smoke"
    print(f"  qstat -f Resource_List.select = {full.get('Resource_List.select')}")

    # qdel (cancel)
    qd = c.qdel(jid, force=True)
    print(f"  qdel exit={qd.exit_code}; out={qd.output.strip()!r}")
    assert qd.ok or "Unknown Job Id" in qd.output, qd.output

    # Clean up
    c.remove(remote_dir)
    print("  submit lifecycle OK")


def main() -> int:
    test_parsers_offline()
    test_cost_estimate()

    c = HpcClient()
    c.connect(HOST, USER, PASS)
    try:
        test_live_readonly(c)
        test_sftp(c)
        test_submit_lifecycle(c)
    finally:
        c.close()

    print("\n✅ E2E: ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
