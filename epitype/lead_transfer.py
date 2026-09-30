"""Durable handoff and explicit takeover acknowledgement; no host automation."""
import argparse
import hashlib
import json
from pathlib import Path

CLAUDE_SOURCE = 'https://code.claude.com/docs/en/desktop'
CLAUDE_MESSAGING = 'https://code.claude.com/docs/en/cross-session-messaging'
CODEX_REPORT = 'https://github.com/openai/codex/issues/44707'


def routes():
    return [
        f'HANDOFF Claude: 已確認 owner 點任務卡開新場；列場／訊息依 {CLAUDE_MESSAGING}；{CLAUDE_SOURCE}',
        f'HANDOFF Codex: 檔案交接後由 owner 開接手場；create_thread/send_message_to_thread/handoff_thread 自動路徑 UNVERIFIED；{CODEX_REPORT} 是問題回報，非支援保證',
        'HANDOFF 共通: 保存交接 → 開接手場 → 接手者讀檔並確認版本 → 舊場才停止；不把工具回覆或傳送成功當成接手',
    ]


def digest(path):
    data = Path(path).read_bytes()
    if not data.strip():
        raise ValueError('交接檔不可空白')
    return hashlib.sha256(data).hexdigest()


def marker(path):
    return Path(str(Path(path).resolve()) + '.transfer.json')


def prepare(path, sender):
    if not sender:
        raise ValueError('必須指定原場 id')
    value = {'handoff': str(Path(path).resolve()), 'sha256': digest(path),
             'sender': sender, 'recipient': None, 'status': 'awaiting-takeover'}
    marker(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return value


def acknowledge(path, recipient, expected_digest):
    value = json.loads(marker(path).read_text(encoding='utf-8'))
    if not recipient or recipient == value['sender']:
        raise ValueError('接手者必須是另一場')
    if expected_digest != value['sha256'] or digest(path) != expected_digest:
        raise ValueError('交接版本已變動，必須重新讀取')
    value.update(recipient=recipient, status='accepted')
    marker(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return value


def ready(path):
    try:
        value = json.loads(marker(path).read_text(encoding='utf-8'))
        return bool(value['status'] == 'accepted' and value.get('recipient')
                    and value['recipient'] != value['sender'] and digest(path) == value['sha256'])
    except (OSError, ValueError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description='保存交接版本，接手者確認後舊場才可停止')
    parser.add_argument('action', choices=['prepare', 'accept', 'check'])
    parser.add_argument('handoff')
    parser.add_argument('--session')
    parser.add_argument('--sha256')
    args = parser.parse_args()
    if args.action == 'prepare':
        value = prepare(args.handoff, args.session)
    elif args.action == 'accept':
        value = acknowledge(args.handoff, args.session, args.sha256)
    else:
        value = {'ready_to_stop': ready(args.handoff)}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
