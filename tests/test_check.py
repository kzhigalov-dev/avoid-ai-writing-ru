import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check', ROOT / 'scripts/check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class ScannerTests(unittest.TestCase):
    def test_real_candidate_line_and_no_authorship_score(self):
        result = check.scan('Введение\nЭто открывает новые горизонты.\n', 'thesis')
        self.assertEqual(result['findings'][0]['line'], 2)
        self.assertIsNone(result['authorship_assessment'])

    def test_scientific_conventions_are_not_errors(self):
        text = 'Цель работы — оценить связь. Полученные результаты являются предварительными. Таким образом, гипотеза требует проверки. Связь статистически значима (p = 0,03).'
        self.assertEqual(check.scan(text, 'thesis')['findings'], [])

    def test_protected_examples_excluded(self):
        text = '«Учёные доказали»\n> меняет правила игры\n\n```text\nоткрывает новые горизонты\n```\n\n| Текст |\n|---|\n| не имеет аналогов |\n\n$\\text{меняет правила игры}$\n'
        self.assertEqual(check.scan(text)['findings'], [])

    def test_attributed_claim_is_not_vague_candidate(self):
        self.assertEqual(check.scan('Исследования показывают различие [12, с. 40].')['findings'], [])

    def test_missing_source_flagged_and_file_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'draft.md'
            original = 'Учёные доказали эффективность метода.'
            file.write_text(original, encoding='utf-8')
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/check.py'), 'scan', str(file)], capture_output=True)
            self.assertEqual(run.returncode, 1)
            self.assertEqual(file.read_text(encoding='utf-8'), original)

    def test_bibliography_not_scanned(self):
        self.assertEqual(check.scan('# Список литературы\n1. Автор. Меняет правила игры.\n')['findings'], [])

    def test_casual_context_preserves_noncritical_patterns(self):
        self.assertEqual(check.scan('Это меняет правила игры.', 'casual')['findings'], [])

    def test_binary_document_rejected(self):
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/check.py'), 'scan', 'draft.docx'], capture_output=True)
        self.assertEqual(run.returncode, 2)

    def test_citation_markup_survives_link_masking(self):
        result = check.scan('Ссылка contentReference[oaicite:0]{index=0}.')
        self.assertEqual(result['findings'][0]['rule'], 'citation-leak')

    def test_citation_markup_in_documentation_code_excluded(self):
        self.assertEqual(check.scan('Пример: `contentReference[oaicite:0]{index=0}`.')['findings'], [])

    def test_cli_clean_and_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'draft.txt'
            file.write_text('Проверены 48 анкет.', encoding='utf-8')
            clean = subprocess.run([sys.executable, str(ROOT / 'scripts/check.py'), 'scan', str(file)], capture_output=True)
            self.assertEqual(clean.returncode, 0)
            missing = subprocess.run([sys.executable, str(ROOT / 'scripts/check.py'), 'scan', str(file.with_name('absent.txt'))], capture_output=True)
            self.assertEqual(missing.returncode, 2)


class PreservationTests(unittest.TestCase):
    def test_ordinary_edit_allowed(self):
        before = 'Было осуществлено проведение анализа 48 анкет [7].'
        after = 'Проанализированы 48 анкет [7].'
        self.assertEqual(check.preserve(before, after)['differences'], [])

    def test_data_citation_and_quote_damage_detected(self):
        before = '# Итог\n«Точный текст». В выборке 48 человек [7, с. 12]. $x=2$.\n'
        after = '# Итог\n«Другой текст». В выборке 49 человек [8, с. 12]. $x=3$.\n'
        kinds = {x['kind'] for x in check.preserve(before, after)['differences']}
        self.assertTrue({'quote', 'numbers', 'citations-and-links', 'math'} <= kinds)

    def test_table_code_url_and_frontmatter_damage_detected(self):
        before = '---\nname: draft\n---\n| x |\n|---|\n| 2 |\n```py\na=1\n```\nhttps://example.org/?page=2\n'
        after = before.replace('draft', 'new').replace('| 2 |', '| 3 |').replace('a=1', 'a=2').replace('page=2', 'page=3')
        kinds = {x['kind'] for x in check.preserve(before, after)['differences']}
        self.assertTrue({'frontmatter', 'table', 'code', 'urls'} <= kinds)

    def test_order_change_visible(self):
        result = check.preserve('48 анкет, 12 ответов.', '12 анкет, 48 ответов.')
        self.assertTrue(next(d for d in result['differences'] if d['kind'] == 'numbers')['order_changed'])

    def test_unit_change_detected(self):
        self.assertTrue(check.preserve('Длина 12 мм.', 'Длина 12 см.')['differences'])

    def test_semantic_limit_explicit(self):
        # Token equality cannot tell whether an uncertainty marker disappeared.
        result = check.preserve('Возможно, связь существует.', 'Связь существует.')
        self.assertEqual(result['differences'], [])
        self.assertTrue(any('уверенность' in line for line in result['limitations']))

    def test_preserve_cli_reports_real_data_damage(self):
        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / 'before.md'
            after = Path(directory) / 'after.md'
            before.write_text('Масса 12 кг.', encoding='utf-8')
            after.write_text('Масса 12 г.', encoding='utf-8')
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/check.py'), 'preserve', str(before), str(after)], capture_output=True)
            self.assertEqual(run.returncode, 1)


class PackageTests(unittest.TestCase):
    def test_upstream_snapshot_checksums(self):
        import hashlib
        import re
        mapping = (ROOT / 'references/upstream-map.md').read_text(encoding='utf-8')
        entries = re.findall(r'`(upstream/[^`]+)`: SHA-256 `([a-f0-9]+)`', mapping)
        self.assertEqual(len(entries), 2)
        for file, digest in entries:
            self.assertEqual(hashlib.sha256((ROOT / file).read_bytes()).hexdigest(), digest)

    def test_all_upstream_categories_have_russian_targets(self):
        import re
        source = (ROOT / 'upstream/references/patterns.md').read_text(encoding='utf-8')
        mapping = (ROOT / 'references/upstream-map.md').read_text(encoding='utf-8')
        patterns = (ROOT / 'references/patterns.md').read_text(encoding='utf-8')
        headings = re.findall(r'^### (.+)$', source, re.M)
        rows = re.findall(r'^\| (.+?) \| \[(.+?)\]\(patterns.md#[^)]+\) \|$', mapping, re.M)
        self.assertEqual(set(headings), {row[0] for row in rows})
        targets = set(re.findall(r'^#{2,3} (.+)$', patterns, re.M))
        self.assertTrue({row[1] for row in rows} <= targets)

    def test_entrypoint_local_links_exist(self):
        import re
        text = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        for link in re.findall(r'\]\(([^)]+)\)', text):
            if '://' not in link:
                self.assertTrue((ROOT / link.split('#')[0]).is_file(), link)


if __name__ == '__main__':
    unittest.main()
