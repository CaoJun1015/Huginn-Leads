"""Offline quality audit. Exit 1 means label disagreements, not a crashed test run."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from huginn_leads.rules import DEFAULT_TEMPLATE_FIELDS, PROCESSOR_VERSION, evaluate, is_candidate

FIELDS = ('memory_gb', 'storage_tb', 'cpu', 'gpu', 'price', 'quantity', 'quantity_unit')
ORIGINS = ('synthetic', 'real_desensitized')
SPLITS = ('development', 'acceptance')


def normalized(value):
    return ''.join(value.split()).casefold() if isinstance(value, str) else value


def load_samples(path):
    samples = json.loads(path.read_text(encoding='utf-8-sig'))
    ids, fingerprints = set(), set()
    for sample in samples:
        if sample['origin'] not in ORIGINS or sample['split'] not in SPLITS:
            raise ValueError('invalid origin/split')
        if sample['origin'] == 'real_desensitized':
            provenance = sample.get('provenance', {})
            if (provenance.get('authorized') is not True or provenance.get('redacted') is not True
                    or not all(provenance.get(key) for key in ('batch', 'reviewer'))):
                raise ValueError('real sample requires authorization, redaction and annotation provenance')
        fingerprint = (normalized(sample['text']), json.dumps(sample['conditions'], sort_keys=True))
        if sample['id'] in ids or fingerprint in fingerprints:
            raise ValueError('duplicate sample or cross-split leakage')
        ids.add(sample['id'])
        fingerprints.add(fingerprint)
        if not sample['reason'] or type(sample['candidate']) is not bool or sample['candidate'] != bool(sample['items']):
            raise ValueError('missing reason or inconsistent candidate label')
        for item in sample['items']:
            known, unknown = set(item['known_fields']), set(item['unknown_fields'])
            if known & unknown or known | unknown != set(FIELDS):
                raise ValueError('each field must have one known/unknown label')
            if item['direction'] not in ('supply', 'demand', 'unknown') or item['outcome'] not in ('match', 'review', 'conflict'):
                raise ValueError('invalid item label')
            if not item['text'] or any(value is None for value in item['known_fields'].values()):
                raise ValueError('invalid known field/text')
    return samples


def extracted_fields(item):
    extracted = item['extracted']
    values = {key: value['value'] for key, value in extracted.get('specs', {}).items()}
    for key in ('price', 'quantity'):
        if extracted.get(key):
            values[key] = extracted[key]['value']
    if extracted.get('quantity'):
        values['quantity_unit'] = extracted['quantity']['unit']
    return values


def measure(samples, predictor=evaluate, gate=is_candidate):
    counts = Counter()
    failures = []
    for sample in samples:
        counts['samples'] += 1
        expected = sample['items']
        actual = predictor(sample['text'], sample['conditions'])
        candidate = gate(sample['text'], sample['conditions'])
        counts['expected_candidates'] += sample['candidate']
        counts['recalled_candidates'] += sample['candidate'] and candidate
        counts['gated_messages'] += candidate
        counts['expected_leads'] += len(expected)
        counts['emitted_leads'] += len(actual)
        counts['review_leads'] += sum(item['outcome'] == 'review' for item in actual)
        counts['matched_leads'] += sum(item['outcome'] == 'match' for item in actual)
        remaining = list(actual)
        errors = []
        if sample['candidate'] and not candidate:
            errors.append('candidate missed')
        for wanted in expected:
            index = next((i for i, item in enumerate(remaining) if normalized(item['text']) == normalized(wanted['text'])), None)
            got = remaining.pop(index) if index is not None else None
            counts['direction_total'] += 1
            if got:
                counts['retained_leads'] += 1
                counts['direction_correct'] += got['direction'] == wanted['direction']
                if got['direction'] != wanted['direction']:
                    errors.append(f"direction {got['direction']} != {wanted['direction']}")
                if got['outcome'] != wanted['outcome']:
                    errors.append(f"outcome {got['outcome']} != {wanted['outcome']}")
                counts['false_matches'] += got['outcome'] == 'match' and wanted['outcome'] != 'match'
            else:
                errors.append('lead missed')
            values = extracted_fields(got) if got else {}
            for field, value in wanted['known_fields'].items():
                counts['known_total'] += 1
                correct = got is not None and normalized(values.get(field)) == normalized(value)
                counts['known_correct'] += correct
                if not correct:
                    errors.append(f"field {field}: {values.get(field)!r} != {value!r}")
            # Missing leads are penalized above, never rewarded as correct unknowns.
            if got:
                for field in wanted['unknown_fields']:
                    counts['unknown_total'] += 1
                    correct = values.get(field) is None
                    counts['unknown_correct'] += correct
                    if not correct:
                        errors.append(f"unknown {field} fabricated as {values[field]!r}")
        counts['false_leads'] += len(remaining)
        counts['false_matches'] += sum(item['outcome'] == 'match' for item in remaining)
        if remaining:
            errors.append(f'{len(remaining)} unexpected lead(s)')
        counts['exact_samples'] += not errors
        if errors:
            failures.append({'id': sample['id'], 'category': sample['category'], 'errors': errors})

    def ratio(numerator, denominator):
        n, d = counts[numerator], counts[denominator]
        return {'numerator': n, 'denominator': d, 'percent': round(n / d * 100, 2) if d else None}

    return {
        'samples': counts['samples'],
        'candidate_recall': ratio('recalled_candidates', 'expected_candidates'),
        'lead_recall': ratio('retained_leads', 'expected_leads'),
        'direction_accuracy': ratio('direction_correct', 'direction_total'),
        'known_field_accuracy': ratio('known_correct', 'known_total'),
        'unknown_field_safety': ratio('unknown_correct', 'unknown_total'),
        'lead_false_discovery': ratio('false_leads', 'emitted_leads'),
        'match_false_discovery': ratio('false_matches', 'matched_leads'),
        'review_ratio': ratio('review_leads', 'emitted_leads'),
        'exact_samples': ratio('exact_samples', 'samples'),
        'failures': failures,
    }


def template_evaluate(text, conditions):
    values = dict(conditions.get('specs', {}), direction=conditions['direction'], product=conditions['product'])
    for field in ('price', 'quantity'):
        if conditions.get(field, {}).get('value') is not None:
            values[field] = conditions[field]['value']
    if conditions.get('quantity', {}).get('unit'):
        values['quantity_unit'] = conditions['quantity']['unit']
    return evaluate(text, conditions | {'values': values}, DEFAULT_TEMPLATE_FIELDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=Path, default=ROOT / 'tests/fixtures/quality_samples.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--compare-revisions', nargs='*', default=[], help='Local trusted Git revisions only')
    args = parser.parse_args()
    samples = load_samples(args.samples)
    groups = {}
    for origin in ORIGINS:
        for split in ('all', *SPLITS):
            groups[f'{origin}/{split}'] = measure([s for s in samples if s['origin'] == origin and (split == 'all' or s['split'] == split)])
    report = {
        'processor_version': PROCESSOR_VERSION,
        'dataset_sha256': hashlib.sha256(args.samples.read_bytes()).hexdigest(),
        'categories': dict(Counter(s['category'] for s in samples)),
        'groups': groups,
        'template_entry_disagreements': [sample['id'] for sample in samples
            if measure([sample], template_evaluate) != measure([sample])],
    }
    report['revision_comparison'] = []
    for revision in args.compare_revisions:
        commit = subprocess.check_output(['git', 'rev-parse', '--verify', f'{revision}^{{commit}}'], cwd=ROOT, text=True).strip()
        source = subprocess.check_output(['git', 'show', f'{commit}:src/huginn_leads/rules.py'], cwd=ROOT, encoding='utf-8')
        historical = types.ModuleType('historical_rules')
        exec(compile(source, f'{commit}/rules.py', 'exec'), historical.__dict__)
        result = measure(samples, historical.evaluate, historical.is_candidate)
        report['revision_comparison'].append({
            'commit': commit, 'processor_version': historical.PROCESSOR_VERSION,
            'exact_samples': result['exact_samples'],
            'failure_ids': [failure['id'] for failure in result['failures']],
        })
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered, encoding='utf-8')
    for name, group in groups.items():
        print(f"{name}: {group['samples']} samples")
        for metric, value in group.items():
            if isinstance(value, dict):
                display = 'N/A' if value['percent'] is None else f"{value['percent']}%"
                print(f"  {metric}: {value['numerator']}/{value['denominator']} ({display})")
    return int(bool(report['template_entry_disagreements']) or any(group['failures'] for name, group in groups.items() if name.endswith('/all')))


if __name__ == '__main__':
    raise SystemExit(main())
