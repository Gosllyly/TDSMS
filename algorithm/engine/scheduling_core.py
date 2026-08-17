import time
import sys
import random
from pathlib import Path
import pandas as pd
from ortools.sat.python import cp_model

from input_data import build_schedule_inputs, get_mix_spec, get_pack_spec


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _to_int(v, scale):
    return int(round(float(v) * scale))


def _scale_from_preset(speed_preset):
    preset = (speed_preset or "balanced").lower()
    if preset == "fast":
        return 20
    if preset == "accurate":
        return 200
    return 100


def _build_horizon(I, B, J1, J2, J3, J4, p1i, p2i, p3i, p4i, di, scale):
    # A tighter but safe upper bound: serialize all tasks.
    total = 0
    for i in I:
        n = len(B[i])
        mix = max((p1i[i, j] for j in J1 if (i, j) in p1i), default=0)
        tab = max((p2i[i, j] for j in J2 if (i, j) in p2i), default=0)
        coat = max((p3i[i, j] for j in J3 if (i, j) in p3i), default=0)
        pack = max((p4i[i, j] for j in J4 if (i, j) in p4i), default=0)
        total += n * (mix + tab + coat + pack)
    total += max(di.values(), default=0)
    total += 200 * scale
    return int(max(total, 2000))


def _drug_family(item):
    return str(item).rsplit(" ", 1)[0]


def _pack_family(item):
    return str(item)


def _add_pairwise_setup_constraints(model, tasks, setup_fn, name):
    for a_idx in range(len(tasks)):
        for b_idx in range(a_idx + 1, len(tasks)):
            a = tasks[a_idx]
            b = tasks[b_idx]
            setup_ab = int(setup_fn(a, b))
            setup_ba = int(setup_fn(b, a))
            if setup_ab == 0 and setup_ba == 0:
                continue

            before = model.NewBoolVar(f"{name}_before_{a_idx}_{b_idx}")

            model.Add(b["start"] >= a["end"] + setup_ab).OnlyEnforceIf(
                [a["presence"], b["presence"], before]
            )
            model.Add(a["start"] >= b["end"] + setup_ba).OnlyEnforceIf(
                [a["presence"], b["presence"], before.Not()]
            )


def _add_sequence_setup_and_run_constraints(model, tasks, setup_fn, max_run, periodic_clear, name):
    if not tasks:
        return

    total_duration_bound = sum(int(task["duration"]) for task in tasks)
    if max_run is None or total_duration_bound <= int(max_run):
        _add_pairwise_setup_constraints(model, tasks, setup_fn, name)
        return

    arcs = []
    arc_lits = {}
    reset_lits = {}
    presences = [task["presence"] for task in tasks]
    any_present = model.NewBoolVar(f"{name}_any_present")
    model.AddMaxEquality(any_present, presences)
    arcs.append((0, 0, any_present.Not()))

    for idx, task in enumerate(tasks, start=1):
        arcs.append((idx, idx, task["presence"].Not()))

        first_lit = model.NewBoolVar(f"{name}_first_{idx}")
        last_lit = model.NewBoolVar(f"{name}_last_{idx}")
        model.AddImplication(first_lit, task["presence"])
        model.AddImplication(last_lit, task["presence"])
        arcs.append((0, idx, first_lit))
        arcs.append((idx, 0, last_lit))
        arc_lits[0, idx] = first_lit

    for a_idx, a in enumerate(tasks, start=1):
        for b_idx, b in enumerate(tasks, start=1):
            if a_idx == b_idx:
                continue

            lit = model.NewBoolVar(f"{name}_arc_{a_idx}_{b_idx}")
            model.AddImplication(lit, a["presence"])
            model.AddImplication(lit, b["presence"])
            arcs.append((a_idx, b_idx, lit))
            arc_lits[a_idx, b_idx] = lit

            setup_ab = int(setup_fn(a, b))
            if setup_ab > 0 or max_run is None:
                model.Add(b["start"] >= a["end"] + setup_ab).OnlyEnforceIf(lit)
            else:
                reset_lit = model.NewBoolVar(f"{name}_reset_{a_idx}_{b_idx}")
                model.AddImplication(reset_lit, lit)
                reset_lits[a_idx, b_idx] = reset_lit
                model.Add(b["start"] >= a["end"]).OnlyEnforceIf([lit, reset_lit.Not()])
                model.Add(b["start"] >= a["end"] + int(periodic_clear)).OnlyEnforceIf([lit, reset_lit])

    model.AddCircuit(arcs)

    if max_run is None:
        return

    acc = {}
    for idx, task in enumerate(tasks, start=1):
        acc[idx] = model.NewIntVar(0, max_run, f"{name}_continuous_before_{idx}")
        model.Add(acc[idx] + int(task["duration"]) <= max_run).OnlyEnforceIf(task["presence"])
        model.Add(acc[idx] == 0).OnlyEnforceIf(arc_lits[0, idx])

    for a_idx, a in enumerate(tasks, start=1):
        for b_idx, b in enumerate(tasks, start=1):
            if a_idx == b_idx:
                continue

            lit = arc_lits[a_idx, b_idx]
            setup_ab = int(setup_fn(a, b))
            if setup_ab > 0:
                model.Add(acc[b_idx] == 0).OnlyEnforceIf(lit)
            else:
                reset_lit = reset_lits.get((a_idx, b_idx))
                if reset_lit is None:
                    model.Add(acc[b_idx] == acc[a_idx] + int(a["duration"])).OnlyEnforceIf(lit)
                else:
                    model.Add(acc[b_idx] == acc[a_idx] + int(a["duration"])).OnlyEnforceIf(
                        [lit, reset_lit.Not()]
                    )
                    model.Add(acc[b_idx] == 0).OnlyEnforceIf([lit, reset_lit])


def _add_staff_capacity(model, intervals, capacity, name):
    if not intervals:
        return
    cap = int(capacity)
    if cap <= 0:
        model.Add(0 == 1)
        return
    model.AddCumulative(intervals, [1] * len(intervals), cap)


def _build_model(
    I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
    stage_staff_limits=None,
    clear_time_matrices=None,
    machine_available_time=None,
    release_time=None,
    max_continuous_run=None,
    periodic_cleaning_time=1.0,
):
    model = cp_model.CpModel()

    p1i = {(i, j): _to_int(v, scale) for (i, j), v in p1.items()}
    p2i = {(i, j): _to_int(v, scale) for (i, j), v in p2.items()}
    p3i = {(i, j): _to_int(v, scale) for (i, j), v in p3.items()}
    p4i = {(i, j): _to_int(v, scale) for (i, j), v in p4.items()}
    di = {i: _to_int(d.get(i, 62), scale) for i in I}
    Ti = {i: _to_int(T.get(i, 0), scale) for i in I}
    vj = {j: _to_int((machine_available_time or {}).get(j, 0), scale) for j in J1 + J2 + J3 + J4}
    rb = {(i, b): _to_int((release_time or {}).get((i, b), 0), scale) for i in I for b in B[i]}
    clear_time_matrices = clear_time_matrices or {}
    setup_i = {
        stage: {
            (from_i, to_i): _to_int(v, scale)
            for (from_i, to_i), v in clear_time_matrices.get(stage, {}).items()
        }
        for stage in (1, 2, 3, 4)
    }

    horizon = _build_horizon(I, B, J1, J2, J3, J4, p1i, p2i, p3i, p4i, di, scale)
    max_setup = max(
        (v for matrix in setup_i.values() for v in matrix.values()),
        default=0,
    )
    horizon += int(max_setup * max(1, sum(len(B[i]) for i in I)))

    t1, t2, t3, t4 = {}, {}, {}, {}
    e1, e2, e3 = {}, {}, {}
    x1, x2, x3, x4 = {}, {}, {}, {}
    E, finish_i = {}, {}
    batch_finish, batch_cycle, batch_cycle_over = {}, {}, {}

    stage_machine_tasks = {
        1: {j: [] for j in J1},
        2: {j: [] for j in J2},
        3: {j: [] for j in J3},
        4: {j: [] for j in J4},
    }
    stage_intervals = {1: [], 2: [], 3: [], 4: []}
    pack_end_by_machine = {}

    for i in I:
        t4[i] = model.NewIntVar(0, horizon, f"t4_{i}")
        E[i] = model.NewIntVar(0, horizon, f"E_{i}")
        finish_i[i] = model.NewIntVar(0, horizon, f"finish_{i}")

        valid_j4 = [j for j in J4 if (i, j) in p4i]
        for j in valid_j4:
            x4[i, j] = model.NewBoolVar(f"x4_{i}_{j}")
        if valid_j4:
            model.Add(sum(x4[i, j] for j in valid_j4) == 1)

        needs_coating = any((i, j) in p3i for j in J3)
        batches = B[i]

        for idx, b in enumerate(batches):
            t1[i, b] = model.NewIntVar(0, horizon, f"t1_{i}_{b}")
            e1[i, b] = model.NewIntVar(0, horizon, f"e1_{i}_{b}")
            t2[i, b] = model.NewIntVar(0, horizon, f"t2_{i}_{b}")
            e2[i, b] = model.NewIntVar(0, horizon, f"e2_{i}_{b}")
            batch_finish[i, b] = model.NewIntVar(0, horizon, f"batch_finish_{i}_{b}")
            batch_cycle[i, b] = model.NewIntVar(0, horizon, f"batch_cycle_{i}_{b}")
            batch_cycle_over[i, b] = model.NewIntVar(0, horizon, f"batch_cycle_over_{i}_{b}")

            valid_j1 = [j for j in J1 if (i, j) in p1i]
            for j in valid_j1:
                x1[i, b, j] = model.NewBoolVar(f"x1_{i}_{b}_{j}")
                itv = model.NewOptionalIntervalVar(t1[i, b], p1i[i, j], e1[i, b], x1[i, b, j], f"mix_{i}_{b}_{j}")
                stage_machine_tasks[1][j].append({
                    "start": t1[i, b],
                    "end": e1[i, b],
                    "presence": x1[i, b, j],
                    "item": i,
                    "feature": get_mix_spec(i),
                    "batch": b,
                    "interval": itv,
                    "duration": p1i[i, j],
                })
                stage_intervals[1].append(itv)
                model.Add(t1[i, b] >= vj[j]).OnlyEnforceIf(x1[i, b, j])
            if valid_j1:
                model.Add(sum(x1[i, b, j] for j in valid_j1) == 1)
            model.Add(t1[i, b] >= rb[i, b])

            valid_j2 = [j for j in J2 if (i, j) in p2i]
            for j in valid_j2:
                x2[i, b, j] = model.NewBoolVar(f"x2_{i}_{b}_{j}")
                itv = model.NewOptionalIntervalVar(t2[i, b], p2i[i, j], e2[i, b], x2[i, b, j], f"tab_{i}_{b}_{j}")
                stage_machine_tasks[2][j].append({
                    "start": t2[i, b],
                    "end": e2[i, b],
                    "presence": x2[i, b, j],
                    "item": i,
                    "feature": get_mix_spec(i),
                    "batch": b,
                    "interval": itv,
                    "duration": p2i[i, j],
                })
                stage_intervals[2].append(itv)
                model.Add(t2[i, b] >= vj[j]).OnlyEnforceIf(x2[i, b, j])
            if valid_j2:
                model.Add(sum(x2[i, b, j] for j in valid_j2) == 1)

            model.Add(t2[i, b] >= e1[i, b])

            if needs_coating:
                t3[i, b] = model.NewIntVar(0, horizon, f"t3_{i}_{b}")
                e3[i, b] = model.NewIntVar(0, horizon, f"e3_{i}_{b}")

                valid_j3 = [j for j in J3 if (i, j) in p3i]
                for j in valid_j3:
                    x3[i, b, j] = model.NewBoolVar(f"x3_{i}_{b}_{j}")
                    itv = model.NewOptionalIntervalVar(t3[i, b], p3i[i, j], e3[i, b], x3[i, b, j], f"coat_{i}_{b}_{j}")
                    stage_machine_tasks[3][j].append({
                        "start": t3[i, b],
                        "end": e3[i, b],
                        "presence": x3[i, b, j],
                        "item": i,
                        "feature": get_mix_spec(i),
                        "batch": b,
                        "interval": itv,
                        "duration": p3i[i, j],
                    })
                    stage_intervals[3].append(itv)
                    model.Add(t3[i, b] >= vj[j]).OnlyEnforceIf(x3[i, b, j])
                if valid_j3:
                    model.Add(sum(x3[i, b, j] for j in valid_j3) == 1)
                model.Add(t3[i, b] >= e2[i, b])

            for j in valid_j4:
                batch_pack_start = model.NewIntVar(0, horizon, f"sp_{i}_{b}_{j}")
                batch_pack_end = model.NewIntVar(0, horizon, f"ep_{i}_{b}_{j}")
                model.Add(batch_pack_start == t4[i] + idx * p4i[i, j]).OnlyEnforceIf(x4[i, j])
                model.Add(batch_pack_end == batch_pack_start + p4i[i, j]).OnlyEnforceIf(x4[i, j])
                model.Add(batch_finish[i, b] == batch_pack_end).OnlyEnforceIf(x4[i, j])
                if needs_coating:
                    model.Add(batch_pack_start >= e3[i, b]).OnlyEnforceIf(x4[i, j])
                else:
                    model.Add(batch_pack_start >= e2[i, b]).OnlyEnforceIf(x4[i, j])
            model.Add(batch_cycle[i, b] == batch_finish[i, b] - t1[i, b])
            model.Add(batch_cycle_over[i, b] >= batch_cycle[i, b] - Ti[i])

        for j in valid_j4:
            pack_duration = len(batches) * p4i[i, j]
            pack_end_by_machine[i, j] = model.NewIntVar(0, horizon, f"pack_end_{i}_{j}")
            itv_p = model.NewOptionalIntervalVar(
                t4[i], pack_duration, pack_end_by_machine[i, j], x4[i, j], f"pack_spec_{i}_{j}"
            )
            stage_machine_tasks[4][j].append({
                "start": t4[i],
                "end": pack_end_by_machine[i, j],
                "presence": x4[i, j],
                "item": i,
                "batch": None,
                "interval": itv_p,
                "duration": pack_duration,
            })
            stage_intervals[4].append(itv_p)
            model.Add(t4[i] >= vj[j]).OnlyEnforceIf(x4[i, j])
            model.Add(finish_i[i] == pack_end_by_machine[i, j]).OnlyEnforceIf(x4[i, j])

        model.Add(E[i] >= finish_i[i] - di[i])

    def batch_setup(stage):
        def _setup(a, b):
            return setup_i.get(stage, {}).get((a["item"], b["item"]), 0)
        return _setup

    def pack_setup(a, b):
        return setup_i.get(4, {}).get((a["item"], b["item"]), 0)

    max_run = _to_int(max_continuous_run, scale) if max_continuous_run is not None else None
    periodic_clear = _to_int(periodic_cleaning_time, scale)
    for stage in (1, 2, 3):
        for j, tasks in stage_machine_tasks[stage].items():
            if tasks:
                model.AddNoOverlap([task["interval"] for task in tasks])
                _add_sequence_setup_and_run_constraints(
                    model, tasks, batch_setup(stage), max_run, periodic_clear, f"seq_s{stage}_{j}"
                )
    for j, tasks in stage_machine_tasks[4].items():
        if tasks:
            model.AddNoOverlap([task["interval"] for task in tasks])
            _add_pairwise_setup_constraints(model, tasks, pack_setup, f"setup_s4_{j}")

    stage_staff_limits = stage_staff_limits or {
        1: max(1, len(J1)),
        2: max(1, len(J2)),
        3: max(1, len(J3)),
        4: max(1, len(J4)),
    }
    for stage in (1, 2, 3, 4):
        _add_staff_capacity(model, stage_intervals[stage], stage_staff_limits.get(stage, 1), f"staff_s{stage}")

    cycle_over_penalty_factor = 1000
    delay_term = sum(int(w.get(i, 1)) * E[i] for i in I)
    cycle_over_term = sum(int(w.get(i, 1)) * batch_cycle_over[i, b] for i in I for b in B[i])
    model.Minimize(delay_term + cycle_over_penalty_factor * cycle_over_term)

    ctx = {
        "model": model,
        "vars": {
            "t1": t1, "t2": t2, "t3": t3, "t4": t4,
            "e1": e1, "e2": e2, "e3": e3,
            "x1": x1, "x2": x2, "x3": x3, "x4": x4,
            "batch_finish": batch_finish,
            "batch_cycle": batch_cycle,
            "batch_cycle_over": batch_cycle_over,
            "E": E,
        },
        "scale": scale,
        "horizon": horizon,
    }
    return ctx


def _make_solver(max_time, workers, seed=None, log=True):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(max_time)
    solver.parameters.num_search_workers = int(workers)
    solver.parameters.log_search_progress = bool(log)
    if seed is not None:
        solver.parameters.random_seed = int(seed)
    return solver


class _StopSearchCallback(cp_model.CpSolverSolutionCallback):
    def __init__(self, stop_checker=None):
        super().__init__()
        self.stop_checker = stop_checker

    def OnSolutionCallback(self):
        if self.stop_checker is not None and self.stop_checker():
            self.StopSearch()


def _add_solution_hints(model, vars_dict, solver):
    for key in ("x1", "x2", "x3", "x4"):
        for var in vars_dict[key].values():
            model.AddHint(var, solver.Value(var))
    for key in ("t1", "t2", "t3", "t4", "batch_finish", "batch_cycle"):
        for var in vars_dict[key].values():
            model.AddHint(var, solver.Value(var))


def _extract_solution_snapshot(solver, vars_dict):
    snap = {}
    for key, mapping in vars_dict.items():
        snap[key] = {k: solver.Value(v) for k, v in mapping.items()}
    return snap


def _spec_criticality(snapshot, I, B, w, d, T, scale):
    scores = {}
    for i in I:
        delay = int(snapshot["E"].get(i, 0)) / max(1, scale)
        overtime = sum(int(snapshot["batch_cycle_over"].get((i, b), 0)) for b in B[i]) / max(1, scale)
        pack_start = int(snapshot["t4"].get(i, 0)) / max(1, scale)
        due = float(d.get(i, 0))
        weight = float(w.get(i, 1))
        cycle_limit = max(float(T.get(i, 0) or 0), 1.0)
        slack = max(0.0, due - pack_start)
        scores[i] = weight * (12.0 * overtime + 3.0 * delay + 0.05 * pack_start + 0.1 * len(B[i])) / cycle_limit - 0.01 * slack
    return scores


def _select_destroy_specs(snapshot, I, J4, B, w, d, T, scale, rng, destroy_size, mode):
    candidates = list(I)
    if not candidates:
        return set()

    destroy_size = max(1, min(len(candidates), destroy_size))
    scores = _spec_criticality(snapshot, I, B, w, d, T, scale)

    if mode == "critical":
        ordered = sorted(candidates, key=lambda i: (-scores.get(i, 0), -w.get(i, 1), -len(B[i]), str(i)))
        seed_specs = ordered[:max(1, destroy_size // 2)]
        return _expand_destroy_set(snapshot, seed_specs, I, J4, destroy_size)

    if mode == "cluster":
        machine_groups = {}
        for i in candidates:
            line = next((j for j in J4 if snapshot["x4"].get((i, j), 0) > 0), None)
            if line is not None:
                machine_groups.setdefault(line, []).append(i)
        if machine_groups:
            hot_line = max(
                machine_groups,
                key=lambda j: (sum(scores.get(i, 0) for i in machine_groups[j]), len(machine_groups[j]), str(j))
            )
            ordered = sorted(
                machine_groups[hot_line],
                key=lambda i: (-scores.get(i, 0), snapshot["t4"].get(i, 0), -len(B[i]), str(i))
            )
            seed_specs = ordered[:max(1, destroy_size // 2)]
            chosen = list(_expand_destroy_set(snapshot, seed_specs, I, J4, destroy_size))
            if len(chosen) < destroy_size:
                rest = [i for i in candidates if i not in chosen]
                rest.sort(key=lambda i: (-scores.get(i, 0), -w.get(i, 1), str(i)))
                chosen.extend(rest[:destroy_size - len(chosen)])
            return set(chosen[:destroy_size])

    if mode == "weight":
        ordered = sorted(candidates, key=lambda i: (-w.get(i, 1), -scores.get(i, 0), -len(B[i]), str(i)))
        top_pool = ordered[:max(destroy_size * 2, destroy_size)]
        rng.shuffle(top_pool)
        return set(top_pool[:destroy_size])

    if mode == "batch_count":
        ordered = sorted(candidates, key=lambda i: (-len(B[i]), -scores.get(i, 0), -w.get(i, 1), str(i)))
        top_pool = ordered[:max(destroy_size * 2, destroy_size)]
        rng.shuffle(top_pool)
        return set(top_pool[:destroy_size])

    if mode == "top_mix":
        ordered = sorted(candidates, key=lambda i: (-scores.get(i, 0), -w.get(i, 1), -len(B[i]), str(i)))
        top_pool = ordered[:max(destroy_size * 3, destroy_size)]
        rng.shuffle(top_pool)
        return set(top_pool[:destroy_size])

    return set(rng.sample(candidates, destroy_size))


def _freeze_snapshot_except_specs(model, vars_dict, snapshot, open_specs):
    def keep_item(key):
        if isinstance(key, tuple) and key:
            return key[0] not in open_specs
        return key not in open_specs

    for key in ("x1", "x2", "x3", "x4", "t1", "t2", "t3", "t4"):
        for idx, var in vars_dict[key].items():
            if keep_item(idx) and idx in snapshot.get(key, {}):
                model.Add(var == snapshot[key][idx])


def _add_snapshot_hints(model, vars_dict, snapshot):
    for key, mapping in vars_dict.items():
        if key not in snapshot:
            continue
        for idx, var in mapping.items():
            if idx in snapshot[key]:
                model.AddHint(var, snapshot[key][idx])


class _SnapshotValueSolver:
    def __init__(self, name_map):
        self._name_map = name_map

    def Value(self, var):
        return self._name_map[var.Name()]


def _snapshot_to_name_map(vars_dict, snapshot):
    name_map = {}
    for key, mapping in vars_dict.items():
        if key not in snapshot:
            continue
        for idx, var in mapping.items():
            if idx in snapshot[key]:
                name_map[var.Name()] = snapshot[key][idx]
    return name_map


def _snapshot_objective(snapshot, I, B, w, cycle_over_penalty_factor):
    delay_term = sum(int(w.get(i, 1)) * int(snapshot["E"].get(i, 0)) for i in I)
    cycle_over_term = sum(
        int(w.get(i, 1)) * int(snapshot["batch_cycle_over"].get((i, b), 0))
        for i in I for b in B[i]
    )
    return delay_term + cycle_over_penalty_factor * cycle_over_term


def _expand_destroy_set(snapshot, seed_specs, I, J4, destroy_size):
    chosen = list(seed_specs)
    chosen_set = set(chosen)
    if len(chosen) >= destroy_size:
        return set(chosen[:destroy_size])

    pack_lines = {
        next((j for j in J4 if snapshot["x4"].get((i, j), 0) > 0), None)
        for i in chosen
    }
    pack_lines.discard(None)

    if not pack_lines:
        return set(chosen[:destroy_size])

    related = []
    for i in I:
        if i in chosen_set:
            continue
        line = next((j for j in J4 if snapshot["x4"].get((i, j), 0) > 0), None)
        if line in pack_lines:
            related.append(i)

    related.sort(key=lambda i: (snapshot["t4"].get(i, 0), str(i)))
    for i in related:
        if len(chosen) >= destroy_size:
            break
        chosen.append(i)
    return set(chosen[:destroy_size])


def _earliest_with_capacity(start, duration, intervals, capacity):
    if capacity <= 0:
        return None
    t = int(start)
    duration = int(duration)
    if duration <= 0:
        return t
    normalized = [(int(s), int(e)) for s, e in intervals if int(e) > int(s)]
    while True:
        end = t + duration
        points = {t, end}
        for s, e in normalized:
            if t < e and end > s:
                points.add(max(t, s))
                points.add(min(end, e))

        conflict_end = None
        ordered = sorted(points)
        for a, b in zip(ordered, ordered[1:]):
            if a >= b:
                continue
            active = sum(1 for s, e in normalized if s <= a < e)
            if active >= capacity:
                conflict_end = b
                break

        if conflict_end is None:
            return t
        t = max(t + 1, int(conflict_end))


def _machine_setup_time(clear_scaled, stage, prev_item, item):
    if prev_item is None:
        return 0
    return int(clear_scaled.get(stage, {}).get((prev_item, item), 0))


def _choose_machine_for_batch(
    item, machines, proc, machine_state, staff_intervals, staff_capacity,
    release, clear_scaled, stage, max_run=None, periodic_clear=0
):
    best = None
    for j in machines:
        duration = int(proc[item, j])
        state = machine_state[stage].get(j, {"end": 0, "item": None, "run": 0})
        last_end = int(state.get("end", 0))
        last_item = state.get("item")
        run_used = int(state.get("run", 0))
        setup = _machine_setup_time(clear_scaled, stage, last_item, item)
        min_gap = setup
        if stage in (1, 2, 3) and max_run is not None and last_item is not None and setup == 0:
            if run_used + duration > int(max_run):
                min_gap = max(min_gap, int(periodic_clear))

        start_lb = max(int(release), last_end + min_gap)
        start = _earliest_with_capacity(start_lb, duration, staff_intervals[stage], staff_capacity.get(stage, 1))
        if start is None:
            continue
        actual_gap = max(0, int(start) - last_end)
        if stage in (1, 2, 3):
            if last_item is None or setup > 0:
                next_run = duration
            elif max_run is not None and periodic_clear > 0 and actual_gap >= int(periodic_clear):
                next_run = duration
            else:
                next_run = run_used + duration
        else:
            next_run = 0

        cand = (start + duration, start, j, duration, next_run)
        if best is None or cand < best:
            best = cand
    return best


def _weighted_choice(weights, rng):
    items = list(weights.items())
    total = sum(max(float(weight), 1e-6) for _, weight in items)
    draw = rng.random() * total
    acc = 0.0
    for key, weight in items:
        acc += max(float(weight), 1e-6)
        if draw <= acc:
            return key
    return items[-1][0]


def _pack_ready_time(snapshot, i, b):
    if (i, b) in snapshot["e3"]:
        return int(snapshot["e3"][i, b])
    return int(snapshot["e2"][i, b])


def _pack_line_tasks(snapshot, I, J4, B, p4, scale):
    p4i = {(i, j): _to_int(v, scale) for (i, j), v in p4.items()}
    line_tasks = {j: [] for j in J4}
    for i in I:
        line = next((j for j in J4 if snapshot["x4"].get((i, j), 0) > 0), None)
        if line is None or (i, line) not in p4i:
            continue
        start = int(snapshot["t4"].get(i, 0))
        duration = len(B[i]) * p4i[i, line]
        line_tasks[line].append({
            "item": i,
            "line": line,
            "start": start,
            "end": start + duration,
            "duration": duration,
            "per_batch": p4i[i, line],
        })
    for tasks in line_tasks.values():
        tasks.sort(key=lambda x: (x["start"], x["end"], str(x["item"])))
    return line_tasks


def _apply_pack_sequence(snapshot, sequence, line, B, p4, d, T, w, scale, clear_time_matrices, machine_available_time):
    p4i = {(i, j): _to_int(v, scale) for (i, j), v in p4.items()}
    di = {i: _to_int(d.get(i, 62), scale) for i in B}
    Ti = {i: _to_int(T.get(i, 0), scale) for i in B}
    setup_i = {
        (from_i, to_i): _to_int(v, scale)
        for (from_i, to_i), v in (clear_time_matrices or {}).get(4, {}).items()
    }
    v_line = _to_int((machine_available_time or {}).get(line, 0), scale)

    new_snapshot = {
        key: dict(value)
        for key, value in snapshot.items()
    }
    current = v_line
    previous = None

    for i in sequence:
        if (i, line) not in p4i:
            return None
        per_batch = p4i[i, line]
        duration = len(B[i]) * per_batch
        setup = setup_i.get((previous, i), 0) if previous is not None else 0
        start_lb = current + setup
        for idx, b in enumerate(B[i]):
            start_lb = max(start_lb, _pack_ready_time(new_snapshot, i, b) - idx * per_batch)
        start = start_lb
        finish = start + duration

        new_snapshot["t4"][i] = start
        for (ii, jj) in list(new_snapshot["x4"].keys()):
            if ii == i:
                new_snapshot["x4"][ii, jj] = 1 if jj == line else 0

        for idx, b in enumerate(B[i]):
            batch_finish = start + (idx + 1) * per_batch
            new_snapshot["batch_finish"][i, b] = batch_finish
            new_snapshot["batch_cycle"][i, b] = batch_finish - int(new_snapshot["t1"][i, b])
            new_snapshot["batch_cycle_over"][i, b] = max(0, new_snapshot["batch_cycle"][i, b] - Ti[i])

        new_snapshot["E"][i] = max(0, finish - di[i])
        current = finish
        previous = i

    return new_snapshot


def _pack_staff_intervals(snapshot, I, J4, B, p4, scale, exclude_item=None):
    p4i = {(i, j): _to_int(v, scale) for (i, j), v in p4.items()}
    intervals = []
    for i in I:
        if i == exclude_item:
            continue
        line = next((j for j in J4 if snapshot["x4"].get((i, j), 0) > 0), None)
        if line is None or (i, line) not in p4i:
            continue
        start = int(snapshot["t4"].get(i, 0))
        intervals.append((start, start + len(B[i]) * p4i[i, line]))
    return intervals


def _has_capacity_for_interval(start, duration, intervals, capacity):
    if capacity <= 0:
        return False
    start = int(start)
    end = start + int(duration)
    if end <= start:
        return True

    points = {start, end}
    for s, e in intervals:
        if start < e and end > s:
            points.add(max(start, int(s)))
            points.add(min(end, int(e)))

    ordered = sorted(points)
    for a, b in zip(ordered, ordered[1:]):
        if a >= b:
            continue
        active = 1 + sum(1 for s, e in intervals if s <= a and a < e)
        if active > capacity:
            return False
    return True


def _earliest_pack_start_with_constraints(
    start_lb, duration, item, line_tasks, staff_intervals, staff_capacity, setup_i
):
    start_lb = int(start_lb)
    duration = int(duration)
    t = start_lb
    line_tasks = sorted(line_tasks, key=lambda x: (x["start"], x["end"], str(x["item"])))

    while True:
        moved = False
        for task in line_tasks:
            setup_before = int(setup_i.get((task["item"], item), 0))
            setup_after = int(setup_i.get((item, task["item"]), 0))
            task_start = int(task["start"])
            task_end = int(task["end"])

            if t >= task_end:
                if t < task_end + setup_before:
                    t = task_end + setup_before
                    moved = True
                    break
                continue

            if t + duration + setup_after <= task_start:
                break

            t = task_end + setup_before
            moved = True
            break

        if moved:
            continue

        staff_start = _earliest_with_capacity(t, duration, staff_intervals, staff_capacity)
        if staff_start is None:
            return None
        if staff_start == t and _has_capacity_for_interval(t, duration, staff_intervals, staff_capacity):
            return t
        t = staff_start


def _left_shift_pack_snapshot(
    snapshot, I, J4, B, p4, d, T, w, scale, clear_time_matrices,
    machine_available_time, stage_staff_limits, max_passes=3
):
    p4i = {(i, j): _to_int(v, scale) for (i, j), v in p4.items()}
    di = {i: _to_int(d.get(i, 62), scale) for i in I}
    Ti = {i: _to_int(T.get(i, 0), scale) for i in I}
    setup_i = {
        (from_i, to_i): _to_int(v, scale)
        for (from_i, to_i), v in (clear_time_matrices or {}).get(4, {}).items()
    }
    vj = {j: _to_int((machine_available_time or {}).get(j, 0), scale) for j in J4}
    staff_capacity = int((stage_staff_limits or {}).get(4, max(1, len(J4))))

    new_snapshot = {key: dict(value) for key, value in snapshot.items()}
    current_obj = _snapshot_objective(new_snapshot, I, B, w, cycle_over_penalty_factor=1000)
    moved_specs = []

    for _ in range(max_passes):
        moved_in_pass = False
        line_tasks = _pack_line_tasks(new_snapshot, I, J4, B, p4, scale)
        items = []
        for tasks in line_tasks.values():
            items.extend(task["item"] for task in tasks)
        items.sort(key=lambda i: (-int(w.get(i, 1)), int(new_snapshot["t4"].get(i, 0)), -len(B[i]), str(i)))

        for i in items:
            line = next((j for j in J4 if new_snapshot["x4"].get((i, j), 0) > 0), None)
            if line is None or (i, line) not in p4i:
                continue

            per_batch = p4i[i, line]
            duration = len(B[i]) * per_batch
            start_lb = vj.get(line, 0)
            for idx, b in enumerate(B[i]):
                start_lb = max(start_lb, _pack_ready_time(new_snapshot, i, b) - idx * per_batch)

            current_start = int(new_snapshot["t4"].get(i, 0))
            if start_lb >= current_start:
                continue

            other_line_tasks = [task for task in line_tasks[line] if task["item"] != i]
            staff_intervals = _pack_staff_intervals(new_snapshot, I, J4, B, p4, scale, exclude_item=i)
            new_start = _earliest_pack_start_with_constraints(
                start_lb, duration, i, other_line_tasks, staff_intervals, staff_capacity, setup_i
            )
            if new_start is None or new_start >= current_start:
                continue

            candidate = {key: dict(value) for key, value in new_snapshot.items()}
            candidate["t4"][i] = new_start
            finish = new_start + duration
            for idx, b in enumerate(B[i]):
                batch_finish = new_start + (idx + 1) * per_batch
                candidate["batch_finish"][i, b] = batch_finish
                candidate["batch_cycle"][i, b] = batch_finish - int(candidate["t1"][i, b])
                candidate["batch_cycle_over"][i, b] = max(0, candidate["batch_cycle"][i, b] - Ti[i])
            candidate["E"][i] = max(0, finish - di[i])

            cand_obj = _snapshot_objective(candidate, I, B, w, cycle_over_penalty_factor=1000)
            if cand_obj < current_obj:
                new_snapshot = candidate
                current_obj = cand_obj
                moved_in_pass = True
                moved_specs.append((i, current_start, new_start))
                line_tasks = _pack_line_tasks(new_snapshot, I, J4, B, p4, scale)

        if not moved_in_pass:
            break

    return new_snapshot, current_obj, moved_specs


def _rebuild_pack_bottleneck_snapshot(
    snapshot, I, J4, B, p4, d, T, w, scale, clear_time_matrices, machine_available_time, rng
):
    line_tasks = _pack_line_tasks(snapshot, I, J4, B, p4, scale)
    scores = _spec_criticality(snapshot, I, B, w, d, T, scale)
    candidate_lines = {
        line: sum(max(scores.get(task["item"], 0), 0.0) for task in tasks) + 0.001 * sum(task["duration"] for task in tasks)
        for line, tasks in line_tasks.items()
        if len(tasks) >= 2
    }
    if not candidate_lines:
        return None, None, None, None

    line = max(candidate_lines, key=lambda j: (candidate_lines[j], len(line_tasks[j]), str(j)))
    tasks = line_tasks[line]
    items = [task["item"] for task in tasks]
    if len(items) <= 1:
        return None, None, None, None

    n = len(items)
    window_candidates = [(0, n, list(items))]
    if n > 4:
        window_sizes = sorted(set([3, 4, min(5, n), min(6, n)]))
        for win in window_sizes:
            if win > n:
                continue
            best_start = 0
            best_window_score = None
            for start_idx in range(0, n - win + 1):
                window_items = items[start_idx:start_idx + win]
                score = sum(scores.get(i, 0) for i in window_items)
                if best_window_score is None or score > best_window_score:
                    best_window_score = score
                    best_start = start_idx
            window_candidates.append((best_start, win, items[best_start:best_start + win]))

    best_snapshot = None
    best_obj = None
    best_label = None
    best_open_specs = None
    for window_idx, (start_idx, win, window_items) in enumerate(window_candidates):
        prefix = items[:start_idx]
        suffix = items[start_idx + win:]
        variants = []
        variants.append(sorted(window_items, key=lambda i: (-scores.get(i, 0), snapshot["t4"].get(i, 0), str(i))))
        variants.append(sorted(window_items, key=lambda i: (d.get(i, 62), -w.get(i, 1), snapshot["t4"].get(i, 0), str(i))))
        variants.append(sorted(window_items, key=lambda i: (-len(B[i]), -scores.get(i, 0), str(i))))
        shuffled = list(window_items)
        rng.shuffle(shuffled)
        variants.append(shuffled)

        for idx, local_seq in enumerate(variants):
            sequence = prefix + local_seq + suffix
            cand_snapshot = _apply_pack_sequence(
                snapshot, sequence, line, B, p4, d, T, w, scale, clear_time_matrices, machine_available_time
            )
            if cand_snapshot is None:
                continue
            cand_obj = _snapshot_objective(cand_snapshot, I, B, w, cycle_over_penalty_factor=1000)
            if best_obj is None or cand_obj < best_obj:
                best_snapshot = cand_snapshot
                best_obj = cand_obj
                best_label = f"{line}/window{window_idx}_start{start_idx}_size{win}_variant{idx}"
                best_open_specs = set(window_items)

    return best_snapshot, best_obj, best_label, best_open_specs


def _build_greedy_snapshot(
    I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
    stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
    max_continuous_run=None, order_mode="priority", rng=None, periodic_cleaning_time=1.0,
):
    p1i = {(i, j): _to_int(v, scale) for (i, j), v in p1.items()}
    p2i = {(i, j): _to_int(v, scale) for (i, j), v in p2.items()}
    p3i = {(i, j): _to_int(v, scale) for (i, j), v in p3.items()}
    p4i = {(i, j): _to_int(v, scale) for (i, j), v in p4.items()}
    di = {i: _to_int(d.get(i, 62), scale) for i in I}
    Ti = {i: _to_int(T.get(i, 0), scale) for i in I}
    vj = {j: _to_int((machine_available_time or {}).get(j, 0), scale) for j in J1 + J2 + J3 + J4}
    rb = {(i, b): _to_int((release_time or {}).get((i, b), 0), scale) for i in I for b in B[i]}
    clear_scaled = {
        stage: {(a, b): _to_int(v, scale) for (a, b), v in (clear_time_matrices or {}).get(stage, {}).items()}
        for stage in (1, 2, 3, 4)
    }
    staff_capacity = stage_staff_limits or {1: len(J1), 2: len(J2), 3: len(J3), 4: len(J4)}
    max_run = _to_int(max_continuous_run, scale) if max_continuous_run is not None else None
    periodic_clear = _to_int(periodic_cleaning_time, scale)

    snapshot = {
        "t1": {}, "t2": {}, "t3": {}, "t4": {},
        "e1": {}, "e2": {}, "e3": {},
        "x1": {}, "x2": {}, "x3": {}, "x4": {},
        "batch_finish": {}, "batch_cycle": {}, "batch_cycle_over": {},
        "E": {},
    }
    machine_state = {
        1: {j: {"end": vj[j], "item": None, "run": 0} for j in J1},
        2: {j: {"end": vj[j], "item": None, "run": 0} for j in J2},
        3: {j: {"end": vj[j], "item": None, "run": 0} for j in J3},
        4: {j: {"end": vj[j], "item": None, "run": 0} for j in J4},
    }
    staff_intervals = {1: [], 2: [], 3: [], 4: []}

    def _order_key(i):
        valid_j1 = [j for j in J1 if (i, j) in p1i]
        valid_j2 = [j for j in J2 if (i, j) in p2i]
        valid_j3 = [j for j in J3 if (i, j) in p3i]
        valid_j4 = [j for j in J4 if (i, j) in p4i]
        mix_min = min((p1i[i, j] for j in valid_j1), default=10**9)
        tab_min = min((p2i[i, j] for j in valid_j2), default=10**9)
        coat_min = min((p3i[i, j] for j in valid_j3), default=0)
        pack_min = min((p4i[i, j] for j in valid_j4), default=10**9)
        full_chain = mix_min + tab_min + coat_min + len(B[i]) * pack_min
        pack_load = len(B[i]) * pack_min
        cycle_limit = max(Ti[i], 1)
        risk = float(w.get(i, 1)) * full_chain / cycle_limit

        if order_mode == "due":
            return (di[i], -w.get(i, 1), pack_load, -len(B[i]), str(i))
        if order_mode == "pack_heavy":
            return (-pack_load, di[i], -w.get(i, 1), full_chain, str(i))
        if order_mode == "risk":
            return (-risk, -pack_load, di[i], -len(B[i]), str(i))
        if order_mode == "random":
            noise = rng.random() if rng is not None else 0.0
            return (noise, -w.get(i, 1), di[i], -len(B[i]), str(i))
        return (-w.get(i, 1), di[i], -len(B[i]), full_chain, str(i))

    order = sorted(I, key=_order_key)
    for i in order:
        valid_j1 = [j for j in J1 if (i, j) in p1i]
        valid_j2 = [j for j in J2 if (i, j) in p2i]
        valid_j3 = [j for j in J3 if (i, j) in p3i]
        valid_j4 = [j for j in J4 if (i, j) in p4i]
        needs_coating = bool(valid_j3)

        if not valid_j1:
            raise ValueError(f"规格 {i} 在配料工序没有可用设备。")
        if not valid_j2:
            raise ValueError(f"规格 {i} 在压片工序没有可用设备。")
        if needs_coating and not valid_j3:
            raise ValueError(f"规格 {i} 在包衣工序没有可用设备。")
        if not valid_j4:
            raise ValueError(f"规格 {i} 在包装工序没有可用设备。")

        batch_ready_for_pack = {}
        for b in B[i]:
            release = rb[i, b]
            choice = _choose_machine_for_batch(
                i, valid_j1, p1i, machine_state, staff_intervals, staff_capacity,
                release, clear_scaled, 1, max_run=max_run, periodic_clear=periodic_clear
            )
            if choice is None:
                raise ValueError(f"规格 {i} 批次 {b} 在配料工序无法构造启发式初解。")
            end, start, j, duration, next_run = choice
            snapshot["t1"][i, b], snapshot["e1"][i, b] = start, end
            for jj in valid_j1:
                snapshot["x1"][i, b, jj] = 1 if jj == j else 0
            machine_state[1][j] = {"end": end, "item": i, "run": next_run}
            staff_intervals[1].append((start, end))

            release = end
            choice = _choose_machine_for_batch(
                i, valid_j2, p2i, machine_state, staff_intervals, staff_capacity,
                release, clear_scaled, 2, max_run=max_run, periodic_clear=periodic_clear
            )
            if choice is None:
                raise ValueError(f"规格 {i} 批次 {b} 在压片工序无法构造启发式初解。")
            end, start, j, duration, next_run = choice
            snapshot["t2"][i, b], snapshot["e2"][i, b] = start, end
            for jj in valid_j2:
                snapshot["x2"][i, b, jj] = 1 if jj == j else 0
            machine_state[2][j] = {"end": end, "item": i, "run": next_run}
            staff_intervals[2].append((start, end))

            if needs_coating:
                release = end
                choice = _choose_machine_for_batch(
                    i, valid_j3, p3i, machine_state, staff_intervals, staff_capacity,
                    release, clear_scaled, 3, max_run=max_run, periodic_clear=periodic_clear
                )
                if choice is None:
                    raise ValueError(f"规格 {i} 批次 {b} 在包衣工序无法构造启发式初解。")
                end, start, j, duration, next_run = choice
                snapshot["t3"][i, b], snapshot["e3"][i, b] = start, end
                for jj in valid_j3:
                    snapshot["x3"][i, b, jj] = 1 if jj == j else 0
                machine_state[3][j] = {"end": end, "item": i, "run": next_run}
                staff_intervals[3].append((start, end))
            batch_ready_for_pack[b] = end

        best_pack = None
        for j in valid_j4:
            per_batch = int(p4i[i, j])
            duration = len(B[i]) * per_batch
            state = machine_state[4].get(j, {"end": 0, "item": None, "run": 0})
            last_end = int(state.get("end", 0))
            last_item = state.get("item")
            setup = _machine_setup_time(clear_scaled, 4, last_item, i)
            start_lb = max(vj[j], last_end + setup)
            for idx, b in enumerate(B[i]):
                start_lb = max(start_lb, batch_ready_for_pack[b] - idx * per_batch)
            start = _earliest_with_capacity(start_lb, duration, staff_intervals[4], staff_capacity.get(4, 1))
            if start is None:
                continue
            cand = (start + duration, start, j, per_batch, duration)
            if best_pack is None or cand < best_pack:
                best_pack = cand

        if best_pack is None:
            raise ValueError(f"规格 {i} 在包装工序无法构造启发式初解。")
        finish, start, j, per_batch, duration = best_pack
        snapshot["t4"][i] = start
        for jj in valid_j4:
            snapshot["x4"][i, jj] = 1 if jj == j else 0
        machine_state[4][j] = {"end": finish, "item": i, "run": 0}
        staff_intervals[4].append((start, finish))

        for idx, b in enumerate(B[i]):
            batch_finish = start + (idx + 1) * per_batch
            snapshot["batch_finish"][i, b] = batch_finish
            snapshot["batch_cycle"][i, b] = batch_finish - snapshot["t1"][i, b]
            snapshot["batch_cycle_over"][i, b] = max(0, snapshot["batch_cycle"][i, b] - Ti[i])
        snapshot["E"][i] = max(0, finish - di[i])

    return snapshot


def _repair_neighborhood(
    I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
    stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
    max_continuous_run, base_snapshot, open_specs, time_limit, seed, workers,
    periodic_cleaning_time=1.0,
    stop_checker=None,
):
    if stop_checker is not None and stop_checker():
        return cp_model.UNKNOWN, None, None
    ctx = _build_model(
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
        stage_staff_limits=stage_staff_limits,
        clear_time_matrices=clear_time_matrices,
        machine_available_time=machine_available_time,
        release_time=release_time,
        max_continuous_run=max_continuous_run,
        periodic_cleaning_time=periodic_cleaning_time,
    )
    model, vars_dict = ctx["model"], ctx["vars"]
    _add_snapshot_hints(model, vars_dict, base_snapshot)
    _freeze_snapshot_except_specs(model, vars_dict, base_snapshot, open_specs)

    solver = _make_solver(time_limit, workers, seed=seed, log=False)
    status = solver.Solve(model, _StopSearchCallback(stop_checker))
    return status, solver, ctx


def _run_alns(
    I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
    stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
    max_continuous_run, initial_snapshot, initial_obj, total_time_sec, workers, rng_seed=17,
    progress_logger=None,
    periodic_cleaning_time=1.0,
    stop_checker=None,
):
    if total_time_sec <= 0:
        return initial_snapshot, None, None, initial_obj

    rng = random.Random(rng_seed)
    best_snapshot = initial_snapshot
    best_obj = initial_obj
    current_snapshot = best_snapshot
    current_obj = best_obj
    best_solver = None
    best_ctx = None

    start = time.time()
    iteration = 0
    destroy_modes = ("random", "weight", "critical", "cluster", "batch_count", "top_mix")
    destroy_mode_weights = {mode: 1.0 for mode in destroy_modes}
    destroy_sizes = sorted(set([
        1,
        max(2, len(I) // 10),
        max(2, len(I) // 5),
        max(3, len(I) // 3),
        max(4, len(I) // 2),
    ]))
    destroy_size_weights = {size: 1.0 for size in destroy_sizes}
    reaction = 0.20
    status_ok = (cp_model.OPTIMAL, cp_model.FEASIBLE)

    while time.time() - start < total_time_sec:
        if stop_checker is not None and stop_checker():
            break
        if progress_logger is not None:
            progress_logger.maybe_emit_periodic()
        remaining = total_time_sec - (time.time() - start)
        if remaining <= 5:
            break

        iteration += 1
        linked_open_specs = set()
        pack_snapshot, pack_obj, pack_label, pack_open_specs = _rebuild_pack_bottleneck_snapshot(
            current_snapshot, I, J4, B, p4, d, T, w, scale,
            clear_time_matrices, machine_available_time, rng
        )
        if stop_checker is not None and stop_checker():
            break
        if pack_snapshot is not None and pack_obj < current_obj:
            current_snapshot = pack_snapshot
            current_obj = pack_obj
            linked_open_specs = set(pack_open_specs or set())
            if pack_obj < best_obj:
                best_snapshot = pack_snapshot
                best_obj = pack_obj
                best_solver = None
                best_ctx = None
                if progress_logger is not None:
                    progress_logger.on_better_solution_found()
                print(f"  ALNS iter={iteration}, pack_rebuild={pack_label}, improved obj={pack_obj}")

        mode = _weighted_choice(destroy_mode_weights, rng)
        destroy_size = min(len(I), _weighted_choice(destroy_size_weights, rng))
        open_specs = _select_destroy_specs(
            current_snapshot, I, J4, B, w, d, T, scale, rng, destroy_size, mode
        )
        if linked_open_specs:
            open_specs = set(open_specs) | linked_open_specs
        repair_time = min(30, max(8, int(remaining * 0.3)))
        seed = rng.randint(1, 10**6)

        if stop_checker is not None and stop_checker():
            break
        status, solver, ctx = _repair_neighborhood(
            I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
            stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
            max_continuous_run, current_snapshot, open_specs, repair_time, seed, workers,
            periodic_cleaning_time=periodic_cleaning_time,
            stop_checker=stop_checker,
        )
        if stop_checker is not None and stop_checker():
            break
        if status not in status_ok:
            destroy_mode_weights[mode] = (1.0 - reaction) * destroy_mode_weights[mode] + reaction * 0.2
            destroy_size_weights[destroy_size] = (1.0 - reaction) * destroy_size_weights[destroy_size] + reaction * 0.2
            continue

        cand_obj = solver.ObjectiveValue()
        cand_snapshot = _extract_solution_snapshot(solver, ctx["vars"])
        delta = cand_obj - current_obj
        temperature = max(1.0, 0.001 * max(1.0, float(best_obj)))
        accepted = False
        reward = 0.5
        if cand_obj < best_obj:
            best_obj = cand_obj
            best_snapshot = cand_snapshot
            best_solver = solver
            best_ctx = ctx
            current_snapshot = cand_snapshot
            current_obj = cand_obj
            accepted = True
            reward = 6.0
            if progress_logger is not None:
                progress_logger.on_better_solution_found()
            print(
                f"  ALNS iter={iteration}, mode={mode}, open_specs={len(open_specs)}, "
                f"improved obj={cand_obj}"
            )
        elif cand_obj <= current_obj:
            prev_current_obj = current_obj
            current_snapshot = cand_snapshot
            current_obj = cand_obj
            accepted = True
            reward = 2.0 if cand_obj < prev_current_obj else 1.0
        else:
            if delta <= 0.02 * max(1.0, float(current_obj)):
                current_snapshot = cand_snapshot
                current_obj = cand_obj
                accepted = True
                reward = 0.8
            else:
                import math
                if rng.random() < math.exp(-max(0.0, float(delta)) / temperature):
                    current_snapshot = cand_snapshot
                    current_obj = cand_obj
                    accepted = True
                    reward = 0.4

        destroy_mode_weights[mode] = (1.0 - reaction) * destroy_mode_weights[mode] + reaction * reward
        destroy_size_weights[destroy_size] = (1.0 - reaction) * destroy_size_weights[destroy_size] + reaction * reward
        if not accepted:
            continue

    if stop_checker is not None and stop_checker():
        return best_snapshot, best_solver, best_ctx, best_obj

    refined_snapshot, refined_obj, moved_specs = _left_shift_pack_snapshot(
        best_snapshot, I, J4, B, p4, d, T, w, scale,
        clear_time_matrices, machine_available_time, stage_staff_limits,
    )
    if refined_obj < best_obj:
        best_snapshot = refined_snapshot
        best_obj = refined_obj
        best_solver = None
        best_ctx = None
        if progress_logger is not None:
            progress_logger.on_better_solution_found()
        if moved_specs:
            print(
                "  ALNS pack_left_shift=" +
                ",".join(
                    f"{str(spec)}:{round(old / max(1, scale), 2)}->{round(new / max(1, scale), 2)}"
                    for spec, old, new in moved_specs[:6]
                )
            )

    return best_snapshot, best_solver, best_ctx, best_obj


def _load_snapshot_from_excel(
    excel_file, I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale
):
    path = Path(excel_file)
    batch_df = pd.read_excel(path, sheet_name="批次工序计划")
    pack_df = pd.read_excel(path, sheet_name="包装及摘要")

    p1i = {(i, j): _to_int(v, scale) for (i, j), v in p1.items()}
    p2i = {(i, j): _to_int(v, scale) for (i, j), v in p2.items()}
    p3i = {(i, j): _to_int(v, scale) for (i, j), v in p3.items()}
    p4i = {(i, j): _to_int(v, scale) for (i, j), v in p4.items()}
    di = {i: _to_int(d.get(i, 62), scale) for i in I}
    Ti = {i: _to_int(T.get(i, 0), scale) for i in I}

    snapshot = {
        "t1": {}, "t2": {}, "t3": {}, "t4": {},
        "e1": {}, "e2": {}, "e3": {},
        "x1": {}, "x2": {}, "x3": {}, "x4": {},
        "batch_finish": {}, "batch_cycle": {}, "batch_cycle_over": {},
        "E": {},
    }

    for i in I:
        for b in B[i]:
            row = batch_df[(batch_df["药品规格"] == i) & (batch_df["批次号"].astype(int) == int(b))]
            if row.empty:
                raise ValueError(f"Excel初始解缺少规格 {i} 批次 {b} 的批次工序记录。")
            row = row.iloc[0]

            mix_device = row["配料设备"]
            tab_device = row["压片设备"]
            if (i, mix_device) not in p1i:
                raise ValueError(f"Excel初始解中 {i} 批次 {b} 的配料设备 {mix_device} 不可用。")
            if (i, tab_device) not in p2i:
                raise ValueError(f"Excel初始解中 {i} 批次 {b} 的压片设备 {tab_device} 不可用。")

            snapshot["t1"][i, b] = _to_int(row["配料开工(班时)"], scale)
            snapshot["e1"][i, b] = snapshot["t1"][i, b] + p1i[i, mix_device]
            snapshot["t2"][i, b] = _to_int(row["压片开工(班时)"], scale)
            snapshot["e2"][i, b] = snapshot["t2"][i, b] + p2i[i, tab_device]

            for j in J1:
                if (i, j) in p1i:
                    snapshot["x1"][i, b, j] = 1 if j == mix_device else 0
            for j in J2:
                if (i, j) in p2i:
                    snapshot["x2"][i, b, j] = 1 if j == tab_device else 0

            needs_coating = any((i, j) in p3i for j in J3)
            if needs_coating:
                coat_device = row["包衣设备"]
                if (i, coat_device) not in p3i:
                    raise ValueError(f"Excel初始解中 {i} 批次 {b} 的包衣设备 {coat_device} 不可用。")
                snapshot["t3"][i, b] = _to_int(row["包衣开工(班时)"], scale)
                snapshot["e3"][i, b] = snapshot["t3"][i, b] + p3i[i, coat_device]
                for j in J3:
                    if (i, j) in p3i:
                        snapshot["x3"][i, b, j] = 1 if j == coat_device else 0

    for i in I:
        row = pack_df[pack_df["药品规格"] == i]
        if row.empty:
            raise ValueError(f"Excel初始解缺少规格 {i} 的包装记录。")
        row = row.iloc[0]
        line = row["分装铝塑设备"]
        if (i, line) not in p4i:
            raise ValueError(f"Excel初始解中 {i} 的包装设备 {line} 不可用。")

        snapshot["t4"][i] = _to_int(row["包装开工(班时)"], scale)
        for j in J4:
            if (i, j) in p4i:
                snapshot["x4"][i, j] = 1 if j == line else 0

        for idx, b in enumerate(B[i]):
            batch_finish = snapshot["t4"][i] + (idx + 1) * p4i[i, line]
            snapshot["batch_finish"][i, b] = batch_finish
            snapshot["batch_cycle"][i, b] = batch_finish - snapshot["t1"][i, b]
            snapshot["batch_cycle_over"][i, b] = max(0, snapshot["batch_cycle"][i, b] - Ti[i])
        finish = snapshot["t4"][i] + len(B[i]) * p4i[i, line]
        snapshot["E"][i] = max(0, finish - di[i])

    return snapshot


def _schedule_tasks_from_snapshot(snapshot, I, J1, J2, J3, J4, B, p1, p2, p3, p4, scale):
    tasks = []
    for i in I:
        for b in B[i]:
            for j in J1:
                if snapshot["x1"].get((i, b, j), 0) > 0:
                    tasks.append((1, j, i, b, snapshot["t1"][i, b], snapshot["e1"][i, b]))
            for j in J2:
                if snapshot["x2"].get((i, b, j), 0) > 0:
                    tasks.append((2, j, i, b, snapshot["t2"][i, b], snapshot["e2"][i, b]))
            for j in J3:
                if snapshot["x3"].get((i, b, j), 0) > 0:
                    tasks.append((3, j, i, b, snapshot["t3"][i, b], snapshot["e3"][i, b]))
        for j in J4:
            if snapshot["x4"].get((i, j), 0) > 0:
                start = snapshot["t4"][i]
                end = start + _to_int(p4[i, j], scale) * len(B[i])
                tasks.append((4, j, i, None, start, end))
    return tasks


def _validate_snapshot_constraints(
    snapshot, I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
    stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
    max_continuous_run, tol=1, periodic_cleaning_time=1.0
):
    violations = []
    p1i = {(i, j): _to_int(v, scale) for (i, j), v in p1.items()}
    p2i = {(i, j): _to_int(v, scale) for (i, j), v in p2.items()}
    p3i = {(i, j): _to_int(v, scale) for (i, j), v in p3.items()}
    p4i = {(i, j): _to_int(v, scale) for (i, j), v in p4.items()}
    di = {i: _to_int(d.get(i, 62), scale) for i in I}
    Ti = {i: _to_int(T.get(i, 0), scale) for i in I}
    vj = {j: _to_int((machine_available_time or {}).get(j, 0), scale) for j in J1 + J2 + J3 + J4}
    rb = {(i, b): _to_int((release_time or {}).get((i, b), 0), scale) for i in I for b in B[i]}
    clear_scaled = {
        stage: {(a, b): _to_int(v, scale) for (a, b), v in (clear_time_matrices or {}).get(stage, {}).items()}
        for stage in (1, 2, 3, 4)
    }

    def add(kind, detail):
        violations.append({"类型": kind, "说明": detail})

    for i in I:
        for b in B[i]:
            mix = [j for j in J1 if snapshot["x1"].get((i, b, j), 0) > 0]
            tab = [j for j in J2 if snapshot["x2"].get((i, b, j), 0) > 0]
            if len(mix) != 1:
                add("设备选择", f"{i} 批次{b} 配料设备选择数={len(mix)}")
                continue
            if len(tab) != 1:
                add("设备选择", f"{i} 批次{b} 压片设备选择数={len(tab)}")
                continue
            j1, j2 = mix[0], tab[0]
            if abs(snapshot["e1"][i, b] - snapshot["t1"][i, b] - p1i[i, j1]) > tol:
                add("加工时长", f"{i} 批次{b} 配料时长不等于参数")
            if abs(snapshot["e2"][i, b] - snapshot["t2"][i, b] - p2i[i, j2]) > tol:
                add("加工时长", f"{i} 批次{b} 压片时长不等于参数")
            if snapshot["t1"][i, b] + tol < rb[i, b]:
                add("释放时间", f"{i} 批次{b} 配料早于释放时间")
            if snapshot["t1"][i, b] + tol < vj[j1]:
                add("设备可用", f"{i} 批次{b} 配料早于设备{j1}可用时间")
            if snapshot["t2"][i, b] + tol < snapshot["e1"][i, b]:
                add("工序先后", f"{i} 批次{b} 压片早于配料完成")
            if snapshot["t2"][i, b] + tol < vj[j2]:
                add("设备可用", f"{i} 批次{b} 压片早于设备{j2}可用时间")

            needs_coating = any((i, j) in p3i for j in J3)
            if needs_coating:
                coat = [j for j in J3 if snapshot["x3"].get((i, b, j), 0) > 0]
                if len(coat) != 1:
                    add("设备选择", f"{i} 批次{b} 包衣设备选择数={len(coat)}")
                else:
                    j3 = coat[0]
                    if abs(snapshot["e3"][i, b] - snapshot["t3"][i, b] - p3i[i, j3]) > tol:
                        add("加工时长", f"{i} 批次{b} 包衣时长不等于参数")
                    if snapshot["t3"][i, b] + tol < snapshot["e2"][i, b]:
                        add("工序先后", f"{i} 批次{b} 包衣早于压片完成")
                    if snapshot["t3"][i, b] + tol < vj[j3]:
                        add("设备可用", f"{i} 批次{b} 包衣早于设备{j3}可用时间")

        pack = [j for j in J4 if snapshot["x4"].get((i, j), 0) > 0]
        if len(pack) != 1:
            add("设备选择", f"{i} 包装设备选择数={len(pack)}")
            continue
        j4 = pack[0]
        if snapshot["t4"][i] + tol < vj[j4]:
            add("设备可用", f"{i} 包装早于设备{j4}可用时间")
        for idx, b in enumerate(B[i]):
            pack_start = snapshot["t4"][i] + idx * p4i[i, j4]
            ready = snapshot["e3"].get((i, b), snapshot["e2"][i, b])
            if pack_start + tol < ready:
                add("包装衔接", f"{i} 批次{b} 包装早于前序完成")
            expected_finish = pack_start + p4i[i, j4]
            if abs(snapshot["batch_finish"][i, b] - expected_finish) > tol:
                add("批次完工", f"{i} 批次{b} 批次完工时间不等于包装结束")
            expected_cycle = snapshot["batch_finish"][i, b] - snapshot["t1"][i, b]
            if abs(snapshot["batch_cycle"][i, b] - expected_cycle) > tol:
                add("周期计算", f"{i} 批次{b} 真实周期计算错误")
            if snapshot["batch_cycle_over"][i, b] + tol < snapshot["batch_cycle"][i, b] - Ti[i]:
                add("周期计算", f"{i} 批次{b} 超周期变量小于真实超期")
        expected_e = max(0, snapshot["t4"][i] + len(B[i]) * p4i[i, j4] - di[i])
        if abs(snapshot["E"][i] - expected_e) > tol:
            add("延误计算", f"{i} 延误时间计算错误")

    tasks = _schedule_tasks_from_snapshot(snapshot, I, J1, J2, J3, J4, B, p1, p2, p3, p4, scale)
    by_machine = {}
    by_stage = {}
    for task in tasks:
        by_machine.setdefault((task[0], task[1]), []).append(task)
        by_stage.setdefault(task[0], []).append(task)

    for (stage, machine), machine_tasks in by_machine.items():
        machine_tasks.sort(key=lambda x: (x[4], x[5], str(x[2]), x[3] or 0))
        running_total = 0
        for idx, task in enumerate(machine_tasks):
            if idx > 0:
                prev = machine_tasks[idx - 1]
                if task[4] + tol < prev[5]:
                    add("设备重叠", f"工序{stage} 设备{machine} 任务重叠: {prev[2]}->{task[2]}")
                required_clear = clear_scaled.get(stage, {}).get((prev[2], task[2]), 0)
                gap = task[4] - prev[5]
                if gap + tol < required_clear:
                    add("清场", f"工序{stage} 设备{machine} {prev[2]}->{task[2]} 清场不足")
                if stage in (1, 2, 3):
                    if required_clear > 0 or (gap >= _to_int(periodic_cleaning_time, scale) - tol):
                        running_total = task[5] - task[4]
                    else:
                        running_total += task[5] - task[4]
                    if max_continuous_run is not None and running_total > _to_int(max_continuous_run, scale) + tol:
                        add("连续运行", f"工序{stage} 设备{machine} 连续运行超过{max_continuous_run}班时")
            else:
                running_total = task[5] - task[4]

    for stage, stage_tasks in by_stage.items():
        cap = (stage_staff_limits or {}).get(stage, 1)
        points = sorted(set([t for task in stage_tasks for t in (task[4], task[5])]))
        for idx in range(len(points) - 1):
            probe = points[idx]
            active = sum(1 for task in stage_tasks if task[4] <= probe < task[5])
            if active > cap:
                add("人员容量", f"工序{stage} 在{probe / scale:.2f}班时同时任务数{active}超过人员上限{cap}")
                break

    objective = _snapshot_objective(snapshot, I, B, w, cycle_over_penalty_factor=1000)
    return violations, objective


def validate_schedule_excel(
    result_excel, demo_file="./输入/demo2.xlsx", aps_file="./输入/副本APS排产信息-4.30.xlsx",
    speed_preset="fast", scale=None, report_file=None
):
    (
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, p5, d, T, w,
        stage_staff_limits, clear_time_matrices, machine_available_time,
        release_time, max_continuous_run
    ) = build_schedule_inputs(demo_file, aps_file)
    if scale is None:
        scale = _scale_from_preset(speed_preset)
    snapshot = _load_snapshot_from_excel(result_excel, I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale)
    violations, objective = _validate_snapshot_constraints(
        snapshot, I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
        stage_staff_limits, clear_time_matrices, machine_available_time,
        release_time, max_continuous_run
    )
    report = pd.DataFrame(violations)
    if report_file:
        with pd.ExcelWriter(report_file) as writer:
            report.to_excel(writer, sheet_name="约束违反明细", index=False)
    print(f"校验完成: violations={len(violations)}, objective={objective}")
    if violations:
        print(report.head(20).to_string(index=False))
    return violations, objective


def results_to_excel_cp(
    I, B, J1, J2, J3, J4, p1, p2, p3, p4, T, vars_dict, solver, scale, w, filename,
    clear_time_matrices=None, max_continuous_run=None, periodic_cleaning_time=1.0
):
    t1, t2, t3, t4 = vars_dict["t1"], vars_dict["t2"], vars_dict["t3"], vars_dict["t4"]
    e1, e2, e3 = vars_dict["e1"], vars_dict["e2"], vars_dict["e3"]
    x1, x2, x3, x4 = vars_dict["x1"], vars_dict["x2"], vars_dict["x3"], vars_dict["x4"]
    E = vars_dict["E"]
    batch_cycle = vars_dict["batch_cycle"]
    batch_cycle_over = vars_dict["batch_cycle_over"]

    batch_records = []
    drug_records = []
    machine_tasks = {}

    def add_task(stage_no, stage_name, device, item, batch, start, end, feature):
        if not device or str(device) in ["未分配", "-", "nan"]:
            return
        machine_tasks.setdefault((stage_no, stage_name, device), []).append({
            "item": item,
            "batch": batch,
            "start": float(start),
            "end": float(end),
            "feature": feature,
        })

    for i in I:
        needs_coating = any((i, j) in p3 for j in J3)
        for b in B[i]:
            mix_device = next((j for j in J1 if (i, j) in p1 and solver.Value(x1[i, b, j]) > 0), "未分配")
            tab_device = next((j for j in J2 if (i, j) in p2 and solver.Value(x2[i, b, j]) > 0), "未分配")
            row = {
                "药品规格": i,
                "批次号": b,
                "配料开工(班时)": round(solver.Value(t1[i, b]) / scale, 2),
                "配料设备": mix_device,
                "压片开工(班时)": round(solver.Value(t2[i, b]) / scale, 2),
                "压片设备": tab_device,
                "批次真实生产周期(班时)": round(solver.Value(batch_cycle[i, b]) / scale, 2),
                "批次周期上限(班时)": T.get(i, 0),
                "批次超周期(班时)": round(solver.Value(batch_cycle_over[i, b]) / scale, 2),
                "批次周期权重": w.get(i, 1),
            }
            add_task(
                1, "1. 配料", mix_device, i, b,
                solver.Value(t1[i, b]) / scale,
                solver.Value(e1[i, b]) / scale,
                get_mix_spec(i)
            )
            add_task(
                2, "2. 压片", tab_device, i, b,
                solver.Value(t2[i, b]) / scale,
                solver.Value(e2[i, b]) / scale,
                get_mix_spec(i)
            )
            if needs_coating:
                coat_device = next((j for j in J3 if (i, j) in p3 and solver.Value(x3[i, b, j]) > 0), "未分配")
                row["包衣开工(班时)"] = round(solver.Value(t3[i, b]) / scale, 2)
                row["包衣设备"] = coat_device
                add_task(
                    3, "3. 包衣", coat_device, i, b,
                    solver.Value(t3[i, b]) / scale,
                    solver.Value(e3[i, b]) / scale,
                    get_mix_spec(i)
                )
            else:
                row["包衣开工(班时)"] = "无需包衣"
                row["包衣设备"] = "-"
            batch_records.append(row)

    for i in I:
        valid_j4 = [j for j in J4 if (i, j) in p4]
        line = next((j for j in valid_j4 if solver.Value(x4[i, j]) > 0), None)
        start = solver.Value(t4[i]) / scale
        dur = len(B[i]) * p4[i, line] if line else 0
        add_task(4, "4. 包装", line, i, None, start, start + dur, get_pack_spec(i))
        drug_records.append({
            "药品规格": i,
            "包装开工(班时)": round(start, 2),
            "包装完工(班时)": round(start + dur, 2),
            "分装铝塑设备": line if line else "未分配",
            "总批次": len(B[i]),
            "权重(w)": w.get(i, 1),
            "延误班时": round(solver.Value(E[i]) / scale, 2),
        })

    clear_time_matrices = clear_time_matrices or {}
    clear_records = []
    for (stage_no, stage_name, device), tasks in machine_tasks.items():
        tasks.sort(key=lambda x: (x["start"], x["end"], x["item"], x["batch"] or 0))
        if not tasks:
            continue
        running_total = tasks[0]["end"] - tasks[0]["start"]
        for idx in range(1, len(tasks)):
            prev_task = tasks[idx - 1]
            curr_task = tasks[idx]
            required_clear = float(clear_time_matrices.get(stage_no, {}).get((prev_task["item"], curr_task["item"]), 0))
            actual_gap = curr_task["start"] - prev_task["end"]
            effective_clear = required_clear
            clear_type = "大清场" if stage_no in (1, 2, 3) and required_clear > 0 else ("小清场" if stage_no == 4 and required_clear > 0 else "无清场")
            if stage_no in (1, 2, 3) and required_clear == 0 and actual_gap + 1e-6 >= periodic_cleaning_time:
                effective_clear = periodic_cleaning_time
                clear_type = "定期清场"

            if stage_no in (1, 2, 3):
                if effective_clear > 0:
                    running_total = curr_task["end"] - curr_task["start"]
                else:
                    running_total += curr_task["end"] - curr_task["start"]
                over_limit = bool(max_continuous_run is not None and running_total > max_continuous_run + 1e-6)
            else:
                running_total = None
                over_limit = False

            clear_records.append({
                "工序": stage_name,
                "设备": device,
                "前任务药品规格": prev_task["item"],
                "前任务批次": prev_task["batch"] if prev_task["batch"] is not None else "-",
                "前任务开始(班时)": round(prev_task["start"], 2),
                "前任务结束(班时)": round(prev_task["end"], 2),
                "后任务药品规格": curr_task["item"],
                "后任务批次": curr_task["batch"] if curr_task["batch"] is not None else "-",
                "后任务开始(班时)": round(curr_task["start"], 2),
                "清场开始(班时)": round(prev_task["end"], 2),
                "清场结束(班时)": round(prev_task["end"] + effective_clear, 2),
                "理论清场时长": effective_clear,
                "实际间隔": round(actual_gap, 2),
                "清场是否满足": actual_gap + 1e-6 >= effective_clear,
                "前任务特征": prev_task["feature"],
                "后任务特征": curr_task["feature"],
                "特征是否相同": prev_task["feature"] == curr_task["feature"],
                "连续无清场累计(班时)": "" if running_total is None else round(running_total, 2),
                "是否超过11班时": over_limit,
                "清场类型": clear_type,
            })

    with pd.ExcelWriter(filename) as writer:
        pd.DataFrame(batch_records).to_excel(writer, sheet_name="批次工序计划", index=False)
        pd.DataFrame(drug_records).to_excel(writer, sheet_name="包装及摘要", index=False)
        pd.DataFrame(clear_records).to_excel(writer, sheet_name="设备清场明细", index=False)


def solve_pharmaceutical_schedule_cp(
    demo_file,
    aps_file,
    speed_preset="balanced",
    scale=None,
    stage1_sec=60,
    stage2_sec=300,
    num_workers=8,
    seeds=(11, 23, 37),
    output_file="排产结果明细_7月_alns.xlsx",
    initial_solution_mode="auto",
    initial_solution_file=None,
):
    print("1. 读取输入数据...")
    (
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, p5, d, T, w,
        stage_staff_limits, clear_time_matrices, machine_available_time,
        release_time, max_continuous_run
    ) = build_schedule_inputs(demo_file, aps_file)

    if scale is None:
        scale = _scale_from_preset(speed_preset)

    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }

    t0 = time.time()
    initial_solution_mode = (initial_solution_mode or "auto").lower()
    if initial_solution_mode not in ("auto", "excel", "best"):
        raise ValueError("initial_solution_mode 只能取 auto / excel / best")
    if initial_solution_mode in ("excel", "best") and not initial_solution_file:
        raise ValueError("选择 excel/best 初始解模式时必须提供 initial_solution_file")

    print(f"2. 构造初解，preset={speed_preset}, scale={scale}, mode={initial_solution_mode}...")
    initial_pool = []

    if initial_solution_mode in ("auto", "best"):
        order_modes = ["priority", "due", "risk", "pack_heavy", "random"]
        for idx, order_mode in enumerate(order_modes):
            rng = random.Random(100 + idx)
            snap = _build_greedy_snapshot(
                I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
                stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
                max_continuous_run=max_continuous_run, order_mode=order_mode, rng=rng,
            )
            obj = _snapshot_objective(snap, I, B, w, cycle_over_penalty_factor=1000)
            initial_pool.append((obj, f"auto:{order_mode}", snap))
            print(f"   初解[auto:{order_mode}] objective={obj}")

    if initial_solution_mode in ("excel", "best"):
        snap = _load_snapshot_from_excel(
            initial_solution_file, I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale
        )
        violations, obj = _validate_snapshot_constraints(
            snap, I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
            stage_staff_limits, clear_time_matrices, machine_available_time,
            release_time, max_continuous_run
        )
        if violations:
            print(f"   初解[excel] 存在{len(violations)}条约束违反，将不作为初始解。")
            for row in violations[:10]:
                print(f"     - {row['类型']}: {row['说明']}")
            if initial_solution_mode == "excel":
                raise ValueError("Excel初始解不满足硬约束，不能作为 ALNS 初始解。")
        else:
            initial_pool.append((obj, "excel", snap))
            print(f"   初解[excel] objective={obj}")

    if not initial_pool:
        raise ValueError("没有可用初始解。")
    initial_obj, initial_mode, initial_snapshot = min(initial_pool, key=lambda x: x[0])
    print(f"   选用初解={initial_mode}, objective={initial_obj}, time={time.time() - t0:.2f}s")

    seed_list = list(seeds) if seeds else [17]
    best_snapshot = initial_snapshot
    best_obj = initial_obj
    best_solver = None
    best_ctx = None
    best_seed = initial_mode

    if stage1_sec > 0:
        print(f"3. Stage-1 启发式预热 ({stage1_sec}s)...")
        stage1_snapshot, stage1_solver, stage1_ctx, stage1_obj = _run_alns(
            I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
            stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
            max_continuous_run, best_snapshot, best_obj, stage1_sec, num_workers,
            rng_seed=seed_list[0],
        )
        print(f"   Stage-1 best objective={stage1_obj}")
        if stage1_obj < best_obj:
            best_snapshot = stage1_snapshot
            best_obj = stage1_obj
            best_solver = stage1_solver
            best_ctx = stage1_ctx
            best_seed = f"warmup-{seed_list[0]}"

    if stage2_sec > 0:
        print(f"4. Stage-2 ALNS优化 ({stage2_sec}s)...")
        remaining_budget = int(stage2_sec)
        for idx, seed in enumerate(seed_list):
            seeds_left = len(seed_list) - idx
            run_budget = max(1, remaining_budget // max(1, seeds_left))
            if idx == len(seed_list) - 1:
                run_budget = max(1, remaining_budget)

            run_snapshot, run_solver, run_ctx, run_obj = _run_alns(
                I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
                stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
                max_continuous_run, best_snapshot, best_obj, run_budget, num_workers,
                rng_seed=seed,
            )
            print(f"   seed={seed}, budget={run_budget}s, best objective={run_obj}")

            if run_obj < best_obj:
                best_snapshot = run_snapshot
                best_obj = run_obj
                best_solver = run_solver
                best_ctx = run_ctx
                best_seed = seed

            remaining_budget -= run_budget
            if remaining_budget <= 0:
                break

    if best_ctx is None:
        best_ctx = _build_model(
            I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
            stage_staff_limits=stage_staff_limits,
            clear_time_matrices=clear_time_matrices,
            machine_available_time=machine_available_time,
            release_time=release_time,
            max_continuous_run=max_continuous_run,
        )
        best_solver = _SnapshotValueSolver(_snapshot_to_name_map(best_ctx["vars"], best_snapshot))

    vars_dict = best_ctx["vars"]
    total_time = time.time() - t0
    print(f"5. 采用最优seed={best_seed}, objective={best_obj}, total_time={total_time:.2f}s")

    results_to_excel_cp(
        I, B, J1, J2, J3, J4, p1, p2, p3, p4, T,
        vars_dict, best_solver, scale, w, output_file,
        clear_time_matrices=clear_time_matrices,
        max_continuous_run=max_continuous_run,
    )
    print(f"已导出: {output_file}")


if __name__ == "__main__":
    DEMO_FILE = "./输入/demo2.xlsx"
    APS_FILE = "./输入/副本APS排产信息-4.30.xlsx"
    OUTPUT_FILE = "排产结果明细_7月_alns.xlsx"
    INITIAL_SOLUTION_FILE = "排产结果明细_7月_cp_w_w_900.xlsx"

    solve_pharmaceutical_schedule_cp(
        DEMO_FILE,

        APS_FILE,
        speed_preset="fast",   # fast / balanced / accurate
        scale=None,             # None时按preset自动设置
        stage1_sec=200,
        stage2_sec=7200,
        num_workers=8,
        seeds=(11, 23, 37),
        output_file=OUTPUT_FILE,
        initial_solution_mode="best",  # auto / excel / best
        initial_solution_file=INITIAL_SOLUTION_FILE,
    )
