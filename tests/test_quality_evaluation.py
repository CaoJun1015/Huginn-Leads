import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('quality', ROOT / 'scripts/evaluate_quality.py')
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


class QualityEvaluationTests(unittest.TestCase):
    def sample(self, text, items):
        return dict(id=text, text=text, category='test', conditions={}, candidate=bool(items), items=items)

    def wanted(self, text):
        return dict(text=text, direction='supply', outcome='review', known_fields={'memory_gb':16},
                    unknown_fields=[field for field in quality.FIELDS if field != 'memory_gb'])

    def prediction(self, text):
        return dict(text=text, direction='demand', outcome='match',
                    extracted={'specs':{'memory_gb':{'value':8}}, 'price':{'value':100}})

    def test_metrics_count_misses_false_positives_and_fabricated_fields(self):
        samples = [self.sample('wrong', [self.wanted('wrong')]),
                   self.sample('missing', [self.wanted('missing')]), self.sample('negative', [])]
        result = quality.measure(samples,
            predictor=lambda text, _: [] if text == 'missing' else [self.prediction(text)],
            gate=lambda text, _: text != 'missing')
        for metric, pair in {
            'candidate_recall':(1,2), 'lead_recall':(1,2), 'direction_accuracy':(0,2),
            'known_field_accuracy':(0,2), 'unknown_field_safety':(5,6),
            'lead_false_discovery':(1,2), 'match_false_discovery':(2,2),
            'review_ratio':(0,2), 'exact_samples':(0,3),
        }.items():
            self.assertEqual((result[metric]['numerator'],result[metric]['denominator']), pair, metric)
        self.assertEqual(len(result['failures']),3)

    def test_empty_group_has_no_invented_accuracy(self):
        result = quality.measure([])
        for value in result.values():
            if isinstance(value,dict):
                self.assertEqual(value,dict(numerator=0,denominator=0,percent=None))

    def test_multiline_pairing_uses_text_not_output_order(self):
        wanted = [self.wanted('first'), self.wanted('second')]
        actual = [dict(text=text, direction='supply', outcome='review',
                       extracted={'specs':{'memory_gb':{'value':16}}}) for text in ('second','first')]
        result = quality.measure([self.sample('both',wanted)],lambda *_:actual,lambda *_:True)
        self.assertEqual(result['failures'],[])
        self.assertEqual(result['known_field_accuracy']['numerator'],2)

    def test_corpus_validation_and_real_provenance(self):
        corpus = quality.load_samples(ROOT / 'tests/fixtures/quality_samples.json')
        self.assertEqual(len(corpus),120)
        self.assertEqual({s['origin'] for s in corpus},{'synthetic'})
        for sample in corpus:
            self.assertEqual(quality.measure([sample],quality.template_evaluate),quality.measure([sample]),sample['id'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'samples.json'
            for invalid in ([corpus[0],corpus[0]], [copy.deepcopy(corpus[0])]):
                if len(invalid)==1:
                    invalid[0]['origin']='real_desensitized'
                path.write_text(json.dumps(invalid),encoding='utf-8')
                with self.assertRaises(ValueError): quality.load_samples(path)
            valid=copy.deepcopy(corpus[0])
            valid.update(origin='real_desensitized',provenance=dict(authorized=True,redacted=True,batch='test',reviewer='test'))
            path.write_text(json.dumps([valid]),encoding='utf-8')
            self.assertEqual(len(quality.load_samples(path)),1)
            valid['provenance']['authorized']='false'
            path.write_text(json.dumps([valid]),encoding='utf-8')
            with self.assertRaises(ValueError): quality.load_samples(path)


if __name__ == '__main__':
    unittest.main()
