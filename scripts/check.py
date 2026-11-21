#!/usr/bin/env python3
"""Small Russian editorial scanner and conservative preservation checker.

No authorship score, no rewriting, no dependencies. Markdown recognition is
deliberately partial; compare meaning manually after every edit.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

CONTEXTS = ('linkedin', 'blog', 'technical-blog', 'investor-email', 'docs',
            'casual', 'academic', 'thesis', 'coursework', 'research-article')
ACADEMIC = {'academic', 'thesis', 'coursework', 'research-article'}


def protected_spans(text):
    """Recognize common fenced/inline code, tables, quotes and math."""
    spans = []
    lines = text.splitlines(keepends=True)
    offset = 0
    fence = None
    frontmatter = text.startswith('---\n') or text.startswith('---\r\n')
    front_start = 0
    bibliography = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if frontmatter:
            if i > 0 and stripped in ('---', '...'):
                spans.append((front_start, offset + len(line), 'frontmatter'))
                frontmatter = False
            offset += len(line)
            continue
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1]:
                spans.append((fence[2], offset + len(line), 'code'))
                fence = None
            offset += len(line)
            continue
        if match:
            fence = (match[1][0], len(match[1]), offset)
            offset += len(line)
            continue
        if re.match(r'^#{1,6}\s+', line):
            bibliography = bool(re.search(r'(список\s+(?:литературы|источников)|библиограф\w*)\s*$', stripped, re.I))
        if bibliography:
            spans.append((offset, offset + len(line), 'bibliography'))
        elif re.match(r'^\s*>', line):
            spans.append((offset, offset + len(line), 'blockquote'))
        elif ('|' in line and (stripped.startswith('|') or
              re.search(r'^\s*:?-{3,}:?\s*\|', line) or
              (i + 1 < len(lines) and re.search(r'^\s*\|?\s*:?-{3,}:?\s*\|', lines[i + 1])) or
              (i > 0 and re.search(r'^\s*\|?\s*:?-{3,}:?\s*\|', lines[i - 1])))):
            spans.append((offset, offset + len(line), 'table'))
        elif re.match(r'^\s*\[[^\]]+\]:\s*\S+', line):
            spans.append((offset, offset + len(line), 'reference-definition'))
        offset += len(line)
    if fence:
        spans.append((fence[2], len(text), 'code'))
    if frontmatter:
        spans.append((0, len(text), 'frontmatter'))
    patterns = [
        ('inline-code', r'(?<!`)(`+)(?!`)[^\n]*?\1(?!`)'),
        ('quote', r'«(?:[^«»]|«[^«»]*»)*»|„[^„“]*“|“[^“”]*”|"[^"\n]+"'),
        ('math', r'\$\$[\s\S]*?\$\$|(?<!\$)\$[^\n$]+\$(?!\$)|\\\[[\s\S]*?\\\]|\\\([^\n]*?\\\)'),
        ('comment', r'<!--[\s\S]*?-->'),
    ]
    for kind, pattern in patterns:
        for match in re.finditer(pattern, text):
            spans.append((match.start(), match.end(), kind))
    # Prefer the enclosing block so quotes inside code aren't counted twice.
    selected = []
    for start, end, kind in sorted(spans, key=lambda s: (s[0], -s[1])):
        if not any(start >= a and end <= b for a, b, _ in selected):
            selected.append((start, end, kind))
    return selected


def mask(text):
    chars = list(text)
    for start, end, _ in protected_spans(text):
        for i in range(start, end):
            if chars[i] not in '\r\n':
                chars[i] = ' '
    return ''.join(chars)


def plain_mask(text):
    result = mask(text)
    # Avoid matches in link destinations, bare URLs, DOI and citation labels.
    for pattern in (r'https?://[^\s<>]+', r'\b10\.\d{4,9}/[^\s<>]+',
                    r'\[[^\]\n]+\](?:\([^\n]*?\)|\[[^\]\n]*\])?',
                    r'(?m)^#{1,6}\s+[^\n]+'):
        result = re.sub(pattern, lambda m: ' ' * len(m[0]), result)
    return result


RULES = [
    ('chatbot', 'P0', r'(?<!\w)(?:надеюсь,? это (?:помогло|поможет)|вот ваш(?:а|е)? (?:диплом|текст|курсовая)|если (?:вам )?нужна дополнительная помощь)(?!\w)', 'Служебная реплика чатбота: проверить жанр.'),
    ('cutoff', 'P0', r'(?<!\w)(?:по состоянию на последнее обновление (?:моей )?базы знаний|как (?:языковая модель|искусственный интеллект)|у меня нет доступа к данным в реальном времени)(?!\w)', 'Оговорка модели внутри авторского текста.'),
    ('vague-attribution', 'P0', r'(?<!\w)(?:уч[её]ные доказали|эксперты (?:считают|полагают)|исследования показывают|независимые тесты подтвердили)(?!\w)', 'Проверить, есть ли конкретный источник в этом или соседнем предложении.'),
    ('citation-leak', 'P0', r'contentReference\[oaicite:\d+\]\{index=\d+\}|\boai_citation\b|\bturn\d+(?:search|view|fetch)\d+\b|\bgrok_card\b', 'Внутренняя разметка ссылки; нужен реальный источник.'),
    ('formulaic-intro', 'P1', r'(?<!\w)(?:в современном (?:стремительно меняющемся|динамично развивающемся) мире|открывает новые горизонты|меняет правила игры)(?!\w)', 'Возможная пустая или рекламная рамка.'),
    ('novelty', 'P1', r'(?<!\w)(?:впервые в истории|никто ранее не (?:рассматривал|исследовал)|не имеет аналогов)(?!\w)', 'Проверить основания и границы новизны; не удалять научный вклад автоматически.'),
    ('hedge-stack', 'P1', r'(?<!\w)(?:может потенциально|потенциально может|возможно,? (?:потенциально )?может)(?!\w)', 'Возможное дублирование оговорок; сохранить действительную неопределённость.'),
    ('clarity', 'P2', r'(?<!\w)(?:осуществл\w* проведение анализа|производ\w* оценку|имеет место увеличение|в силу того обстоятельства,? что)(?!\w)', 'Канцелярит: предложение по ясности, не признак авторства.'),
    ('filler', 'P2', r'(?<!\w)(?:необходимо особо отметить тот факт,? что|следует особо подчеркнуть,? что)(?!\w)', 'Проверить, можно ли убрать рамку без потери оценки.'),
]


def scan(text, context='blog'):
    editable = plain_mask(text)
    findings = []
    for rule, severity, pattern, note in RULES:
        if context == 'casual' and severity != 'P0':
            continue
        # Citation markup contains brackets, so link masking would hide it.
        searchable = mask(text) if rule == 'citation-leak' else editable
        for match in re.finditer(pattern, searchable, re.I):
            line_start = text.rfind('\n', 0, match.start()) + 1
            line_end = text.find('\n', match.end())
            line_end = len(text) if line_end < 0 else line_end
            nearby = text[line_start:line_end]
            if rule == 'vague-attribution' and re.search(r'\[\d+[^\]]*\]|https?://|\([А-ЯЁA-Z][^)]*\d{4}[^)]*\)', nearby):
                continue
            findings.append({'rule': rule, 'severity': severity,
                             'line': text.count('\n', 0, match.start()) + 1,
                             'column': match.start() - line_start + 1,
                             'text': text[match.start():match.end()], 'note': note})
    return {'status': 'candidates-only', 'context': context,
            'authorship_assessment': None, 'findings': sorted(findings, key=lambda f: (f['line'], f['column'])),
            'limitations': ['Неполный каталог; совпадение не является обоснованным замечанием.',
                            'Защищённые области исключены эвристически; их проверяет редактор.',
                            'Морфологический анализ и проверка фактов не выполняются.']}


def inventory(text):
    spans = protected_spans(text)
    result = {}
    for a, b, kind in spans:
        result.setdefault(kind, []).append(text[a:b])
    editable = mask(text)
    tokens = {
        'numbers': r'(?<!\w)[+−-]?\d+(?:[.,]\d+)*(?:[eE][+−-]?\d+)?(?:\s*[%‰])?(?!\w)',
        'urls': r'https?://[^\s<>]+',
        'doi': r'\b10\.\d{4,9}/[^\s<>]+',
        'citations-and-links': r'!?\[[^\]\n]+\](?:\([^\n]*?\)|\[[^\]\n]*\])?',
        'headings': r'(?m)^#{1,6}\s+[^\n]+',
        'paths': r'(?:[A-Za-z]:[\\/]|\.{1,2}/)[^\s<>]+',
        'units': r'(?<!\w)(?:кг|мг|мкг|г|км|см|мм|м|мл|л|мс|с|мин|ч|Гц|кГц|МГц|ГГц|Вт|кВт|МВт|Па|кПа|МПа|К|°C|°С|kg|mg|km|cm|mm|ms|Hz|kHz|MHz|GHz|kW|MPa)(?!\w)',
    }
    for kind, pattern in tokens.items():
        result[kind] = re.findall(pattern, editable)
    return result


def preserve(before, after):
    old, new = inventory(before), inventory(after)
    differences = []
    for kind in sorted(set(old) | set(new)):
        a, b = old.get(kind, []), new.get(kind, [])
        if a != b:
            differences.append({'kind': kind,
                                'removed': list((Counter(a) - Counter(b)).elements()),
                                'added': list((Counter(b) - Counter(a)).elements()),
                                'order_changed': Counter(a) == Counter(b)})
    return {'status': 'differences' if differences else 'no-mechanical-differences',
            'differences': differences,
            'limitations': ['Смысл, причинность, атрибуция и уверенность проверяются отдельно моделью.',
                            'Markdown распознаётся частично; бинарные документы не поддерживаются.',
                            'Разрешённые изменения требуют отдельной оценки и не отменяют этот результат.']}


def read(path):
    p = Path(path)
    if p.suffix.lower() not in ('.md', '.markdown', '.txt'):
        raise ValueError('Поддерживаются только UTF-8 .md, .markdown и .txt')
    return p.read_text(encoding='utf-8-sig')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    scanner = commands.add_parser('scan', help='Кандидаты редакторских замечаний, без правок')
    scanner.add_argument('file')
    scanner.add_argument('--context', choices=CONTEXTS, default='blog')
    validator = commands.add_parser('preserve', help='Консервативное сравнение защищённых элементов')
    validator.add_argument('before')
    validator.add_argument('after')
    args = parser.parse_args()
    try:
        if args.command == 'scan':
            result = scan(read(args.file), args.context)
            code = int(bool(result['findings']))
        else:
            result = preserve(read(args.before), read(args.after))
            code = int(bool(result['differences']))
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
