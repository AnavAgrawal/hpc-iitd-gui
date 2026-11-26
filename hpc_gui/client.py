"""HPC SSH/SFTP client: thin wrapper around paramiko with PBS/amgr helpers."""
from __future__ import annotations

import os
import posixpath
import stat as stat_mod
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Tuple

import paramiko

from .parsers import (
    Job,
    Queue,
    parse_balance,
    parse_projects,
    parse_qstat_f,
    parse_qstat_f_many,
    parse_qstat_Q,
    parse_qstat_u,
    parse_qsub_output,
    parse_select,
)


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        if self.stderr and not self.stdout:
            return self.stderr
        if self.stderr:
            return self.stdout + "\n" + self.stderr
        return self.stdout


class HpcError(Exception):
    pass


class NotConnected(HpcError):
    pass


class HpcClient:
    """Thread-safe wrapper around paramiko for the IITD HPC cluster.

    Multiple callers may run ``run`` concurrently; each command gets its own
    channel. SFTP access is serialised by an internal lock.
    """

    def __init__(self) -> None:
        self._ssh: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._sftp_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.host: str = ""
        self.username: str = ""
        self.home: str = ""
        self.amgr_logged_in: bool = False

    # ---------- connection ----------
    def connect(self, host: str, username: str, password: str, timeout: int = 30) -> None:
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(
            host,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
        # Keepalive so the session doesn't drop while sitting idle.
        try:
            cli.get_transport().set_keepalive(30)
        except Exception:
            pass
        self._ssh = cli
        self.host = host
        self.username = username
        # Capture home directory
        res = self.run("echo $HOME")
        self.home = res.stdout.strip() or f"/home/{username}"

    def close(self) -> None:
        with self._sftp_lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None
        self.amgr_logged_in = False

    @property
    def connected(self) -> bool:
        if self._ssh is None:
            return False
        t = self._ssh.get_transport()
        return bool(t and t.is_active())

    # ---------- raw command execution ----------
    def run(self, cmd: str, timeout: int = 60) -> CommandResult:
        if not self.connected:
            raise NotConnected("SSH session is not connected")
        assert self._ssh is not None
        # paramiko's SSHClient.exec_command is not fully re-entrant under
        # concurrent calls; serialise channel creation while still letting
        # the event loop breathe elsewhere.
        with self._run_lock:
            stdin, stdout, stderr = self._ssh.exec_command(cmd, timeout=timeout)
        try:
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()
        finally:
            try:
                stdin.close(); stdout.close(); stderr.close()
            except Exception:
                pass
        return CommandResult(out, err, rc)

    # ---------- amgr (budget manager) ----------
    # ``amgr`` is a Python CLI that hits an internal REST endpoint via
    # ``urllib``. Some users have ``http_proxy`` / ``https_proxy`` exported
    # in their ``.bashrc`` (e.g. proxy22.iitd.ac.in), but that proxy host is
    # NOT resolvable from the login-node context where ``amgr`` runs, so the
    # call dies with ``urlopen error [Errno -2] Name or service not known``.
    # We strip those vars for every ``amgr`` invocation; nothing else the
    # GUI runs (ssh/pbs/sftp) needs them.
    _AMGR_PREFIX = "unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && "

    def amgr_login(self, password: str) -> CommandResult:
        """Log in to the budgets service. Required before amgr ls/checkbalance."""
        safe = password.replace("'", "'\\''")
        res = self.run(f"{self._AMGR_PREFIX}amgr login -p '{safe}'", timeout=45)
        self.amgr_logged_in = res.ok
        return res

    def list_projects(self) -> List[str]:
        res = self.run(f"{self._AMGR_PREFIX}amgr ls project", timeout=30)
        if not res.ok:
            return []
        return parse_projects(res.stdout)

    def check_balance(self, project: str) -> Optional[float]:
        if not project:
            return None
        safe = project.replace("'", "'\\''")
        res = self.run(
            f"{self._AMGR_PREFIX}amgr checkbalance project -n '{safe}'",
            timeout=30,
        )
        if not res.ok:
            return None
        return parse_balance(res.stdout)

    # ---------- PBS job commands ----------
    def list_my_jobs(self, include_history: bool = False) -> Tuple[List[Job], str]:
        flag = "-x " if include_history else ""
        res = self.run(f"qstat {flag}-u {self.username}", timeout=30)
        return parse_qstat_u(res.stdout), res.output

    def list_my_jobs_enriched(
        self, include_history: bool = False
    ) -> Tuple[List[Job], str, dict]:
        """Return ``(jobs, raw, extras)`` where ``extras[job_id]`` is a dict
        with keys ``ncpus``, ``ngpus``, ``nodes``, ``est_start``.

        Makes at most two SSH calls: one ``qstat -u`` and one batched
        ``qstat -f <ids>``.
        """
        jobs, raw = self.list_my_jobs(include_history=include_history)
        extras: dict = {}
        if jobs:
            ids = " ".join(j.job_id for j in jobs)
            res = self.run(f"qstat -f {ids}", timeout=45)
            if res.ok:
                parsed = parse_qstat_f_many(res.stdout)
                for jid, fields in parsed.items():
                    sel = parse_select(fields.get("Resource_List.select", ""))
                    extras[jid] = {
                        "nodes": sel["nodes"],
                        "ncpus": sel["ncpus"],
                        "ngpus": sel["ngpus"],
                        "est_start": fields.get("estimated.start_time", ""),
                        "comment": fields.get("comment", ""),
                        "queue": fields.get("queue", ""),
                    }
        return jobs, raw, extras

    def list_queues(self) -> Tuple[List[Queue], str]:
        res = self.run("qstat -Q", timeout=30)
        return parse_qstat_Q(res.stdout), res.output

    def list_queue_jobs(self, queue: str) -> Tuple[List[Job], str]:
        """List jobs in a specific queue across all users.

        Uses ``qstat -aw <queue>`` which emits the same wide column format as
        ``qstat -u`` and so can be parsed by :func:`parse_qstat_u`.
        """
        safe = queue.replace("'", "").replace(" ", "")
        res = self.run(f"qstat -aw {safe}", timeout=30)
        return parse_qstat_u(res.stdout), res.output

    def job_full(self, job_id: str) -> CommandResult:
        return self.run(f"qstat -f {job_id}", timeout=30)

    def job_full_parsed(self, job_id: str) -> dict:
        res = self.job_full(job_id)
        if not res.ok:
            return {}
        return parse_qstat_f(res.stdout)

    def job_short(self, job_id: str) -> CommandResult:
        return self.run(f"qstat -anxws {job_id}", timeout=30)

    def job_estimate(self, job_id: str) -> CommandResult:
        return self.run(f"qstat -T {job_id}", timeout=30)

    def qdel(self, job_id: str, force: bool = False) -> CommandResult:
        cmd = f"qdel {'-W force ' if force else ''}{job_id}"
        return self.run(cmd, timeout=30)

    def qhold(self, job_id: str) -> CommandResult:
        return self.run(f"qhold {job_id}", timeout=30)

    def qrls(self, job_id: str) -> CommandResult:
        return self.run(f"qrls {job_id}", timeout=30)

    def qsub(self, remote_script_path: str, workdir: Optional[str] = None,
             extra_flags: str = "") -> Tuple[Optional[str], CommandResult]:
        """Submit an existing PBS script already present on the remote host.

        Returns (job_id or None, full CommandResult).
        """
        # Expand '~' to absolute so cd + path resolution always works.
        script = self.expand(remote_script_path)
        wd = self.expand(workdir) if workdir else None
        cd = f"cd {shell_quote(wd)} && " if wd else ""
        flags = f" {extra_flags}" if extra_flags else ""
        res = self.run(f"{cd}qsub{flags} {shell_quote(script)}", timeout=60)
        return (parse_qsub_output(res.stdout) if res.ok else None), res

    # ---------- SFTP helpers ----------
    def _ensure_sftp(self) -> paramiko.SFTPClient:
        if not self.connected:
            raise NotConnected("SSH session is not connected")
        assert self._ssh is not None
        if self._sftp is None:
            self._sftp = self._ssh.open_sftp()
        return self._sftp

    def expand(self, path: str) -> str:
        """Expand '~' using the remote home directory. Relative paths are
        resolved relative to $HOME (consistent with how users typically type them)."""
        if not path:
            return path
        if path.startswith("~/"):
            return posixpath.join(self.home, path[2:])
        if path == "~":
            return self.home
        if path.startswith("/"):
            return path
        # Relative path -> anchor at $HOME
        return posixpath.join(self.home, path) if self.home else path

    def listdir(self, remote_dir: str) -> List[Tuple[str, int, bool, int]]:
        """Return list of (name, size, is_dir, mtime) entries."""
        remote_dir = self.expand(remote_dir)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            entries = sftp.listdir_attr(remote_dir)
        out = []
        for e in entries:
            is_dir = stat_mod.S_ISDIR(e.st_mode or 0)
            out.append((e.filename, e.st_size or 0, is_dir, e.st_mtime or 0))
        out.sort(key=lambda r: (not r[2], r[0].lower()))
        return out

    def stat(self, path: str) -> Optional[paramiko.SFTPAttributes]:
        path = self.expand(path)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            try:
                return sftp.stat(path)
            except IOError:
                return None

    def is_dir(self, path: str) -> bool:
        s = self.stat(path)
        return bool(s and stat_mod.S_ISDIR(s.st_mode or 0))

    def read_text(self, remote_path: str, max_bytes: int = 2_000_000,
                  tail: bool = False) -> str:
        """Read a remote file as text. If ``tail`` is set, reads the last
        ``max_bytes`` bytes rather than the first."""
        remote_path = self.expand(remote_path)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            attr = sftp.stat(remote_path)
            size = attr.st_size or 0
            with sftp.file(remote_path, "rb") as f:
                if tail and size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")

    def mkdirs(self, path: str) -> None:
        path = self.expand(path)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            _mkdirs(sftp, path)

    def remove(self, path: str) -> None:
        path = self.expand(path)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            try:
                attr = sftp.stat(path)
            except IOError:
                return
            if stat_mod.S_ISDIR(attr.st_mode or 0):
                _rmtree(sftp, path)
            else:
                sftp.remove(path)

    def rename(self, old: str, new: str) -> None:
        old = self.expand(old); new = self.expand(new)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            sftp.posix_rename(old, new) if hasattr(sftp, "posix_rename") else sftp.rename(old, new)

    def put_file(self, local: str, remote: str,
                 progress: Optional[Callable[[int, int], None]] = None) -> None:
        remote = self.expand(remote)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            _mkdirs(sftp, posixpath.dirname(remote))
            sftp.put(local, remote, callback=progress)
            if local.endswith((".sh", ".pbs")) or os.access(local, os.X_OK):
                try:
                    sftp.chmod(remote, 0o755)
                except Exception:
                    pass

    def get_file(self, remote: str, local: str,
                 progress: Optional[Callable[[int, int], None]] = None) -> None:
        remote = self.expand(remote)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
            sftp.get(remote, local, callback=progress)

    def upload_tree(self, local_dir: str, remote_dir: str,
                    log: Callable[[str], None] = lambda m: None,
                    progress: Callable[[int, int, str], None] = lambda d, t, m: None,
                    skip_same_size: bool = True,
                    should_cancel: Callable[[], bool] = lambda: False) -> Tuple[int, int]:
        """Recursively upload a local tree. Returns (uploaded, skipped) counts."""
        remote_dir = self.expand(remote_dir)
        local_dir = os.path.abspath(local_dir)
        # First walk to get totals for progress
        all_files: List[Tuple[str, str, int]] = []
        for root, dirs, files in os.walk(local_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", ".venv", "node_modules")]
            rel = os.path.relpath(root, local_dir).replace(os.sep, "/")
            rdir = remote_dir if rel in (".", "") else posixpath.join(remote_dir, rel)
            for fn in files:
                if fn.endswith(".pyc"):
                    continue
                lpath = os.path.join(root, fn)
                rpath = posixpath.join(rdir, fn)
                try:
                    size = os.path.getsize(lpath)
                except OSError:
                    continue
                all_files.append((lpath, rpath, size))
        total = len(all_files)
        uploaded = skipped = 0
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            _mkdirs(sftp, remote_dir)
            seen_dirs = {remote_dir}
            for i, (lpath, rpath, size) in enumerate(all_files):
                if should_cancel():
                    log("[cancelled]")
                    break
                rd = posixpath.dirname(rpath)
                if rd not in seen_dirs:
                    _mkdirs(sftp, rd)
                    seen_dirs.add(rd)
                if skip_same_size:
                    try:
                        rst = sftp.stat(rpath)
                        if rst.st_size == size:
                            skipped += 1
                            progress(i + 1, total, os.path.basename(lpath))
                            continue
                    except IOError:
                        pass
                log(f"[put] {os.path.relpath(lpath, local_dir)} ({size} bytes)")
                try:
                    sftp.put(lpath, rpath)
                    if lpath.endswith((".sh", ".pbs")) or os.access(lpath, os.X_OK):
                        try:
                            sftp.chmod(rpath, 0o755)
                        except Exception:
                            pass
                    uploaded += 1
                except Exception as e:
                    log(f"[err ] {lpath}: {e}")
                progress(i + 1, total, os.path.basename(lpath))
        return uploaded, skipped

    def download_tree(self, remote_dir: str, local_dir: str,
                      log: Callable[[str], None] = lambda m: None,
                      progress: Callable[[int, int, str], None] = lambda d, t, m: None,
                      should_cancel: Callable[[], bool] = lambda: False) -> Tuple[int, int]:
        remote_dir = self.expand(remote_dir)
        local_dir = os.path.abspath(local_dir)
        with self._sftp_lock:
            sftp = self._ensure_sftp()
            all_files: List[Tuple[str, str]] = []
            for rpath, lpath in _walk_remote(sftp, remote_dir, local_dir):
                all_files.append((rpath, lpath))
            total = len(all_files)
            downloaded = skipped = 0
            for i, (rpath, lpath) in enumerate(all_files):
                if should_cancel():
                    log("[cancelled]")
                    break
                os.makedirs(os.path.dirname(lpath), exist_ok=True)
                try:
                    rst = sftp.stat(rpath)
                    if os.path.exists(lpath) and os.path.getsize(lpath) == (rst.st_size or 0):
                        skipped += 1
                        progress(i + 1, total, os.path.basename(rpath))
                        continue
                except IOError:
                    pass
                log(f"[get] {os.path.relpath(rpath, remote_dir)}")
                try:
                    sftp.get(rpath, lpath)
                    downloaded += 1
                except Exception as e:
                    log(f"[err ] {rpath}: {e}")
                progress(i + 1, total, os.path.basename(rpath))
        return downloaded, skipped


# ---------- private helpers ----------
def _mkdirs(sftp: paramiko.SFTPClient, path: str) -> None:
    if path in ("/", "", "."):
        return
    try:
        sftp.stat(path)
        return
    except IOError:
        pass
    parent = posixpath.dirname(path)
    if parent and parent != path:
        _mkdirs(sftp, parent)
    try:
        sftp.mkdir(path)
    except IOError:
        pass


def _rmtree(sftp: paramiko.SFTPClient, path: str) -> None:
    for entry in sftp.listdir_attr(path):
        sub = posixpath.join(path, entry.filename)
        if stat_mod.S_ISDIR(entry.st_mode or 0):
            _rmtree(sftp, sub)
        else:
            sftp.remove(sub)
    sftp.rmdir(path)


def _walk_remote(sftp: paramiko.SFTPClient, rroot: str, lroot: str
                 ) -> Iterator[Tuple[str, str]]:
    """Yield (remote_path, local_path) for every file under rroot recursively."""
    for entry in sftp.listdir_attr(rroot):
        rp = posixpath.join(rroot, entry.filename)
        lp = os.path.join(lroot, entry.filename)
        if stat_mod.S_ISDIR(entry.st_mode or 0):
            yield from _walk_remote(sftp, rp, lp)
        else:
            yield rp, lp


def shell_quote(s: str) -> str:
    """Single-quote a string for safe shell embedding."""
    if s == "" or any(c in s for c in " \t\"'$`\\!*?"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s
