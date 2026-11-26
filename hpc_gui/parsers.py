"""Parsers for PBS qstat / amgr outputs."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Job:
    job_id: str
    username: str = ""
    queue: str = ""
    name: str = ""
    sess_id: str = ""
    nds: str = ""
    tsk: str = ""
    memory: str = ""
    walltime: str = ""
    state: str = ""
    elapsed: str = ""


@dataclass
class Queue:
    name: str
    max: str = ""
    total: str = ""
    enabled: str = ""
    started: str = ""
    queued: str = ""
    running: str = ""
    held: str = ""
    waiting: str = ""
    transit: str = ""
    exiting: str = ""
    qtype: str = ""


_DASH_RE = re.compile(r"^-+\s")


def parse_qstat_u(text: str) -> List[Job]:
    """Parse output of ``qstat -u <user>`` / ``qstat -x -u <user>``.

    The format is fixed-width with a header line beginning with 'Job ID'
    and a separator made of dashes/spaces. Example row::

        883831.pbshpc   mt12307* standard clip_test   20774   1   8    --  01:00 R 00:04
    """
    lines = text.splitlines()
    # Find the separator line
    sep_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("---") and "---" in ln[4:]:
            sep_idx = i
            break
    if sep_idx is None:
        return []
    sep = lines[sep_idx]
    # Column widths inferred from separator; split by runs of dashes
    widths = []
    start = 0
    while start < len(sep):
        if sep[start] == "-":
            end = start
            while end < len(sep) and sep[end] == "-":
                end += 1
            widths.append((start, end))
            start = end
        else:
            start += 1
    jobs: List[Job] = []
    for ln in lines[sep_idx + 1:]:
        if not ln.strip() or ln.lstrip().startswith(("cice", "cpu", "gpu")):
            # Belongs to -w node continuation lines; skip
            continue
        cells: List[str] = []
        for i, (a, b) in enumerate(widths):
            # Extend right to next column or end of line
            right = widths[i + 1][0] if i + 1 < len(widths) else len(ln)
            cells.append(ln[a:right].strip() if a < len(ln) else "")
        if len(cells) < 11 or not cells[0]:
            continue
        jobs.append(Job(
            job_id=cells[0],
            username=cells[1],
            queue=cells[2],
            name=cells[3],
            sess_id=cells[4],
            nds=cells[5],
            tsk=cells[6],
            memory=cells[7],
            walltime=cells[8],
            state=cells[9],
            elapsed=cells[10],
        ))
    return jobs


def parse_qstat_Q(text: str) -> List[Queue]:
    """Parse ``qstat -Q`` (queue summary) output."""
    lines = text.splitlines()
    sep_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("---"):
            sep_idx = i
            break
    if sep_idx is None:
        return []
    queues: List[Queue] = []
    for ln in lines[sep_idx + 1:]:
        parts = ln.split()
        if len(parts) < 12:
            continue
        queues.append(Queue(
            name=parts[0],
            max=parts[1],
            total=parts[2],
            enabled=parts[3],
            started=parts[4],
            queued=parts[5],
            running=parts[6],
            held=parts[7],
            waiting=parts[8],
            transit=parts[9],
            exiting=parts[10],
            qtype=parts[11],
        ))
    return queues


def parse_qstat_f(text: str) -> Dict[str, str]:
    """Parse the key-value output of ``qstat -f <jobid>``.

    Values may continue on subsequent lines (indented with a tab). We rejoin
    continuation lines.
    """
    result: Dict[str, str] = {}
    current_key: Optional[str] = None
    current_val_parts: List[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("Job Id:"):
            if current_key:
                result[current_key] = "".join(current_val_parts).strip()
            result["Job Id"] = raw.split(":", 1)[1].strip()
            current_key = None
            current_val_parts = []
            continue
        if raw.startswith("\t") or raw.startswith("    \t") or raw.startswith("\t"):
            # Continuation
            current_val_parts.append(raw.strip())
            continue
        if "=" in raw:
            if current_key:
                result[current_key] = "".join(current_val_parts).strip()
            key, val = raw.split("=", 1)
            current_key = key.strip()
            current_val_parts = [val.strip()]
        else:
            current_val_parts.append(raw.strip())
    if current_key:
        result[current_key] = "".join(current_val_parts).strip()
    return result


def parse_qstat_f_many(text: str) -> Dict[str, Dict[str, str]]:
    """Parse ``qstat -f`` output covering multiple job IDs.

    Returns a mapping of ``job_id -> {field: value}``. Each record starts with a
    ``Job Id: <id>`` line.
    """
    chunks: Dict[str, Dict[str, str]] = {}
    current_lines: List[str] = []
    current_id: Optional[str] = None

    def flush() -> None:
        if current_id is None:
            return
        chunks[current_id] = parse_qstat_f("\n".join(current_lines))

    for raw in text.splitlines():
        if raw.startswith("Job Id:"):
            flush()
            current_id = raw.split(":", 1)[1].strip()
            current_lines = [raw]
        else:
            current_lines.append(raw)
    flush()
    return chunks


_SELECT_NGPUS_RE = re.compile(r"ngpus=(\d+)")
_SELECT_NCPUS_RE = re.compile(r"ncpus=(\d+)")
_SELECT_NODES_RE = re.compile(r"^\s*(\d+)\s*:")


def parse_select(select: str) -> Dict[str, int]:
    """Parse a PBS ``Resource_List.select`` spec like
    ``2:ncpus=16:ngpus=1:mem=30gb`` into ``{nodes, ncpus, ngpus}`` totals.
    """
    if not select:
        return {"nodes": 0, "ncpus": 0, "ngpus": 0}
    # A select can have chunks joined by '+'. Sum over them.
    total_nodes = total_ncpus = total_ngpus = 0
    for chunk in select.split("+"):
        m_nodes = _SELECT_NODES_RE.match(chunk)
        nodes = int(m_nodes.group(1)) if m_nodes else 1
        m_cpu = _SELECT_NCPUS_RE.search(chunk)
        ncpus = int(m_cpu.group(1)) if m_cpu else 0
        m_gpu = _SELECT_NGPUS_RE.search(chunk)
        ngpus = int(m_gpu.group(1)) if m_gpu else 0
        total_nodes += nodes
        total_ncpus += nodes * ncpus
        total_ngpus += nodes * ngpus
    return {"nodes": total_nodes, "ncpus": total_ncpus, "ngpus": total_ngpus}


_COST_RE = re.compile(r"'Cost'\s*:\s*([-+]?[0-9]*\.?[0-9]+)")


def parse_balance(text: str) -> Optional[float]:
    """Extract ``Cost`` value from ``amgr checkbalance project ...`` output."""
    m = _COST_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def parse_projects(text: str) -> List[str]:
    """Parse ``amgr ls project`` output — one project name per line."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("Code ") or ln.startswith("Message"):
            continue
        out.append(ln)
    return out


def parse_qsub_output(text: str) -> Optional[str]:
    """qsub prints the new job id on success, e.g. ``883831.pbshpc``."""
    for ln in text.splitlines():
        ln = ln.strip()
        if "." in ln and ln.split(".")[0].isdigit():
            return ln
    return None
