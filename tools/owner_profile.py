#!/usr/bin/env python3
"""Summarize owner-loop profiles (logs/owner-profile.jsonl, AGENT_DESKTOP_PROFILE_OWNER=1).

    python tools/owner_profile.py ARTIFACTS_OR_FILE... [--top N] [--exclude SCENARIO ...]

Reports probe lateness (p50/p95/p99/max and counts over 50/100/250ms), the top
contributors by total and by worst single time with their call paths, the
triggers of 50ms stalls, stalls that overlapped held input, startup failures,
and input timing against its intended hold, gap, scroll pace and recheck
interval. The
scenario is taken from the integration tests' artifact directory names
(agent-desktop-SCENARIO-XXXXXXXX); worker-stopped is excluded from the totals by
default because it freezes the worker on purpose. See docs/TESTING.md.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sys

STALLS = (50, 100, 250)
PASS_THROUGH = ('Connection.ready', 'Scheduler.tick', 'Desktop.tick')  # Stall triggers are their children.
SLACK_MS = 10  # Timing overshoot beyond this is reported one by one (the owner ticks every 5ms).
PROBE_SLACK = .006  # A late probe can land just after the delayed event it explains.
PROBE_INTERVAL = .005


def scenario(path):
    for part in reversed(Path(path).parts):
        match = re.fullmatch(r'agent-desktop-(.+)-[a-z0-9_]{8}', part)
        if match:
            return match.group(1)
    return '?'


def load(path):
    records = []
    with open(path, encoding='utf-8') as stream:
        for line in stream:
            try:
                records.append(json.loads(line))
            except ValueError:
                pass  # A worker killed mid-write leaves a partial last line.
    return records


def percentile(hist, fraction):
    total = sum(hist.values())
    if not total:
        return None
    target, seen = fraction * total, 0
    for key in sorted(hist):
        seen += hist[key]
        if seen >= target:
            return key
    return max(hist)


def category(element):
    name = element.split('@', 1)[0].split('[', 1)[0]
    if name == 'os.fsync':
        return 'fsync'
    if name.startswith('Store.') or name == 'mkdir_durable':
        return 'store (not fsync)'
    if name in ('atomic', 'read_metadata'):
        return 'lifecycle files'
    if name == 'import':
        return 'import'
    if name == 'gc':
        return 'gc'
    if name in ('Children.start', 'spawn', 'Desktop.launch'):
        return 'child spawn'
    if name.startswith(('PrivateBus.', 'dbus.')):
        return 'dbus'
    if name in ('Capture.__init__', 'Capture.step'):
        return 'screenshot'
    if name.startswith('Query.'):
        return 'window query'
    if name.startswith('Input.'):
        return 'libei'
    if name == 'profiler.write':
        return 'profiler (own writes)'
    return 'other'


def paths_of(spans):
    """Full call path of each span in a stall record (sorted by offset, then depth)."""
    stack, out = [], []
    for depth, name, offset, length, own in spans:
        del stack[depth:]
        stack.append(name)
        out.append(('>'.join(stack), offset, length, own))
    return out


def triggers(spans):
    """What one owner turn spent its stall on: request dispatch, task steps, ticks (1ms or more each)."""
    names = set()
    for depth, name, offset, length, own in spans:
        if length < 1:
            continue
        if depth == 1 and name not in PASS_THROUGH:
            names.add(name.split('@', 1)[0])
        elif depth == 2:
            names.add(name.split('@', 1)[0])
    return ' + '.join(sorted(names)) or '(root only)'


def owner_lateness(late, start, end):
    """Owner lateness inside [start, end]: each late probe covers [t - ms - one interval, t].

    The probe that ends a stall can run after the request completed, so probes are
    matched by overlap, not by their timestamp.
    """
    total = worst = 0.0
    for record in late:
        begin = record['t'] - record['ms'] / 1000 - PROBE_INTERVAL
        overlap = min(end, record['t']) - max(start, begin)
        if overlap > 0:
            total += overlap * 1000
            worst = max(worst, record['ms'])
    return total, worst


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--top', type=int, default=12)
    parser.add_argument('--exclude', nargs='*', default=['worker-stopped'], metavar='SCENARIO')
    args = parser.parse_args(argv)
    files = []
    for raw in args.paths:
        path = Path(raw)
        files += [path] if path.is_file() else sorted(path.rglob('owner-profile.jsonl'))
    if not files:
        parser.error('no owner-profile.jsonl found')

    hist, paths, late, stalls, inputs, done = defaultdict(int), {}, [], [], [], []
    rows, stopped, ends, stalled = [], [], {}, {}
    excluded = set(args.exclude)
    for path in files:
        records = load(path)
        label = scenario(path)
        summaries = [r for r in records if r['kind'] == 'summary']
        summary = summaries[-1] if summaries else None
        file_late = [r | {'label': label, 'file': str(path)} for r in records if r['kind'] == 'late']
        file_hist = {float(k): v for k, v in summary['probe']['hist'].items()} if summary else {}
        rows.append((label, path, summary, file_hist, file_late))
        stopped += [(label, r) for r in records if r['kind'] in ('failed', 'truncated')]
        ends[str(path)] = max((r['t'] for r in records if type(r.get('t')) in (int, float)), default=0)
        stalled[str(path)] = sum(r['ms'] for r in records if r['kind'] == 'stall')
        if label in excluded:
            continue
        for key, count in file_hist.items():
            hist[key] += count
        for key, value in (summary['paths'] if summary else {}).items():
            entry = paths.setdefault(key, [0, 0.0, 0.0, 0.0, 0.0])
            entry[0] += value[0]
            entry[1] += value[1]
            entry[2] += value[2]
            entry[3] = max(entry[3], value[3])
            entry[4] = max(entry[4], value[4])
        late += file_late
        stalls += [r | {'label': label, 'file': str(path)} for r in records if r['kind'] == 'stall']
        inputs.append((label, str(path), [r for r in records if r['kind'] == 'input']))
        done += [r | {'label': label, 'file': str(path)} for r in records if r['kind'] == 'done']

    print(f'{len(files)} profile(s); excluded from totals: {sorted(excluded) or "none"}')
    for label, record in stopped:
        reason = record.get('error') if record['kind'] == 'failed' else f'byte budget ({record.get("bytes")} bytes)'
        print(f'  {label}: recording stopped early: {reason}')
    print('\nPer generation (probe lateness, ms):')
    print(f'  {"scenario":<20} {"samples":>7} {"p50":>6} {"p99":>6} {"max":>8} ' + ' '.join(f'>{s:<4}' for s in STALLS))
    for label, path, summary, file_hist, file_late in rows:
        samples = summary['probe']['samples'] if summary else 0
        worst = summary['probe']['max_ms'] if summary else None
        worst = max([worst or 0] + [r['ms'] for r in file_late])
        counts = ' '.join(f'{sum(r["ms"] > s for r in file_late):<5}' for s in STALLS)
        mark = ' (excluded)' if label in excluded else ''
        print(f'  {label:<20} {samples:>7} {percentile(file_hist, .5) or 0:>6.1f} {percentile(file_hist, .99) or 0:>6.1f} '
              f'{worst:>8.1f} {counts}{mark}')

    total = sum(hist.values())
    print(f'\nLateness over all included generations: {total} probes every 5ms')
    if total:
        worst = max([max(hist)] + [r['ms'] for r in late])
        print('  p50 {:.1f}  p95 {:.1f}  p99 {:.1f}  max {:.1f} ms (0.1ms buckets below 10ms, 1ms below 100ms)'.format(
            percentile(hist, .5), percentile(hist, .95), percentile(hist, .99), worst))
        print('  ' + ', '.join(f'>{s}ms: {sum(r["ms"] > s for r in late)}' for s in STALLS)
              + f', >=10ms: {len(late)}')

    by_leaf, by_category = {}, defaultdict(float)
    for key, (count, seconds, own, worst, worst_own) in paths.items():
        leaf = key.rsplit('>', 1)[-1]
        entry = by_leaf.setdefault(leaf, [0, 0.0, 0.0, defaultdict(float)])
        entry[0] += count
        entry[1] += own
        entry[2] = max(entry[2], worst_own)
        entry[3][key] += own
        by_category[category(leaf)] += own
    print('\nSelf time by category (ms, all included generations):')
    whole = sum(by_category.values()) or 1
    for name, own in sorted(by_category.items(), key=lambda item: -item[1]):
        print(f'  {name:<20} {own:>10.1f}  {100 * own / whole:5.1f}%')
    store = [(key, value) for key, value in paths.items() if key.rsplit('>', 1)[-1].startswith('Store.')
             and not any(part.startswith('Store.') for part in key.split('>')[:-1])]
    store_ms = sum(value[1] for _, value in store)
    fsync_ms = sum(value[2] for key, value in paths.items() if key.rsplit('>', 1)[-1].startswith('os.fsync')
                   and 'Store.' in key)
    print(f'\nStore calls: {sum(v[0] for _, v in store)} calls, {store_ms:.1f} ms inclusive, '
          f'of which fsync {fsync_ms:.1f} ms and the rest {store_ms - fsync_ms:.1f} ms')

    def show(title, key):
        print(f'\nTop {args.top} by {title} (self ms; count, total, worst; heaviest path):')
        for leaf, (count, own, worst, where) in sorted(by_leaf.items(), key=key)[:args.top]:
            heaviest = max(where.items(), key=lambda item: item[1])[0]
            print(f'  {count:>6} {own:>9.1f} {worst:>8.2f}  {leaf}\n{"":>27}{heaviest}')
    show('total time', lambda item: -item[1][1])
    show('worst single call', lambda item: -item[1][2])

    print(f'\nWorst {args.top} late probes:')
    by_file = defaultdict(list)
    for stall in stalls:
        by_file[stall['file']].append(stall)

    def stall_at(file, start):
        return min((s for s in by_file[file] if abs(s['t'] - start) < .001), key=lambda s: abs(s['t'] - start),
                   default=None)
    for record in sorted(late, key=lambda r: -r['ms'])[:args.top]:
        context = {k: record[k] for k in ('held', 'active', 'phase') if k in record}
        print(f'  {record["ms"]:8.1f} ms  {record["label"]:<18} {context}')
        for name, offset, length in record['roots']:
            if length < 1:
                continue
            print(f'{"":>14}root {name} {length:.1f} ms')
            stall = stall_at(record['file'], record['t'] + offset / 1000)
            if stall:
                for path, _, _, own in sorted(paths_of(stall['spans']), key=lambda s: -s[3])[:4]:
                    if own >= .5:
                        print(f'{"":>18}{own:7.1f}  {path}')

    print('\nInput timing against intent (ms over the intended value; the owner steps every 5ms):')
    kinds = defaultdict(list)
    late_by_file = defaultdict(list)
    for record in late:
        late_by_file[record['file']].append(record)
    for label, file, events in inputs:
        events.sort(key=lambda e: e['t'])
        last = {}
        for event in events:
            rid = event.get('rid')
            previous = last.get(rid)
            name = event['ev']
            if event.get('failed'):
                continue  # The emission raised; its time is not a release or press time.
            if name == 'release' and previous and previous['ev'] == 'press' and 'hold' in previous:
                over = (event['t'] - previous['t'] - previous['hold']) * 1000
                kind = f'{event.get("active")} hold {previous["hold"] * 1000:g}ms'
                if over >= -1:
                    kinds[kind].append((over, label, event, file, previous['t']))
                else:
                    kinds[kind + ' (released early)'].append((over, label, event, file, previous['t']))
            elif name == 'press' and previous and previous['ev'] == 'release' and 'gap' in event:
                kinds[f'{event.get("active")} gap {event["gap"] * 1000:g}ms'].append(
                    ((event['t'] - previous['t'] - event['gap']) * 1000, label, event, file, previous['t']))
            elif name == 'scroll' and previous and previous['ev'] in ('scroll', 'move') and 'gap' in event:
                kinds[f'scroll pace {event["gap"] * 1000:g}ms'].append(
                    ((event['t'] - previous['t'] - event['gap']) * 1000, label, event, file, previous['t']))
            elif name == 'recheck_start' and event.get('due'):
                kinds['focus recheck start vs due'].append(
                    ((event['t'] - event['due']) * 1000, label, event, file, event['due']))
            if name in ('press', 'release', 'scroll', 'move'):
                last[rid] = event
    for kind, values in sorted(kinds.items()):
        overs = sorted(v[0] for v in values)
        p99 = overs[min(len(overs) - 1, int(.99 * len(overs)))]
        beyond = [v for v in values if v[0] > SLACK_MS]
        print(f'  {kind:<36} n={len(overs):<5} median {overs[len(overs) // 2]:6.1f}  p99 {p99:6.1f}  '
              f'max {overs[-1]:6.1f}  >{SLACK_MS}ms: {len(beyond)}')
        for over, label, event, file, since in sorted(beyond, key=lambda v: -v[0])[:5]:
            # The late probe(s) between the previous event and this one, and the root that caused the worst.
            during = [r for r in late_by_file[file] if since < r['t'] <= event['t'] + PROBE_SLACK]
            worst = max(during, key=lambda r: r['ms'], default=None)
            cause = ''
            if worst is not None:
                roots = sorted(worst['roots'], key=lambda root: -root[2])
                cause = f'; late probe {worst["ms"]:.1f} ms, root {roots[0][0]} {roots[0][2]:.1f} ms' if roots else ''
            print(f'{"":>6}{over:7.1f} ms  {label}  {event.get("path")}{cause}')

    print('\nStalls of 50ms or more by trigger (one owner turn each; fsyncs counted from its spans):')
    groups = defaultdict(list)
    for stall in stalls:
        if stall['ms'] >= 50:
            fsyncs = [span for span in stall['spans'] if span[1].startswith('os.fsync')]
            groups[triggers(stall['spans'])].append((stall['ms'], len(fsyncs), sum(span[4] for span in fsyncs)))
    whole = sum(v[0] for values in groups.values() for v in values)
    fsync_whole = sum(v[2] for values in groups.values() for v in values)
    print(f'  {sum(map(len, groups.values()))} stalls, {whole:.0f} ms, of which fsync {fsync_whole:.0f} ms '
          f'({100 * fsync_whole / (whole or 1):.0f}%)')
    for key, values in sorted(groups.items(), key=lambda item: -sum(v[0] for v in item[1]))[:args.top]:
        total_ms = sum(v[0] for v in values)
        print(f'  {len(values):>4} x  sum {total_ms:7.0f}  max {max(v[0] for v in values):6.1f}  '
              f'fsyncs/turn {sum(v[1] for v in values) / len(values):4.1f}  {key}')

    # A hold runs from the press that found nothing held to the first confirmed release:
    # held 0 afterwards and input not uncertain. Otherwise it runs to the end of the profile.
    holds, unconfirmed = defaultdict(list), 0
    for label, file, events in inputs:
        pressed = None
        for event in sorted(events, key=lambda e: e['t']):
            if event['ev'] == 'press' and pressed is None and event.get('held_before') == 0 and event.get('held'):
                pressed = event
            elif event['ev'] == 'release' and pressed is not None:
                if event.get('held') == 0 and not event.get('uncertain') and not event.get('failed'):
                    holds[file].append((pressed['t'], event['t']))
                    pressed = None
                else:
                    unconfirmed += 1
        if pressed is not None:
            holds[file].append((pressed['t'], ends.get(file, pressed['t'])))
    overlapped = []
    for stall in stalls:
        start, end = stall['t'], stall['t'] + stall['ms'] / 1000
        overlap = sum(max(0.0, min(end, b) - max(start, a)) for a, b in holds[stall['file']]) * 1000
        if overlap >= 1:
            overlapped.append((overlap, stall))
    print(f'\nStalls (10ms or more) during a held key or button: {len(overlapped)} '
          f'of {len(stalls)}; held time inside them {sum(o for o, _ in overlapped):.0f} ms; '
          f'releases that left input held or uncertain: {unconfirmed}')
    for overlap, stall in sorted(overlapped, key=lambda item: -item[0])[:args.top]:
        print(f'  {overlap:8.1f} ms held of a {stall["ms"]:.1f} ms stall  {stall["label"]:<18} '
              f'{stall.get("active")}/{stall.get("phase")}  {triggers(stall["spans"])}')

    failures = []
    for label, path, summary, file_hist, file_late in rows:
        try:
            failure = json.loads((Path(path).parent.parent / 'startup-failure.json').read_text())
        except (OSError, ValueError):
            continue
        failures.append((label, failure.get('code'), failure.get('message'), stalled[str(path)]))
    print(f'\nGenerations with a startup-failure.json (the first owner failure; compositor-death and '
          f'bus-death record one on purpose): {len(failures)}')
    for label, code, message, before in failures:
        print(f'  {label:<18} {code}: {message} (owner stalls of 10ms or more in that generation: {before:.0f} ms)')

    timeouts = [r for r in done if r.get('code') == 'timeout']
    print(f'\nResponses: {len(done)}; timeout: {len(timeouts)} (owner lateness overlapping each, from its own profile)')
    for record in timeouts:
        lateness = owner_lateness(late_by_file[record['file']], record['admitted'], record['t'])
        print(f'  {record["label"]:<18} {record["op"]:<10} {record["rid"]} admitted '
              f'{(record["t"] - record["admitted"]) * 1000:6.0f} ms; owner late {lateness[0]:6.0f} ms of it, '
              f'worst late probe {lateness[1]:.1f} ms')
    return 0


if __name__ == '__main__':
    sys.exit(main())
