"""Portable experiment summaries built only from recorded host-side results."""
from collections import Counter
from datetime import datetime, timezone
from html import escape
import math
from pathlib import Path
import re


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def display(value):
    if value is None:
        return 'Not recorded'
    if type(value) is bool:
        return 'Yes' if value else 'No'
    if number(value):
        return f'{value:g}' if type(value) is int else f'{value:.3g}'
    return str(value)


def seconds(value):
    return f'{value:.2f} s' if number(value) else 'Not recorded'


def outcome(row):
    if row.get('status') == 'completed' and type(row.get('verified_success')) is bool:
        return 'PASS' if row['verified_success'] else 'FAIL'
    return 'RUNNING' if row.get('status') == 'running' else 'ERROR' if row.get('status') != 'completed' else 'UNVERIFIED'


def telemetry(root, row):
    """Read bounded tool event counts; omit arguments and raw tool output."""
    import json
    from .run_artifacts import open_saved
    calls, results = Counter(), Counter()
    notes, found, read = [], False, 0
    path = row.get('attempt_path', '')
    for suffix in ('guest-output/events.jsonl', 'events.jsonl'):
        try:
            with open_saved(root / 'evaluation', path + '/' + suffix) as stream:
                found = True
                while line := stream.readline(1024 * 1024):
                    read += len(line)
                    if read > 64 * 1024 * 1024:
                        notes.append('Tool events truncated at 64 MiB.')
                        break
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError()
                        name = event.get('tool')
                        if isinstance(name, str) and name:
                            if event.get('type') == 'tool_call':
                                calls[name] += 1
                            elif event.get('type') == 'tool_result':
                                results[name] += 1
                    except ValueError:
                        if 'Some tool events are unreadable.' not in notes:
                            notes.append('Some tool events are unreadable.')
            break
        except (OSError, ValueError):
            continue
    return dict(calls=dict(calls), results=dict(results), available=found, notes=notes)


def reported_tokens(row):
    usages = row.get('provider_usage')
    if not isinstance(usages, list):
        return None
    values = []
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        total = usage.get('total_tokens')
        if not number(total):
            incoming = usage.get('prompt_tokens', usage.get('input_tokens'))
            outgoing = usage.get('completion_tokens', usage.get('output_tokens'))
            total = incoming + outgoing if number(incoming) and number(outgoing) else None
        if number(total):
            values.append(total)
    return sum(values) if values else None


def md(value):
    # Escape HTML and Markdown syntax so arbitrary prompts/outputs stay text.
    text = escape(display(value), quote=False)
    return re.sub(r'([\\`*_{}\[\]()#+.!|>-])', r'\\\1', text).replace('\n', '<br>')


class Document:
    def __init__(self):
        self.markdown, self.html = [], []

    def heading(self, text, level=2):
        self.markdown.append('#' * level + ' ' + md(text))
        self.html.append(f'<h{level}>{escape(text)}</h{level}>')

    def paragraph(self, text):
        self.markdown.append(md(text))
        self.html.append('<p>' + escape(text) + '</p>')

    def table(self, columns, rows):
        self.markdown.append('\n'.join(['| ' + ' | '.join(md(c) for c in columns) + ' |',
                                       '| ' + ' | '.join('---' for _ in columns) + ' |',
                                       *['| ' + ' | '.join(md(c) for c in row) + ' |' for row in rows]]))
        head = ''.join('<th scope="col">' + escape(display(c)) + '</th>' for c in columns)
        body = ''.join('<tr>' + ''.join('<td>' + escape(display(c)).replace('\n', '<br>') + '</td>' for c in row) + '</tr>' for row in rows)
        self.html.append('<div class="table-scroll"><table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>')

    def code(self, text):
        text = str(text)
        # Longer fence than any recorded run output, including malicious text.
        fence = '`' * max(3, 1 + max((len(m.group()) for m in re.finditer(r'`+', text)), default=0))
        self.markdown.append(f'{fence}text\n{text}\n{fence}')
        self.html.append('<pre><code>' + escape(text) + '</code></pre>')


COLORS = dict(PASS='#167d58', FAIL='#c34642', ERROR='#a86512', UNVERIFIED='#686c9c', RUNNING='#2776b8', PENDING='#9ca9b8')


def charts(groups, rows, observed_tools):
    """Standalone SVG with labeled values, no JS, fonts or network assets."""
    parts = ['<title id="chart-title">Experiment outcomes, worker runtime and observed tool result events</title>',
             '<desc id="chart-desc">Outcomes use the latest attempt per trial. Errors and unstarted trials are separate from verified failures. Runtime uses recorded worker execution seconds.</desc>',
             '<rect width="100%" height="100%" fill="white"/>']
    y = 35
    def text(x, y, value, size=14, weight='normal'):
        parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}">{escape(str(value))}</text>')
    def bar(x, y, width, color):
        parts.append(f'<rect x="{x:.2f}" y="{y}" width="{width:.2f}" height="19" rx="3" fill="{color}"/>')
    text(24, y, 'Trial outcomes by condition', 20, 'bold')
    y += 30
    for index, (name, color) in enumerate(COLORS.items()):
        x = 24 + index * 148
        bar(x, y - 14, 16, color)
        text(x + 22, y, name, 12)
    y += 36
    maximum = max((sum(group['outcomes'].values()) for group in groups.values()), default=1) or 1
    for name, group in groups.items():
        text(24, y + 14, name[:26])
        x = 235
        for result, color in COLORS.items():
            count = group['outcomes'].get(result, 0)
            if count:
                width = 500 * count / maximum
                bar(x, y, width, color)
                x += width
        label = ', '.join(f'{count} {kind.lower()}' for kind, count in group['outcomes'].items() if count) or 'No trials recorded'
        text(235, y + 39, label, 12)
        y += 67
    if not groups:
        text(24, y, 'Evaluation has not started; no trial outcomes recorded.')
        y += 35
    y += 25
    text(24, y, 'Mean worker runtime by condition (seconds)', 20, 'bold')
    y += 30
    runtime = {name: [r['execution_seconds'] for r in rows if r.get('condition_id') == name and number(r.get('execution_seconds'))] for name in groups}
    means = {name: sum(values) / len(values) if values else None for name, values in runtime.items()}
    maximum = max((value for value in means.values() if value is not None), default=1) or 1
    for name, mean in means.items():
        text(24, y + 14, name[:26])
        if mean is not None:
            bar(235, y, 500 * mean / maximum, '#2776b8')
        text(760, y + 14, f'{mean:.2f} s' if mean is not None else 'Not recorded')
        y += 35
    if not means:
        text(24, y, 'Worker runtime is not recorded yet.')
        y += 35
    y += 30
    text(24, y, 'Observed tool result events (top 10)', 20, 'bold')
    y += 30
    maximum = max(observed_tools.values(), default=1) or 1
    for name, count in observed_tools.most_common(10):
        text(24, y + 14, name[:26])
        bar(235, y, 500 * count / maximum, '#686c9c')
        text(760, y + 14, count)
        y += 35
    if not observed_tools:
        text(24, y, 'No tool result events observed in the collected logs.')
        y += 35
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 {y + 25}" role="img" aria-labelledby="chart-title chart-desc" style="font-family:system-ui,sans-serif;fill:#223047">' + ''.join(parts) + '</svg>'


STYLE = '''body{margin:0;background:#edf2f7;color:#223047;font:16px/1.6 system-ui,-apple-system,sans-serif}main{max-width:1120px;margin:32px auto;padding:40px;background:white;border-radius:16px;box-shadow:0 8px 40px #22304715}h1{font-size:32px;line-height:1.2}h2{margin-top:40px;border-bottom:2px solid #dbe5f0;padding-bottom:8px}h3{margin-top:28px}p{overflow-wrap:anywhere}.table-scroll,.chart{overflow-x:auto;margin:20px 0}table{border-collapse:collapse;width:100%;font-size:14px}th{background:#e8eff8;text-align:left}th,td{padding:10px 12px;border-bottom:1px solid #dbe5f0;vertical-align:top}td{overflow-wrap:anywhere;min-width:80px}tbody tr:nth-child(even){background:#f7f9fc}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5fa;border:1px solid #dbe5f0;padding:18px;border-radius:8px;font:14px/1.65 ui-monospace,monospace}.chart svg{width:100%;min-width:760px;display:block}@media(max-width:700px){main{margin:0;padding:20px;border-radius:0}h1{font-size:26px}}@media print{body{background:white}main{margin:0;padding:0;box-shadow:none;max-width:none}.table-scroll,.chart{overflow:visible}.chart svg{min-width:0}thead{display:table-header-group}tr,pre{break-inside:avoid}h2,h3{break-after:avoid}}'''


def render(report, *, root=None, recorded_at=None):
    config, workflow, evaluation = report.get('run_configuration') or {}, report.get('workflow') or {}, report.get('evaluation') or {}
    attempts = evaluation.get('attempts') or []
    latest = {}
    for row in attempts:
        old = latest.get(row['trial_id'])
        if old is None or int(row.get('attempt', 0)) > int(old.get('attempt', 0)):
            latest[row['trial_id']] = row
    rows = sorted(latest.values(), key=lambda r: r['trial_id'])
    groups = {c['id']: dict(outcomes=Counter()) for c in config.get('conditions') or []}
    for row in rows:
        group = groups.setdefault(row.get('condition_id', 'Unknown'), dict(outcomes=Counter()))
        group['outcomes'][outcome(row)] += 1
    for trial in config.get('schedule') or []:
        if trial['trial_id'] not in latest:
            groups.setdefault(trial['condition_id'], dict(outcomes=Counter()))['outcomes']['PENDING'] += 1
    counts = Counter(outcome(row) for row in rows)
    tools, metrics = Counter(), {}
    for row in rows:
        observed = telemetry(Path(root), row) if root else row.get('report_telemetry', dict(available=False,calls={},results={},notes=[]))
        metrics[row['trial_id']] = observed
        tools.update(observed['results'])
    doc = Document()
    identity = Path(workflow.get('output') or 'run').name
    title = 'Experiment run summary · ' + identity
    doc.heading(title, 1)
    doc.paragraph('Saved run snapshot · ' + (recorded_at or datetime.now(timezone.utc).isoformat()))
    doc.paragraph('Trial outcomes and charts use the latest attempt per trial. PASS/FAIL are verified task results; execution errors or interruptions, unverified outcomes and pending trials are shown separately.')
    steps = (workflow.get('workflow_progress') or {}).get('steps') or []
    starts = [s['started_at'] for s in steps if s.get('started_at')] + [r['started_at'] for r in rows if r.get('started_at')]
    ends = [s['ended_at'] for s in steps if s.get('ended_at')] + [r['ended_at'] for r in rows if r.get('ended_at')]
    started, ended = min(starts) if starts else None, max(ends) if ends else None
    duration = None
    if started and ended and workflow.get('recorded_status') in ('completed', 'completed_with_errors', 'failed', 'cancelled', 'interrupted'):
        try:
            duration = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
        except ValueError:
            pass
    model = config.get('model') or {}
    scenario = config.get('scenarioforge') or {}
    metadata = scenario.get('metadata') or {}
    doc.table(['Run', 'Recorded value'], [
        ['Workflow', workflow.get('workflow_id')], ['Status', workflow.get('recorded_status')],
        ['Scenario', metadata.get('name') or metadata.get('scenario') or (workflow.get('scenario_experiment') or {}).get('scenario')],
        ['Model / provider', display(model.get('name')) + ' / ' + display(model.get('provider'))],
        ['Started', started], ['Last recorded finish', ended], ['Recorded run span', seconds(duration)],
        ['Planned trials', evaluation.get('planned_trials')], ['Observed trials', len(rows)],
        ['PASS / FAIL', f'{counts["PASS"]} / {counts["FAIL"]}'],
        ['Execution errors or interruptions / unverified / running', f'{counts["ERROR"]} / {counts["UNVERIFIED"]} / {counts["RUNNING"]}'],
        ['Unstarted trials', evaluation.get('unstarted_trials')], ['Attempts recorded', evaluation.get('attempt_count')]])
    if not evaluation:
        doc.paragraph('Evaluation has not started; no trial outcomes or scores have been recorded.')
    doc.heading('Charts')
    svg = charts(groups, rows, tools)
    doc.markdown.append('![Trial outcomes, worker runtime and observed tools](experiment-charts.svg)')
    doc.html.append('<div class="chart">' + svg + '</div>')
    doc.paragraph('The Markdown chart uses the companion experiment-charts.svg file included in the run ZIP. The HTML report embeds the chart and opens offline.')
    doc.heading('Condition comparison')
    comparisons, condition_metrics = [], []
    summaries = evaluation.get('conditions') or {}
    for name, group in groups.items():
        summary, results = summaries.get(name, {}), group['outcomes']
        verified = results['PASS'] + results['FAIL']
        comparisons.append([name, results['PASS'], results['FAIL'], results['ERROR'], results['UNVERIFIED'], results['RUNNING'], results['PENDING'],
                            f'{100 * results["PASS"] / verified:.1f}%' if verified else 'Not verified'])
        condition_metrics.append([name, summary.get('mean_score'), seconds(summary.get('mean_execution_seconds')),
                                  summary.get('unassisted_successes'), summary.get('assisted_successes'),
                                  summary.get('hints_released'), summary.get('facts_revealed'),
                                  summary.get('mean_progress_score'), seconds(summary.get('mean_time_to_first_flag_seconds'))])
    doc.table(['Condition','Pass','Fail','Error','Unverified','Running','Pending','Verified success rate'], comparisons)
    doc.table(['Condition','Mean score','Mean worker time','Unassisted passes','Assisted passes','Hints','Facts revealed','Mean progress','Mean first flag'], condition_metrics)
    doc.heading('Trial runs')
    doc.table(['Trial','Task','Condition','Repeat','Attempt','Outcome','Worker status','Worker time','Total trial time'], [
        [r['trial_id'],r.get('task_id'),r.get('condition_id'),r.get('repetition'),r.get('attempt'),outcome(r),r.get('status'),
         seconds(r.get('execution_seconds')),seconds(r.get('elapsed_seconds'))] for r in rows])
    doc.heading('Trial metrics')
    doc.table(['Trial','Score','Flags','Progress','First flag','Hints','Facts revealed','Model calls','Reported tokens'], [
        [r['trial_id'],r.get('score'),r.get('flags_observed'),r.get('progress_score'),seconds(r.get('time_to_first_flag_seconds')),
         r.get('hints_released'),r.get('facts_revealed'),r.get('model_calls'),reported_tokens(r)] for r in rows])
    doc.paragraph('Worker time is execution_seconds; total trial time is elapsed_seconds, including transfer and collection. Model calls can include retries and summarization. Tokens are sums of available provider reports; provider and nested-operation usage may be incomplete. Missing values are not zero.')
    doc.heading('Tools')
    doc.table(['Condition','Available tools'], [[c['id'],', '.join(c.get('tools') or []) or 'No tools'] for c in config.get('conditions') or []])
    for condition in config.get('conditions') or []:
        if condition.get('guidance_snapshot'):
            doc.heading('Additional guidance · ' + condition['id'],3)
            doc.code('\n\n'.join(g.get('text', '') for g in condition['guidance_snapshot']))
    tool_rows = []
    for row in rows:
        observed = metrics[row['trial_id']]
        names = sorted(set(observed['calls']) | set(observed['results']))
        if names:
            tool_rows.extend([row['trial_id'],row.get('condition_id'),name,observed['calls'].get(name,0),observed['results'].get(name,0)] for name in names)
        else:
            tool_rows.append([row['trial_id'],row.get('condition_id'),'No tool events observed' if observed['available'] else 'Tool events not recorded',0 if observed['available'] else None,0 if observed['available'] else None])
        for note in observed['notes']:
            doc.paragraph(row['trial_id'] + ': ' + note)
    doc.table(['Trial','Condition','Observed tool','Call events','Result events'],tool_rows)
    doc.paragraph('Available tools come from the frozen condition configuration. Observed tools come from collected tool_call/tool_result events; incomplete or missing telemetry does not prove that no tools ran.')
    doc.heading('Task prompts')
    for task in config.get('tasks') or []:
        doc.heading(task['id'] + ' · ' + display(task.get('family')),3)
        doc.code(task.get('prompt') or 'No saved task prompt')
    if not config.get('tasks'):
        doc.paragraph('No task prompt was captured before evaluation started.')
    doc.heading('Captured system prompts')
    for index, prompt in enumerate(config.get('system_prompts') or [],1):
        doc.heading(f'System prompt {index}',3)
        doc.code(prompt['text'])
    if not config.get('system_prompts'):
        doc.paragraph(config.get('system_prompt_note') or 'No system prompt was collected.')
    doc.heading('Agent output and errors')
    from .diagnostics import clean
    if workflow.get('error'):
        doc.code(clean(workflow['error']))
    for row in rows:
        doc.heading(row['trial_id'] + ' · ' + outcome(row),3)
        for error in row.get('errors') or []:
            doc.paragraph(clean(error))
        answer = row.get('final_answer')
        if answer:
            doc.code(answer[:12000])
            if len(answer) > 12000:
                doc.paragraph('Output preview limited to 12,000 characters. Full answer is in evaluation/' + row['attempt_path'] + '/attempt.json in the run ZIP.')
        else:
            doc.paragraph('No final agent answer was recorded.')
    if len(attempts) > len(rows):
        doc.heading('Attempt history')
        doc.table(['Trial','Attempt','Condition','Outcome','Worker time'],[[r['trial_id'],r.get('attempt'),r.get('condition_id'),outcome(r),seconds(r.get('execution_seconds'))] for r in attempts])
    doc.heading('Trial timestamps')
    doc.table(['Trial','Started','Ended'],[[r['trial_id'],r.get('started_at'),r.get('ended_at')] for r in rows])
    doc.heading('Stage timing')
    doc.table(['Stage','VM','Status','Started','Ended','Elapsed','Error'],[[s.get('label') or s.get('id'),s.get('vmid'),s.get('status'),s.get('started_at'),s.get('ended_at'),seconds(s.get('elapsed_seconds')),s.get('error') or ''] for s in steps])
    doc.heading('Recorded limits and provenance')
    execution = config.get('execution') or {}
    doc.table(['Setting','Value'],[[key,execution.get(key)] for key in ('max_turns','wall_seconds','tool_timeout','context_window','provide_progressive_hints')] +
              [['Allowed targets',', '.join((execution.get('network_policy') or {}).get('allow') or [])],
               ['Excluded targets',', '.join((execution.get('network_policy') or {}).get('disallow') or [])],
               ['Spec hash',evaluation.get('spec_hash')],['Scenario package hash',scenario.get('package_hash')]])
    html = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + escape(title) + '</title><style>' + STYLE + '</style></head><body><main>' + '\n'.join(doc.html) + '</main></body></html>'
    return {'experiment-summary.md':'\n\n'.join(doc.markdown)+'\n','experiment-summary.html':html,'experiment-charts.svg':svg}
