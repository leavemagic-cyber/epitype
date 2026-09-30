"""Handoff rule blocks are exact owner quotations from active decision cards."""
import argparse
from pathlib import Path
from . import memsearch, memspec

BEGIN = '<!-- EPITYPE OWNER RULINGS BEGIN -->'
END = '<!-- EPITYPE OWNER RULINGS END -->'


def region(text):
    if BEGIN not in text and END not in text:
        return None
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(END) < text.index(BEGIN):
        raise ValueError('裁定標記必須完整且只有一對')
    return text[text.index(BEGIN):text.index(END) + len(END)]


def render(vaults):
    lines = [BEGIN, 'owner 裁定（從卡片產生；修改正本後重新產生）：']
    for vault in vaults:
        for path in memsearch.card_files(vault):
            fields, _problem = memspec.frontmatter_fields(path)
            if str(fields.get('handoff_rule', '')).casefold() != 'true':
                continue
            if (fields.get('decided_by') != 'owner-explicit'
                    or fields.get('decision_status') != 'active' or not fields.get('owner_quote')):
                raise ValueError(f'交接裁定必須有有效 owner 原話：{path.name}')
            lines.extend((f"[{path.name}]", fields['owner_quote']))
    lines.append(END)
    return '\n'.join(lines)


def generate(text, vaults):
    old = region(text)
    block = render(vaults)
    return text.replace(old, block, 1) if old is not None else text.rstrip() + '\n\n' + block + '\n'


def violation(before, after, vaults):
    old = region(before)
    new = region(after)
    if old is None and new is None:
        return None
    if new != render(vaults):
        return '交接裁定段與卡片正本不一致；請從 owner 裁定卡重新產生，不改寫或刪除規則段。'
    return None


def main():
    parser = argparse.ArgumentParser(description='由 owner 裁定卡產生交接規則段')
    parser.add_argument('--vault', action='append', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    path = Path(args.out)
    before = path.read_text(encoding='utf-8-sig') if path.exists() else ''
    path.write_text(generate(before, [Path(vault) for vault in args.vault]), encoding='utf-8')


if __name__ == '__main__':
    main()
