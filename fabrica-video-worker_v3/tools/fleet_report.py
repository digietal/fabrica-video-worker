"""v17: relatório da frota a partir dos logs (um ou vários arquivos, um por Worker).

Uso: python tools/fleet_report.py logs/*.txt [--run RUN_ID]
Lê as linhas "📊 {...}" e mostra: Workers ativos, duração por etapa, espera na fila, retries,
falhas, tráfego, ocupação (saturação/ociosidade) e tempo total da produção.
Somente leitura; não acessa banco nem Gateway.
"""
import json
import statistics
import sys
from collections import defaultdict


def parse(paths):
    for p in paths:
        with open(p, errors="replace") as f:
            for line in f:
                i = line.find("📊 ")
                if i >= 0:
                    try:
                        yield json.loads(line[i + 2:])
                    except ValueError:
                        pass


def report(events, run=None):
    ends = [e for e in events if e.get("event") == "job_end" and (not run or e.get("run_id") == run)]
    starts = [e for e in events if e.get("event") == "job_start" and (not run or e.get("run_id") == run)]
    hbs = [e for e in events if e.get("event") == "worker_heartbeat"]
    out = {"workers": sorted({e.get("worker_id") for e in ends + starts if e.get("worker_id")}), "jobs": len(ends)}
    if ends:
        t0 = min(e["started_at"] for e in ends)
        t1 = max(e["ended_at"] for e in ends)
        out["wall_s"] = round(t1 - t0, 1)
        busy = sum(e.get("duration_s", 0) for e in ends)
        out["busy_s"] = round(busy, 1)
    stages = defaultdict(list)
    for e in ends:
        stages[e.get("type")].append(e.get("duration_s", 0))
    out["stages"] = {k: {"n": len(v), "mean_s": round(statistics.mean(v), 2), "max_s": max(v), "sum_s": round(sum(v), 1)}
                     for k, v in sorted(stages.items())}
    waits = [e["queue_wait_s"] for e in starts if e.get("queue_wait_s") is not None]
    if waits:
        out["queue_wait_s"] = {"p50": statistics.median(waits), "max": max(waits)}
    out["status"] = defaultdict(int)
    for e in ends:
        out["status"][e.get("status")] += 1
    out["retries"] = sum(1 for e in starts if (e.get("attempts") or 1) > 1)
    out["download_mb"] = round(sum(e.get("download_bytes", 0) for e in ends) / 2**20, 1)
    out["upload_mb"] = round(sum(e.get("upload_bytes", 0) for e in ends) / 2**20, 1)
    if hbs:
        cap = sum(h.get("capacity", 0) for h in hbs)
        act = sum(h.get("active_jobs", 0) for h in hbs)
        out["occupancy_pct"] = round(100 * act / cap, 1) if cap else None
        out["idle_polls"] = sum(h.get("idle_polls", 0) for h in hbs)
        out["min_free_mem_mb"] = min((h.get("mem_free_mb") for h in hbs if h.get("mem_free_mb") is not None), default=None)
    out["status"] = dict(out["status"])
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    run = None
    if "--run" in args:
        i = args.index("--run")
        run = args[i + 1]
        del args[i:i + 2]
    print(json.dumps(report(list(parse(args)), run), indent=2, ensure_ascii=False))
