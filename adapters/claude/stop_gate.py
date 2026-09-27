import sys, time; sys.dont_write_bytecode = True; _STARTED_AT = time.monotonic(); [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdin, sys.stdout, sys.stderr)]  # cp950 consoles must not break hook entrypoints.
"""Stop adapter shared by both hosts: a turn that re-opens a settled ruling is blocked.

2026-09-05 事故：owner 已裁「虛擬必須鏡像實盤」的事，被同一場 session 重新端成選項
問 owner（owner：「為什麼還是會發生這種錯誤？」）。SessionStart 注入裁定、
UserPromptSubmit 喚回裁定，兩者都只是「說給模型聽」；回合結束前沒有任何一道閘去比
對模型剛說出口的話。這支 adapter 就是那道閘：命中決策卡的 `forbidden` 就擋；把已裁定
的事再問一次 owner 也擋，並要求照裁定改寫。

Fail-open by construction: any exception, an expired deadline, or a missing config
lets the turn end. A hook that could hang or crash the turn would be worse than the
error it guards against.
"""

from collections import namedtuple
import codecs
import json
import os
from pathlib import Path
import re

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epitype import memspec
from _hook_common import (
    isolated_temp_root,
    append_gate_log,
    clear_recall_markers,
    compile_bounded_regex,
    compile_pattern_or_literal,
    declared_frontmatter,
    emit,
    expired,
    governance_vault,
    leave_note,
    config_path,
    load_config,
    native_cwd_vaults,
    read_event,
    recall_marker_directory,
    remaining_seconds,
    run_synthetic,
    sequence_fields,
    with_session,
    write_config,
)

_TERMINATORS = re.escape(memspec.STOP_GATE_SENTENCE_TERMINATORS)
# One sentence with its terminator, or the trailing fragment that has none.
_SENTENCE_REGEX = re.compile(f"[^{_TERMINATORS}]*[{_TERMINATORS}]|[^{_TERMINATORS}]+$")
_WHITESPACE_REGEX = re.compile(r"\s+")
_FORBIDDEN_RULE = "forbidden"
_QUESTION_RULE = "question"
_DECISION_CACHE_FILENAME = "stop_decisions.json"
# The cached entry is discovery only — whether this card declares a ruling at all.
# Every authority the gate acts on is re-read from this turn's bytes below, so a
# ruling that gained fields (require_when/require_text/advice) needs no version bump.
# 7：fence_shell 成為武裝欄位——舊快取把只帶它的卡記成「不是裁定」，不換版就要等輪替才認得。
_DECISION_CACHE_VERSION = 7
_KEY = "key"
_DECIDED_AT = "decided_at"
_QUOTE = "quote"

_REQUIRE_RULE = "require"
_REQUIRE_WHEN = "require_when"
_REQUIRE_TEXT = "require_text"
_ADVICE = "advice"
_EXPIRES = "expires"
_TURN_CHECK_RULE = "turn_check"

_SELFTEST_HANG_SECONDS = 120

_Turn = namedtuple("_Turn", "texts prompt dispatch calls_after_dispatch")


def _turn(texts=(), prompt="", dispatch="", calls_after_dispatch=0):
    """`_Turn` 的預設建構子：測試與退路都只在意前兩個欄位。"""
    return _Turn(list(texts), prompt, dispatch, calls_after_dispatch)


_Decision = namedtuple(
    "_Decision",
    "key decided_at quote forbidden aliases path decided_by require_when require_text advice"
    " applies_to turn_check turn_check_limit on_hit fence_shell fence_langs",
    # 只有最後幾欄有預設值：沒標處置的卡就是「照擋」，沒宣告殼層的卡不查圍籬區塊；
    # 既有的建構處不必跟著改。
    defaults=("", "", ()),
)


def _one_line(value):
    return " ".join(str(value or "").split())


def _normalized(text):
    """Whitespace dropped and case folded, so an alias matches however it is spaced.

    別名去空白比對：`SESSION_RESUME` 與 `SESSION RESUME`、`Stop Hook` 與 `stop hook`
    是同一個名字，模型換個寫法就繞過閘門的話這道閘等於不存在。"""
    return _WHITESPACE_REGEX.sub("", str(text or "")).casefold()


def _decision_frontmatter(path):
    """Frontmatter of a card this gate rules on: a decision, or any card that arms.

    Arming must not depend on the card's type. card_lint tells the author of a
    behaviour card to add `forbidden` or `require_when`; if only decision-keyed cards
    were read, following that instruction would enforce nothing — the same silent
    disarming this gate exists to prevent, arriving through the lint's own advice
    (2026-09-16: six freshly armed behaviour cards were inert for exactly this
    reason)."""
    # 這張清單就是「哪些欄位會讓一張卡被這道閘看見」。新增武裝欄位卻忘了加進來，卡片
    # 檢查器會說它合格、閘門卻整張讀不到——2026-09-20 Codex 審查抓到 turn_check 正是
    # 這樣漏接的：只寫 turn_check 的卡，載入數是 0，而且沒有任何缺陷通知。
    for field in (
        memspec.DECISION_KEY_FIELD,
        memspec.FORBIDDEN_FIELD,
        memspec.REQUIRE_WHEN_FIELD,
        memspec.TURN_CHECK_FIELD,
        memspec.FENCE_SHELL_FIELD,
    ):
        lines = declared_frontmatter(
            path, field, memspec.STOP_GATE_FRONTMATTER_MAX_BYTES
        )
        if lines is not None:
            return lines
    return None


def _read_decision(path):
    """The active ruling one card carries, as plain JSON values, or None.

    Status is re-read from the card, never from the index: a stale index would pin a
    ruling the owner has already superseded, and this gate blocks on what it reads.

    U38: reads via memspec.frontmatter_fields/memspec.TOP_LEVEL_FIELD directly —
    the same primitives decision_lint._parse_frontmatter wraps — so this gate no
    longer pays decision_lint's argparse/dataclasses import even deferred."""
    front_lines = _decision_frontmatter(path)
    if front_lines is None:
        return None
    fields, _problem = memspec.frontmatter_text("---\n" + "\n".join(front_lines) + "\n---\n")
    key = _one_line(fields.get(memspec.DECISION_KEY_FIELD))
    status = _one_line(fields.get(memspec.DECISION_STATUS_FIELD))
    if key:
        # A ruling speaks only while it is the current one.
        if status != memspec.ACTIVE_DECISION_STATUS:
            return None
    else:
        # A behaviour card arms without carrying a ruling of its own; it is named by
        # its own name, and anything retired or superseded stops speaking.
        key = _one_line(fields.get(memspec.NAME_FIELD))
        if not key or status in memspec.STOP_GATE_SILENT_STATUSES:
            return None
    sequences = sequence_fields(
        front_lines, (memspec.ALIASES_FIELD, memspec.FORBIDDEN_FIELD, memspec.FENCE_LANGS_FIELD))
    decided_by = _one_line(fields.get(memspec.DECIDED_BY_FIELD))
    quote = _one_line(fields.get(memspec.OWNER_QUOTE_FIELD))
    if not quote and decided_by != memspec.OWNER_EXPLICIT_DECIDER:
        quote = _one_line(fields.get(memspec.DESCRIPTION_FIELD))
    return {
        _KEY: key,
        memspec.DECIDED_BY_FIELD: decided_by,
        _DECIDED_AT: _one_line(fields.get(memspec.CURRENT_DECISION_AT_FIELD)),
        _QUOTE: quote[: memspec.STOP_GATE_QUOTE_MAX_CHARS],
        memspec.FORBIDDEN_FIELD: sequences[memspec.FORBIDDEN_FIELD],
        memspec.ALIASES_FIELD: sequences[memspec.ALIASES_FIELD],
        _REQUIRE_WHEN: _one_line(fields.get(memspec.REQUIRE_WHEN_FIELD)),
        _REQUIRE_TEXT: _one_line(fields.get(memspec.REQUIRE_TEXT_FIELD)),
        _ADVICE: _one_line(fields.get(memspec.DESCRIPTION_FIELD))[
            : memspec.STOP_GATE_QUOTE_MAX_CHARS
        ],
        # 只把日期存下來，過沒過期在用的時候才判：卡片不動也會過期，而這裡的結果進快取。
        _EXPIRES: _one_line(fields.get(memspec.VALID_UNTIL_FIELD))
        or _one_line(fields.get(memspec.GRANT_EXPIRES_FIELD)),
        memspec.APPLIES_TO_FIELD: _one_line(fields.get(memspec.APPLIES_TO_FIELD)).casefold(),
        memspec.TURN_CHECK_FIELD: _one_line(fields.get(memspec.TURN_CHECK_FIELD)).casefold(),
        memspec.TURN_CHECK_LIMIT_FIELD: _one_line(fields.get(memspec.TURN_CHECK_LIMIT_FIELD)),
        memspec.ON_HIT_FIELD: _one_line(fields.get(memspec.ON_HIT_FIELD)).casefold(),
        memspec.FENCE_SHELL_FIELD: _one_line(fields.get(memspec.FENCE_SHELL_FIELD)).casefold(),
        memspec.FENCE_LANGS_FIELD: [
            _one_line(item).casefold() for item in sequences[memspec.FENCE_LANGS_FIELD]
        ],
    }


def _decision_from_ruling(vault, card_path, ruling):
    """一份裁定資料變成閘門用的結構；不該生效的回 None。"""
    if not isinstance(ruling, dict) or not ruling.get(_KEY):
        return None
    if memspec.card_expired(ruling.get(_EXPIRES)):
        # 過期的卡不再擋人。時限型的規則（試行一週、某日之前不要做某事）本來就該
        # 自己停下來，靠人記得去拔掉的話，它會一直擋到有人被擋為止。
        return None
    return _Decision(
        _one_line(ruling.get(_KEY)),
        _one_line(ruling.get(_DECIDED_AT)),
        _one_line(ruling.get(_QUOTE))[: memspec.STOP_GATE_QUOTE_MAX_CHARS],
        tuple(_strings(ruling.get(memspec.FORBIDDEN_FIELD))),
        tuple(_strings(ruling.get(memspec.ALIASES_FIELD))),
        vault / card_path,
        _one_line(ruling.get(memspec.DECIDED_BY_FIELD)),
        _one_line(ruling.get(_REQUIRE_WHEN)),
        _one_line(ruling.get(_REQUIRE_TEXT)),
        _one_line(ruling.get(_ADVICE)),
        _one_line(ruling.get(memspec.APPLIES_TO_FIELD)).casefold(),
        _one_line(ruling.get(memspec.TURN_CHECK_FIELD)).casefold(),
        _one_line(ruling.get(memspec.TURN_CHECK_LIMIT_FIELD)),
        _one_line(ruling.get(memspec.ON_HIT_FIELD)).casefold(),
        fence_shell=_one_line(ruling.get(memspec.FENCE_SHELL_FIELD)).casefold(),
        fence_langs=tuple(
            item.casefold() for item in _strings(ruling.get(memspec.FENCE_LANGS_FIELD))),
    )


def _read_cache(cache_path):
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}, ""
    if not isinstance(loaded, dict):
        return {}, {}, ""
    manifest = loaded.get("manifest")
    rulings = loaded.get("decisions")
    if not isinstance(manifest, dict) or not isinstance(rulings, dict):
        return {}, {}, ""
    version = loaded.get("version")
    if version != _DECISION_CACHE_VERSION:
        # 舊版的負例不能信：它不認得新的武裝欄位，照收的話只帶新欄位的卡永遠被當成
        # 「不是裁定」而且沒有訊號。但整份丟掉也不行——每回合只重認上限張，大庫升級後
        # 排在後面的既有規則會暫時失效（Codex 審查 2026-09-25）。所以正例沿用（候選每
        # 回合本來就重讀確認），負例丟掉重認。
        if isinstance(version, bool) or not isinstance(version, int) or version > _DECISION_CACHE_VERSION:
            return {}, {}, ""
        rulings = {
            key: value for key, value in rulings.items()
            if isinstance(value, dict) and value.get(_KEY)
        }
        return {key: value for key, value in manifest.items() if key in rulings}, rulings, ""
    cursor = loaded.get("cursor")
    return manifest, rulings, cursor if isinstance(cursor, str) else ""


def _write_cache(cache_path, manifest, rulings, cursor=""):
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        staging = cache_path.with_name(f".{cache_path.name}.tmp-{os.getpid()}")
        staging.write_text(
            json.dumps(
                {"version": _DECISION_CACHE_VERSION, "manifest": manifest, "decisions": rulings, "cursor": cursor},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(staging, cache_path)
    except OSError:
        pass


def _decisions(vault, started_at, defects=None):
    """Cache discovery only; every returned authority comes from this turn's bytes.

    Refresh at most one cap of discovery cards and one cap of known decisions.
    Negative entries rotate, because timestamps/size/identity cannot prove that a
    non-decision's contents never changed. Unread work retains its old signature.
    """
    from epitype import cardscan

    defects = [] if defects is None else defects
    vault = Path(vault).resolve()
    try:
        scan = cardscan.scan_vault(Path(vault).resolve())
    except Exception as exc:
        # 這個庫的規則這一次一條都沒生效。安靜回空的話，外面看起來就像「這裡沒有
        # 規則」——而那正是沒有人會去查的那個答案。
        defects.append(memspec.GATE_VAULT_UNREADABLE_NOTICE.format(
            gate=memspec.GATE_NAME_STOP, vault=Path(vault).name,
            reason=type(exc).__name__))
        return []
    manifest, paths = {}, {}
    for card_path, path, mtime_ns, size, ctime_ns in scan:
        if expired(started_at):
            # 逾時在盤點階段：這個庫這一輪一條都沒生效。靜靜回空的話，「這回合沒擋」
            # 與「這回合根本沒檢查」長得一模一樣。
            defects.append(memspec.STOP_GATE_INCOMPLETE_DEFECT.format(
                checked=0, total="?", vault=vault.name))
            return []
        # 走訪已經帶回這三項；再 stat 一次只為了 inode 與裝置編號，而 Windows 的
        # 目錄列表根本不給那兩項（實測回 0），等於每張卡多付一次系統呼叫換兩個零。
        manifest[card_path] = [mtime_ns, size, ctime_ns]
        paths[card_path] = path
    cache_path = vault / memspec.FTS_INDEX_DIRECTORY / _DECISION_CACHE_FILENAME
    old_manifest, cached, cursor = _read_cache(cache_path)
    old_cursor = cursor
    rulings = {key: value for key, value in cached.items() if key in paths}
    verified = {key: old_manifest.get(key) for key in rulings}
    changed = [key for key in paths if key not in cached or old_manifest.get(key) != manifest[key]]
    negatives = sorted(key for key in paths if key in cached and key not in changed
                       and not (isinstance(cached[key], dict) and cached[key].get(_KEY)))
    rotated = [key for key in negatives if key > cursor] + [key for key in negatives if key <= cursor]
    refreshed = set()
    cap = memspec.STOP_GATE_MAX_CARDS_PER_VAULT
    scanning = (changed + rotated)[:cap]
    if len(changed) > cap:
        # 輪替重驗的負例不算：它們早就認過了。要講的是「還沒認過的卡這回合沒認完」。
        defects.append(memspec.STOP_GATE_CAPPED_DEFECT.format(
            checked=cap, total=len(changed), vault=vault.name))
    for position, card_path in enumerate(scanning):
        if expired(started_at):
            # 這裡逾時比下面那個迴圈逾時更難看得出來：沒被認過的卡根本進不了候選名單，
            # 於是連「檢查了 N/M 張」的分母都不含它們——報出來會像全部檢查過。新卡與剛
            # 改過的卡正好都排在這裡，也就是最需要生效的那些。
            defects.append(memspec.STOP_GATE_UNSCANNED_DEFECT.format(
                skipped=len(scanning) - position, vault=vault.name))
            break
        try:
            rulings[card_path] = _read_decision(paths[card_path])
        except Exception:
            rulings[card_path] = None
        refreshed.add(card_path)
        verified[card_path] = manifest[card_path]
        if card_path in negatives:
            cursor = card_path

    found = []
    candidates = [key for key in sorted(rulings) if isinstance(rulings[key], dict) and rulings[key].get(_KEY)]
    # 上限只管「找」，不管「擋」。貴的是掃整個庫去分辨哪些卡帶著裁定——那件事有快取，
    # 找過就不必再找；已經找出來的卡再讀一次 frontmatter 很便宜。以前連執行都砍到上限，
    # 於是卡片一多，排在後面的就默默不生效，而且要靠「先被放行過一次」才拿得到優先權，
    # 是個死結（2026-09-17 對抗審查實測：5000 張卡裡 500 張武裝，每回合只有 12 張生效）。
    # 排序仍然有用：命中多的先讀，超時的時候先保住最會攔到東西的那些。
    candidates.sort(key=_priority_key(vault, rulings))
    for index, card_path in enumerate(candidates):
        if expired(started_at):
            # 少檢查幾張卡一定要出聲。以前逾時就靜靜回傳已經找到的部分，於是「這回合
            # 沒擋」跟「這回合沒檢查完」長得一模一樣。
            defects.append(memspec.STOP_GATE_INCOMPLETE_DEFECT.format(
                checked=index, total=len(candidates), vault=vault.name))
            break
        # 每一回合重讀：時間戳與大小證明不了內容沒變（把日期 01-02 改成 01-03，大小
        # 一模一樣）。這一段是 tests/stop_freshness_regression.py 釘住的線，省時間不
        # 能從這裡省——2026-09-19 試過信清單，那份測試當場擋下來。
        if card_path not in refreshed:
            try:
                rulings[card_path] = _read_decision(paths[card_path])
            except Exception:
                rulings[card_path] = None
            verified[card_path] = manifest[card_path]
        ruling = rulings[card_path]
        if expired(started_at):
            # 重讀這張卡的時候剛好逾時。`continue` 會在下一圈開頭補上缺陷行，但這張是
            # 最後一張時就沒有下一圈了——迴圈正常結束、零缺陷，跟「全部檢查完、沒有東西
            # 要擋」長得一模一樣。這一句是把那個最後一格補起來。
            defects.append(memspec.STOP_GATE_INCOMPLETE_DEFECT.format(
                checked=index, total=len(candidates), vault=vault.name))
            break
        decision = _decision_from_ruling(vault, card_path, ruling)
        if decision is not None:
            found.append(decision)
    if verified != old_manifest or rulings != cached or cursor != old_cursor:
        _write_cache(cache_path, verified, rulings, cursor)
    # 逾時要回「已經讀到的那些」，不是整批丟掉。以前逾時一律回空，整個庫零條生效，而
    # 上面那行「只檢查了 N/M 張」因此永遠是假的——實際是 0。已經讀出來的裁定是這一輪
    # 真正讀過的位元組，拿它們去比對只會多擋，不會亂擋。
    return found


def _priority_key(vault, rulings):
    """Order candidates by how often each ruling has actually fired lately.

    Fail-quiet on purpose: without the nightly health file every card scores the
    same and the order stays alphabetical, which is what it was before."""
    try:
        from epitype import compliance

        ranks = compliance.priority_rank(vault)
    except Exception:
        ranks = {}

    def key(card_path):
        ruling = rulings.get(card_path) or {}
        name = ruling.get(_KEY) if isinstance(ruling, dict) else None
        return ranks.get(name, (0, "")) + (card_path,)

    return key


def _strings(value):
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def _quoted_spans(message):
    """Character ranges of `message` that cite something rather than propose it:
    the owner's own quoting conventions (memspec.STOP_GATE_QUOTE_TEXT_REGEX) plus a
    Markdown blockquote line in full.

    U64: a Stop-gate report that cites a blocked phrase as evidence, or a
    write-gate script that defines it as a string literal, is not the model
    re-opening the ruling. Spans are merged so a hit is judged against one
    contiguous range rather than accidentally straddling two adjacent ones."""
    if not memspec.STOP_GATE_QUOTE_MASK_ENABLED:
        return []
    spans = [match.span() for match in memspec.STOP_GATE_QUOTE_TEXT_REGEX.finditer(message)]
    spans.extend(match.span() for match in memspec.STOP_GATE_BLOCKQUOTE_LINE_REGEX.finditer(message))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    # 引用是一句話，不是一段主張。太長的引用區段不遮——比例那一道的分母是模型自己
    # 寫的，多打二十個字就壓得下去，絕對長度壓不下去。
    merged = [
        (start, end) for start, end in merged
        if end - start <= memspec.STOP_GATE_QUOTE_SPAN_MAX_CHARS
    ]
    # 遮罩佔整則太多就整則不遮：引用一句話當證據佔比很小，把整段主張包進引號或改寫成
    # `>` 引用行佔比就很高，而後者是把靜音開關交給被管制的那一方（U64 要豁免的是引用，
    # 不是偽裝）。2026-09-17 對抗審查四種寫法全部繞過，這道比例上限是它的答案。
    body = len(message.strip())
    if body and sum(end - start for start, end in merged) / body > memspec.STOP_GATE_QUOTE_MASK_MAX_SHARE:
        merged = []
    # 圍籬程式碼區塊不受上面兩道上限管：它不是「把主張包進引號」，是交付物——要 owner
    # 貼到別處去跑的指令、要給另一個工具讀的提示詞、一份檔案的內容。長度上限那一道
    # 本來就整塊不算（memspec.TURN_LENGTH_FENCE_REGEX），這裡同一個判準。
    #
    # 誠實講代價：規則因此在圍籬裡看不見東西。接受它，是因為這一類誤擋 2026-09-19 一天
    # 出現八次，而「把話藏進程式碼區塊來規避」這件事，owner 看到的仍然是一塊程式碼。
    fences = [match.span() for match in memspec.TURN_LENGTH_FENCE_REGEX.finditer(message)]
    if not fences:
        return merged
    combined = sorted(merged + fences)
    result = []
    for start, end in combined:
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


_ACTION_DENIAL = re.compile(
    r"(?:不會|不能|不應|不該|不是|並非|沒有|未曾|並未|不打算|不再|拒絕)"
    r"(?:為了[^。！？!?\n]{0,80}|(?:再|去|再去)?)$"
)
_ACTION_DENIAL_BREAKS = ("。", "！", "？", "!", "?", "\n", "；", ";", "但", "可是", "然而", "卻", "反而", "不過")


def _negated_action(message, action_start, action_text, quoted):
    """Check a card-marked action against a nearby denial in the same clause."""
    base = max(0, action_start - 120)
    before = message[base:action_start]
    cut = max((position + len(mark) for mark in _ACTION_DENIAL_BREAKS
               if (position := before.rfind(mark)) >= 0), default=0)
    base += cut
    before = before[cut:]
    earlier = before.rfind(action_text)
    if earlier >= 0:
        base += earlier + len(action_text)
        before = before[earlier + len(action_text):]
    denial = _ACTION_DENIAL.search(before)
    return bool(denial and not any(
        quote_start <= base + denial.start() < quote_end
        for quote_start, quote_end in quoted
    ))


def _exceptions(vault):
    """Fragments the nightly review found produce a block that changed nothing.

    Fail-quiet: with no health file every pattern enforces its full range, which is
    what it did before the review existed."""
    try:
        from epitype import compliance

        return compliance.exceptions_for(vault)
    except Exception:
        return {}


def _demoted(vault):
    """自動降級已經拿掉，這裡永遠是空的。

    判準分不出「規則太寬」與「我一直犯這條」：2026-09-17 實跑，一條 6 次命中、6 次
    全部擋下、0 漏擋的規則被關了 14 天。留著這個函式是讓呼叫端不必分岔；不再匯入
    彩排模組，因為為了拿一個空集合，每一回合都要多載 8.3 ms。"""
    return frozenset()


def _forbidden_fragment(decision, message, defects, masked=None):
    """The matched fragment of the first usable `forbidden` pattern that fires
    outside a quoted citation (U64: see _quoted_spans) — a hit fully inside a
    quoted span is a citation, not a restatement; one outside still blocks,
    including a second, unquoted occurrence of a pattern already cited once.

    A pattern the shared validator rejects is dropped and named on stderr, never
    silently: an unusable pattern is a ruling that stopped being enforced, and the
    turn still ends rather than being blocked by a card nobody can fix."""
    masked = [] if masked is None else masked
    quoted = _quoted_spans(message)
    for pattern in decision.forbidden:
        # 這則訊息連這條規則的必要字面都沒有，命不了中，不必編譯它（省 43 ms／回合）。
        # 代價講明白：壞掉的樣式那一則缺陷行會延到「字面真的出現」的那一回合才報出來。
        if memspec.prefilter_misses(pattern, message):
            continue
        # A pattern that will not compile used to be dropped, which left the card
        # looking armed and enforcing nothing. It now falls back to matching the text
        # literally — narrower than any working pattern, so it cannot over-block —
        # and says so, because a rule quietly behaving differently is the failure
        # this gate exists to remove.
        regex, repaired = compile_pattern_or_literal(pattern)
        if regex is None or repaired:
            defects.append(
                memspec.STOP_GATE_PATTERN_DEFECT.format(
                    decision=decision.key,
                    pattern=_one_line(pattern)[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS],
                    reason=(memspec.STOP_GATE_PATTERN_REPAIRED_REASON if repaired
                            else memspec.STOP_GATE_PATTERN_UNUSABLE_REASON),
                )
            )
        if regex is None:
            continue
        for found in regex.finditer(message):
            if ("negatable_action" in regex.groupindex
                    and found.group("negatable_action") is not None
                    and _negated_action(message, found.start("negatable_action"),
                                        found.group("negatable_action"), quoted)):
                continue
            if any(start <= found.start() and found.end() <= end for start, end in quoted):
                # 遮罩放過的命中要留痕。以前這裡直接 continue，於是繞過去之後三個地方
                # 同時看不到：閘不擋、稽核沒紀錄、夜間重放也算不到。留一列之後，使用者
                # 查得到「這條規則被引用豁免放過幾次」。
                masked.append(_one_line(found.group(0))[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS])
                continue
            return _one_line(found.group(0)) or _one_line(pattern)
    return None


def _requirement_gap(decision, message, defects):
    """The trigger fragment when this turn owes the card's required text and lacks it.

    Whole classes of rule are "do this first", and whether I did it is invisible from
    outside. The conversion is to require that doing it leaves a mark in the message:
    the rule stops asking for the unobservable act and asks for the sentence that
    reports it. Omission then becomes checkable, and a fabricated mark is no longer a
    skipped step but a false statement, which the honesty floor already governs.

    A pattern the shared validator rejects is dropped and named on stderr: a
    requirement nobody can fix must not block every turn forever."""
    if not decision.require_when or not decision.require_text:
        return None
    # 觸發條件的必要字面都不在這則訊息裡，就不可能命中——連編譯都省下來。
    if memspec.prefilter_misses(decision.require_when, message):
        return None
    patterns = {}
    for field, pattern in (
        (memspec.REQUIRE_WHEN_FIELD, decision.require_when),
        (memspec.REQUIRE_TEXT_FIELD, decision.require_text),
    ):
        try:
            patterns[field] = compile_bounded_regex(pattern)
        except Exception as exc:
            defects.append(
                memspec.STOP_GATE_PATTERN_DEFECT.format(
                    decision=decision.key,
                    pattern=_one_line(pattern)[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS],
                    reason=f"{field}: {type(exc).__name__}: {exc}",
                )
            )
            return None
    quoted = _quoted_spans(message)

    def outside_quotes(regex):
        for found in regex.finditer(message):
            if any(start <= found.start() and found.end() <= end for start, end in quoted):
                continue
            return found
        return None

    triggered = outside_quotes(patterns[memspec.REQUIRE_WHEN_FIELD])
    if triggered is None:
        return None
    # 觸發條件要在引號外面找（引述別人的話不是我在主張），但**證據要在整則裡找**。
    # 證據常常就是一段引文——「owner 原話要附引號原文」這條的證據根本就是引號本身。
    # 2026-09-19 真的發生：一則正常長度、有三處引號的回報，附了原話仍被擋，因為遮罩
    # 把每個「…」連同括號一起遮掉，證據就消失了；同一句單獨送反而過（引用佔比超過
    # 上限、遮罩整則失效）。證據被引號遮掉的閘，會在訊息越規矩的時候越擋人。
    if patterns[memspec.REQUIRE_TEXT_FIELD].search(message) is not None:
        return None
    return _one_line(triggered.group(0))[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS]


def _turn_body(message):
    """去掉圍籬程式碼區塊的訊息本文。

    貼給 owner 的指令與程式不算「話多」，也不算「宣稱查過某個檔」——那是他要的東西。"""
    return memspec.TURN_LENGTH_FENCE_REGEX.sub(" ", message)


def _length_gap(decision, message, turn):
    """回合太長就回一句理由，否則 None。owner 這次要完整或詳細就不擋。"""
    limit = memspec.TURN_LENGTH_DEFAULT_LIMIT
    raw = str(decision.turn_check_limit or "").strip()
    if raw.isdigit() and int(raw) > 0:
        limit = int(raw)
    chars = len(_turn_body(message).strip())
    if chars <= limit:
        return None
    if memspec.TURN_LENGTH_EXEMPT_REGEX.search(turn.prompt or ""):
        return None
    return memspec.TURN_LENGTH_REASON.format(
        chars=chars, decision=_named(decision), limit=limit, advice=decision.advice)


def _cited_unread_gap(decision, message, turn, opened_names):
    """宣稱查過某個檔、而這一場沒有任何工具呼叫碰過它，就回一句理由。

    附記是空的時候一律不擋：那可能是動手閘沒註冊、或這一場真的還沒動過任何工具，跟
    「沒讀就答」長得一模一樣。分不出來的時候不擋人。

    `opened_names` 只收強證據（呼叫指名的目標）。弱證據——自由文字裡掃到的檔名——不能
    證明讀過，所以拿它來放行等於自己替自己背書。"""
    if not opened_names:
        return None
    body = _turn_body(message)
    seen = set()
    # 宣稱要跟檔名綁在同一句。2026-09-20 Codex 審查抓到：整段只要有一個「查過」，同一段
    # 裡任何檔名都會被拿來判——「我查過 alpha.py。接下來要讀 beta.py，還沒讀。」因此
    # 被擋，而那句話本身是對的。
    for sentence in _SENTENCE_REGEX.findall(body):
        claim = memspec.TURN_CITED_CLAIM_REGEX.search(sentence)
        if claim is None:
            continue
        for match in memspec.TURN_CITED_PATH_REGEX.finditer(sentence):
            name = match.group(0).replace("\\", "/").rsplit("/", 1)[-1].casefold()
            if not name or name in seen:
                continue
            seen.add(name)
            if len(seen) > memspec.TURN_CITED_MAX_PATHS:
                return None
            if name in memspec.TURN_CITED_GENERIC_NAMES or name in opened_names:
                continue
            return memspec.TURN_CITED_UNREAD_REASON.format(
                claim=claim.group(0), path=name, decision=_named(decision),
                advice=decision.advice)
    return None


def _unverified_delegation_gap(decision, message, turn):
    """收到派工結果、自己一次手都沒動就宣稱完成，就回一句理由。

    責任不會跟著派工一起派出去：子代理只回傳界線內的結果，驗證、整合、交付還是主責的。
    子代理說話那一側沒有閘門（2026-09-19 實測），所以這一條擋在我這一側。"""
    if not turn.dispatch or turn.calls_after_dispatch:
        return None
    body = _turn_body(message)
    claim = memspec.TURN_DONE_CLAIM_REGEX.search(body)
    if claim is None:
        return None
    if memspec.TURN_OWNERSHIP_TEXT_REGEX.search(body):
        return None
    return memspec.TURN_UNVERIFIED_DELEGATION_REASON.format(
        source=turn.dispatch, claim=claim.group(0), decision=_named(decision),
        advice=decision.advice)


def _prompt_block_gap(decision, message, turn):
    """A requested pasteable prompt must be present in the chat message."""
    if not re.search(memspec.TURN_PROMPT_REQUEST_PATTERN, turn.prompt or "", re.IGNORECASE):
        return None
    for opening in re.finditer(r"(?m)^[ \t]*(`{3,})[^\n]*\n", message):
        marker = opening.group(1)
        tail = message[opening.end():]
        closing = re.search(r"(?m)^[ \t]*" + re.escape(marker) + r"[ \t]*$", tail)
        if closing and tail[:closing.start()].strip():
            return None
    return memspec.TURN_PROMPT_BLOCK_REASON.format(advice=decision.advice)


def _turn_check_gap(decision, message, turn, opened_names, defects):
    """卡片指名的內建檢查。字面比對看不到的事實，由這裡判。"""
    name = decision.turn_check
    if not name:
        return None
    if name not in memspec.TURN_CHECK_NAMES:
        defects.append(memspec.TURN_CHECK_UNKNOWN_DEFECT.format(
            decision=decision.key, name=name,
            known=memspec.LIST_JOINER.join(memspec.TURN_CHECK_NAMES)))
        return None
    if name == memspec.TURN_CHECK_LENGTH:
        return _length_gap(decision, message, turn)
    if name == memspec.TURN_CHECK_UNVERIFIED_DELEGATION:
        return _unverified_delegation_gap(decision, message, turn)
    if name == memspec.TURN_CHECK_PROMPT_BLOCK:
        return _prompt_block_gap(decision, message, turn)
    return _cited_unread_gap(decision, message, turn, opened_names)


def _require_reason(decision, trigger):
    """配對要求被擋下來時要講的那一句：短、看得懂、不印樣式原文。

    這段字 owner 也看得到，而且每擋一次就進一次上下文。列得出字面選項就只列選項；
    列不出來（樣式全是符號）才退回附上卡片描述。"""
    items, more = memspec.readable_alternatives(decision.require_text)
    if items:
        expected = memspec.REQUIRE_HINT_TEMPLATE.format(
            items=memspec.LIST_JOINER.join(items)
            + (memspec.REQUIRE_HINT_MORE if more else ""))
        advice = ""
    else:
        expected = memspec.REQUIRE_HINT_FALLBACK
        advice = decision.advice[: memspec.STOP_GATE_REASON_QUOTE_MAX_CHARS]
    return memspec.STOP_GATE_REQUIRE_REASON.format(
        trigger=trigger, expected=expected, advice=advice)


def _is_question(sentence):
    text = sentence.strip()
    return text.endswith(memspec.STOP_GATE_QUESTION_ENDINGS) or any(
        marker in text for marker in memspec.STOP_GATE_QUESTION_MARKERS
    )


def _asks_again(decision, message):
    """True when one question sentence names this same ruling by two of its aliases.

    Two aliases of one card, not one: a single shared word ("虛擬") appears in
    unrelated questions, while「虛擬盤」and「鏡像實盤」together in a question is the
    settled subject being put back to the owner. Aliases belonging to different cards
    never add up."""
    # Only a sourced, explicit owner ruling can make a question "already decided".
    if decision.decided_by != memspec.OWNER_EXPLICIT_DECIDER or not decision.quote:
        return False
    aliases = tuple(
        alias
        for alias in dict.fromkeys(_normalized(item) for item in decision.aliases)
        if len(alias) >= memspec.STOP_GATE_MIN_ALIAS_CHARS
    )
    if len(aliases) < memspec.STOP_GATE_ALIAS_HITS:
        return False
    for match in _SENTENCE_REGEX.finditer(message):
        sentence = match.group(0)
        if not sentence.strip() or not _is_question(sentence):
            continue
        haystack = _normalized(sentence)
        if sum(1 for alias in aliases if alias in haystack) >= memspec.STOP_GATE_ALIAS_HITS:
            return True
    return False


def _named(decision):
    if decision.decided_at:
        return memspec.STOP_GATE_DECISION_NAMED.format(
            key=decision.key, decided_at=decision.decided_at)
    return decision.key


def _best_effort_audit(callback, *arguments):
    try:
        callback(*arguments)
    except Exception:
        pass


def _message_digest(message):
    """Short digest of a turn's text — the same value the nightly replay computes."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    return hashlib.sha256(str(message).encode("utf-8", errors="replace")).hexdigest()[:16]


def _claim_marker(session_id, decision_key, message):
    """One block per (session, decision, message); the marker lives in the recall
    marker directory so PreCompact's clear_recall_markers drops it with the rest.

    A marker that cannot be written must not silence the ruling, so a filesystem
    error claims the block rather than swallowing it."""
    # 用到才載入：hashlib 要 6 ms，絕大多數呼叫走不到這裡。
    import hashlib

    if not session_id:
        return True
    digest = hashlib.sha256(
        (str(decision_key) + "\0" + str(message)).encode("utf-8", errors="replace")
    ).hexdigest()
    try:
        directory = recall_marker_directory(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / (memspec.STOP_GATE_MARKER_PREFIX + digest)
        with marker.open("x", encoding="ascii") as stream:
            stream.write(digest + "\n")
    except FileExistsError:
        return False
    except OSError:
        return True
    return True


def _audit(config, decision_key, rule, started_at, session_id=None, digest="",
           kind=memspec.STOP_GATE_LOG_KIND):
    """The blocked message's digest goes on the row, never the message.

    The nightly replay has to tell "this ruling let something through" from "the turn
    was stopped by a different ruling and rewritten" — the gate reports one verdict
    per turn and stops, so without the digest every second violation in a blocked
    message reads as a miss."""
    try:
        row = {
            "kind": kind,
            "decision": decision_key,
            "rule": rule,
            "digest": digest,
        }
        append_gate_log(
            governance_vault(config, for_write=True),
            with_session(row, session_id),
            started_at,
        )
    except Exception:
        pass


def _vaults(config, event):
    """The cwd's own vault(s) first, then the governance vault holding the ledger."""
    vaults = list(native_cwd_vaults(event.get("cwd") if isinstance(event, dict) else None))
    governance = governance_vault(config)
    if governance not in vaults:
        vaults.append(governance)
    return vaults


def _turn_texts(transcript_path):
    """Assistant text blocks since the last real user prompt, in order; [] if unreadable."""
    return _turn_context(transcript_path).texts


def _turn_context(transcript_path):
    """這一回合說了什麼，以及 owner 這次問的是什麼。

    要 owner 的原話，是因為有些檢查只在「他沒要求這樣」的時候才成立：報告長度上限就是
    一例——他自己點了完整或詳細，長就是他要的。

    Tool results are logged as user rows but do not end a turn (same rule as the
    nightly replay's `compliance._turns`)."""
    try:
        path = Path(str(transcript_path))
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(max(0, size - memspec.STOP_GATE_TURN_TAIL_BYTES))
            lines = stream.read().decode("utf-8", "replace").splitlines()
    except (OSError, ValueError, TypeError):
        return _turn()
    from epitype import transcript as transcript_reader

    texts = []
    prompt = ""
    # 派工的結果進來之後，我自己還動過幾次手。倒著走，遇到派工那一次呼叫就凍結計數。
    dispatch = ""
    calls_after = 0
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        parts = transcript_reader.turn_parts(row)
        if parts is None:
            continue
        kind, row_texts, tools = parts
        if kind == transcript_reader.TOOL_RESULT:
            # 工具回傳記在真人那一側，但它不結束一個回合。
            continue
        if kind == transcript_reader.USER:
            prompt = " ".join(piece for piece in row_texts if piece)
            break
        for name, _payload in reversed(tools):
            if dispatch:
                break
            if name.casefold() in memspec.TURN_DISPATCH_TOOLS:
                dispatch = name
            else:
                calls_after += 1
        for piece in reversed(row_texts):
            if str(piece or "").strip():
                texts.append(piece)
    texts.reverse()
    prompt = prompt[: memspec.STOP_GATE_MESSAGE_MAX_CHARS]
    if not dispatch and memspec.TURN_DISPATCH_NOTICE_MARKER in prompt:
        # 背景子代理跑完是以一則新提問的形式回來的，這一回合裡沒有派工那一次呼叫。
        dispatch = memspec.TURN_DISPATCH_NOTICE_MARKER
    return _Turn(texts, prompt, dispatch, calls_after)


def _shingles(text):
    """一段文字的重疊比對單位。用固定長度切片，不用 difflib——它要 O(n²)，而這在熱路徑。"""
    body = _WHITESPACE_REGEX.sub("", str(text or ""))[: memspec.BLOCKED_ECHO_MAX_CHARS]
    size = memspec.BLOCKED_ECHO_SHINGLE
    return {body[index:index + size] for index in range(0, max(0, len(body) - size + 1))}


def _echo_state_path(config, session_id):
    session = "".join(char for char in str(session_id or "") if char.isalnum() or char in "-_")
    if not session:
        return None
    return (Path(governance_vault(config, for_write=True)) / memspec.FTS_INDEX_DIRECTORY
            / memspec.BLOCKED_ECHO_STATE_DIRECTORY / (session + ".txt"))


def _remember_blocked(config, session_id, message):
    """把被擋的那一段留下來，下一次好比對。壞了就算了——這是紀錄，不是關卡。"""
    path = _echo_state_path(config, session_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(message)[: memspec.BLOCKED_ECHO_MAX_CHARS], encoding="utf-8")
    except OSError:
        pass


def _echo_overlap(config, session_id, message):
    """這一段跟上次被擋那一段有多像（0–1）；沒有上一段就回 0。"""
    path = _echo_state_path(config, session_id)
    if path is None:
        return 0.0
    try:
        previous = path.read_text(encoding="utf-8")
    except OSError:
        return 0.0
    before = _shingles(previous)
    now = _shingles(message)
    if len(before) < memspec.BLOCKED_ECHO_MIN_SHINGLES or not now:
        return 0.0
    return len(before & now) / len(before)


def _handle(event, started_at, defects):
    message = event.get("last_assistant_message")
    if event.get("stop_hook_active"):
        # 這是被擋之後的重寫。規則不在這一輪重判（會無限迴圈），但有一件事只有這一輪
        # 看得到：重寫出來的東西跟剛才被擋那一段是不是幾乎一樣。owner 已經看過那一段，
        # 整段重貼等於同一段話給他看兩次。
        if not isinstance(message, str) or not message.strip():
            return None
        config = load_config(started_at)
        if config is None:
            return None
        session_id = event.get("session_id", event.get("sessionId"))
        overlap = _echo_overlap(config, session_id, message)
        if overlap < memspec.BLOCKED_ECHO_MAX_OVERLAP:
            return None
        if not _claim_marker(session_id, memspec.BLOCKED_ECHO_DECISION, message):
            return None
        # 不擋：重複的那一段已經在 owner 眼前，再擋一輪只會生出第三段。記帳＋下一則提醒。
        _audit(config, memspec.BLOCKED_ECHO_DECISION, memspec.BLOCKED_ECHO_DECISION, started_at,
               session_id, _message_digest(message), kind=memspec.STOP_NOTE_LOG_KIND)
        _best_effort_audit(leave_note, config, session_id,
                           memspec.BLOCKED_ECHO_REASON.format(overlap=overlap))
        return None
    if not isinstance(message, str) or not message.strip():
        return None
    # Mid-turn text reaches the owner too; reading only the last message let every card
    # miss whatever was said before a tool call.
    turn = _turn_context(event.get("transcript_path")) if event.get("transcript_path") else _turn()
    texts = list(turn.texts)
    if message.strip() not in memspec.join_turn_text(texts):
        texts.append(message)
    message = memspec.join_turn_text(texts)
    config = load_config(started_at)
    if config is None:
        # load_config 只在期限已過時回 None（其他壞法都丟例外）。放行照舊，但要講出來。
        defects.append(memspec.STOP_GATE_EXPIRED_BEFORE_CHECK_DEFECT)
        return None
    return _verdict(event, message, config, started_at, defects, turn)


def _verdict(event, message, config, started_at, defects, turn=None):
    decisions = []
    vaults = _vaults(config, event)
    for index, vault in enumerate(vaults):
        if expired(started_at):
            defects.append(memspec.STOP_GATE_INCOMPLETE_DEFECT.format(
                checked=index, total=len(vaults), vault=memspec.VAULT_WORD))
            break
        decisions.extend(_decisions(vault, started_at, defects))
    if not decisions:
        return None

    # A forbidden hit outranks a repeated question: the model already said the thing
    # the owner ruled out, which is the harder violation of the two.
    excepted = {}
    demoted = set()
    for vault in _vaults(config, event):
        for card, digests in _exceptions(vault).items():
            excepted.setdefault(card, set()).update(digests)
        demoted |= _demoted(vault)
    decisions = [decision for decision in decisions if decision.key not in demoted]
    # 只管寫檔內容的裁定不在回合結束比對：那一類講的是檔案裡不該出現什麼，不是我不該說什麼。
    decisions = [
        decision for decision in decisions
        if decision.applies_to != memspec.APPLIES_TO_WRITE
    ]
    # 自動放行認的是「這一則訊息」，不是「那串字」：同一句無害的話不再被重複擋下，而
    # 任何別的訊息照擋。放行一串字會把字面規則整條關掉，那是 2026-09-17 審查實跑出來的。
    digest = _message_digest(message)
    decisions = [
        decision for decision in decisions
        if digest not in excepted.get(decision.key, frozenset())
    ]
    session_id = event.get("session_id", event.get("sessionId"))
    blocking = [decision for decision in decisions if decision.on_hit != memspec.ON_HIT_NOTE]
    verdict = _first_violation(blocking, event, message, config, started_at, defects, turn)
    if verdict is None:
        # 只提醒的卡排在後面：真的要擋的時候，那一輪重寫本來就會把用詞一起帶過。
        noting = [decision for decision in decisions if decision.on_hit == memspec.ON_HIT_NOTE]
        noted = _first_violation(noting, event, message, config, started_at, defects, turn)
        if noted is not None and _claim_marker(session_id, noted[0].key, message):
            _audit(config, noted[0].key, noted[1], started_at, session_id,
                   _message_digest(message), kind=memspec.STOP_NOTE_LOG_KIND)
            # 擋人用的那句話寫著「請改寫」；提醒要講的正好相反，所以字面規則另給一句短的。
            _best_effort_audit(leave_note, config, session_id, (
                memspec.STOP_NOTE_FORBIDDEN.format(fragment=noted[3], decision=noted[0].key)
                # 其他類的理由取第一句就夠：提醒是要我下次照做，不是要我讀完整段說明。
                if noted[3]
                else noted[2].split(memspec.SENTENCE_END)[0] + memspec.SENTENCE_END))
        return None
    decision, rule, reason, _fragment = verdict
    if not _claim_marker(session_id, decision.key, message):
        return None
    _audit(config, decision.key, rule, started_at, session_id, _message_digest(message))
    # 留著這一段，下一輪才比得出「重寫的東西跟 owner 已經看過的那一段一不一樣」。
    _best_effort_audit(_remember_blocked, config, session_id, message)
    return {"decision": "block", "reason": reason + memspec.STOP_GATE_REWRITE_HINT}


# 開頭圍籬：行首（可縮排，清單裡的區塊會縮）三個以上反引號或波浪號，後面第一個詞是語言
# 標記。反引號圍籬的標記裡不能再有反引號（CommonMark），所以一行裡的 ```bash``` 不算。
_FENCE_OPEN_REGEX = re.compile(r"^([ \t]*)(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$")
_SHELL_ERROR_LINE_REGEX = re.compile(r"\bline (\d+)|:\s*(\d+):")
# 一個 powershell.exe 解析全部區塊，只解析、不執行。輸入是 stdin 上的 JSON（全 ASCII，
# 中文走 \u 跳脫），輸出每區塊一行；錯誤訊息轉成 UTF-8 的 base64，避開主控台字碼頁。
_POWERSHELL_PARSE_SCRIPT = r"""
$ProgressPreference = 'SilentlyContinue'
$raw = [Console]::In.ReadToEnd()
$blocks = @((ConvertFrom-Json -InputObject $raw).blocks)
$lines = @()
for ($i = 0; $i -lt $blocks.Count; $i++) {
  $tokens = $null; $errors = $null
  [void][System.Management.Automation.Language.Parser]::ParseInput([string]$blocks[$i], [ref]$tokens, [ref]$errors)
  if ($errors.Count -gt 0) {
    $message = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$errors[0].Message))
    $lines += "$i`t$($errors[0].Extent.StartLineNumber)`t$message"
  } else {
    $lines += "$i`tok"
  }
}
[Console]::Out.Write(($lines -join "`n"))
"""
# 同一回合、同一殼層、同一批區塊只解析一次：兩張卡都宣告 powershell 時不起兩個行程。
_FENCE_RESULTS = {}


def _shell_fences(message):
    """[(第幾個圍籬區塊, 語言標記, 內容)]，只收有語言標記的；未閉合的一路到結尾。"""
    lines = str(message or "").splitlines()
    fences = []
    ordinal = 0
    index = 0
    while index < len(lines):
        opening = _FENCE_OPEN_REGEX.match(lines[index])
        index += 1
        if opening is None:
            continue
        indent, marker, lang = opening.groups()
        ordinal += 1
        body = []
        while index < len(lines):
            line = lines[index]
            index += 1
            stripped = line.strip()
            if stripped and set(stripped) == {marker[0]} and len(stripped) >= len(marker):
                break
            body.append(line[len(indent):] if line.startswith(indent) else line.lstrip())
        if lang:
            fences.append((ordinal, lang.casefold(), "\n".join(body)))
    return fences


def _powershell_executable():
    # 指名 Windows PowerShell 5.1 本尊：PATH 上的 powershell 可能被別的東西蓋掉，而 owner
    # 按執行跑的是這一支。找不到才交給 PATH，那裡也沒有就是 FileNotFoundError → 放行。
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
    if root:
        candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return os.fspath(candidate)
    return "powershell.exe"


def _parse_powershell(blocks, timeout, executable=None):
    import base64
    import subprocess

    encoded = base64.b64encode(_POWERSHELL_PARSE_SCRIPT.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [executable or _powershell_executable(), "-NoLogo", "-NoProfile", "-NonInteractive",
         "-EncodedCommand", encoded],
        input=json.dumps({"blocks": blocks}, ensure_ascii=True),
        capture_output=True, text=True, encoding="ascii", errors="replace",
        timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"powershell exit {result.returncode}")
    parsed = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[1] == "ok":
            parsed[int(parts[0])] = None
        elif len(parts) == 3:
            text = base64.b64decode(parts[2]).decode("utf-8", errors="replace")
            parsed[int(parts[0])] = (int(parts[1]), _one_line(text))
    if sorted(parsed) != list(range(len(blocks))):
        raise ValueError("parser output malformed")
    return [parsed[position] for position in range(len(blocks))]


def _parse_posix(shell, blocks, timeout, executable=None):
    import shutil
    import subprocess

    executable = executable or shutil.which(shell)
    if not executable:
        raise FileNotFoundError(shell)
    deadline = time.monotonic() + timeout
    results = []
    for body in blocks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(shell)
        result = subprocess.run(
            [executable, "-n"], input=body, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=remaining, check=False,
        )
        if result.returncode == 0:
            results.append(None)
            continue
        # 語法錯要看殼層自己的診斷；殼層根本沒跑起來（126／127，或 Windows 上 PATH 裡的
        # bash 是沒裝發行版的 WSL 啟動器）要放行，不能當成語法錯擋人。
        first = next(
            (line for line in result.stderr.splitlines()
             if re.search(memspec.FENCE_SHELL_POSIX_SYNTAX_PATTERN, line, re.IGNORECASE)),
            "",
        )
        if result.returncode in memspec.FENCE_SHELL_POSIX_UNRUNNABLE_CODES or not first:
            raise RuntimeError(f"{shell} exit {result.returncode}")
        found = _SHELL_ERROR_LINE_REGEX.search(first)
        line_number = int(next(group for group in found.groups() if group)) if found else 1
        results.append((line_number, _one_line(first)))
    return results


def _parse_shell_blocks(shell, blocks, timeout, executable=None):
    """每個區塊的第一個語法錯誤 (行號, 訊息)，沒錯回 None。解析器出事一律丟例外。"""
    if shell == memspec.FENCE_SHELL_POWERSHELL:
        return _parse_powershell(blocks, timeout, executable)
    return _parse_posix(shell, blocks, timeout, executable)


def _fence_shell_gap(decision, message, event, started_at, defects, parser=None):
    """宣告了 fence_shell 的卡：這回合的指令區塊能不能被那個殼層解析。能擋就回理由。

    只驗語法、不執行。解析器不存在、逾時、出例外一律放行，並在 stderr 留一行。"""
    shell = decision.fence_shell
    if not shell:
        return None
    # 子代理的回報是給主線讀的，owner 不會在那裡按執行。
    if isinstance(event, dict) and event.get("hook_event_name") == memspec.SUBAGENT_STOP_EVENT:
        return None
    if shell not in memspec.FENCE_SHELL_VALUES:
        defects.append(memspec.STOP_GATE_FENCE_SHELL_SKIPPED_DEFECT.format(
            decision=decision.key,
            reason=memspec.STOP_GATE_FENCE_SHELL_UNKNOWN_REASON.format(
                value=shell, allowed="/".join(memspec.FENCE_SHELL_VALUES))))
        return None
    langs = frozenset(decision.fence_langs or memspec.FENCE_SHELL_DEFAULT_LANGS)
    fences = [fence for fence in _shell_fences(message) if fence[1] in langs]
    if not fences:
        return None
    if len(fences) > memspec.FENCE_SHELL_MAX_BLOCKS:
        defects.append(memspec.STOP_GATE_FENCE_SHELL_TRUNCATED_DEFECT.format(
            decision=decision.key, checked=memspec.FENCE_SHELL_MAX_BLOCKS, total=len(fences)))
        fences = fences[: memspec.FENCE_SHELL_MAX_BLOCKS]
    blocks = [body for _ordinal, _lang, body in fences]
    cache_key = (shell, tuple(blocks))
    results = _FENCE_RESULTS.get(cache_key)
    if results is None:
        timeout = min(memspec.FENCE_SHELL_TIMEOUT_SECONDS, remaining_seconds(started_at))
        try:
            if timeout <= 0:
                raise TimeoutError("hook deadline reached")
            results = (parser or _parse_shell_blocks)(shell, blocks, timeout)
        except Exception as exc:
            reason = type(exc).__name__
            defects.append(memspec.STOP_GATE_FENCE_SHELL_SKIPPED_DEFECT.format(
                decision=decision.key, reason=reason))
            _FENCE_RESULTS[cache_key] = reason
            return None
        _FENCE_RESULTS[cache_key] = results
    if isinstance(results, str):
        return None
    display = memspec.FENCE_SHELL_DISPLAY[shell]
    for (ordinal, lang, body), error in zip(fences, results):
        if error is None:
            continue
        line_number, text = error
        body_lines = body.splitlines()
        snippet = body_lines[line_number - 1] if 0 < line_number <= len(body_lines) else ""
        return memspec.STOP_GATE_FENCE_SHELL_REASON.format(
            index=ordinal, lang=lang, shell=display, line=line_number,
            snippet=_one_line(snippet)[: memspec.FENCE_SHELL_SNIPPET_MAX_CHARS],
            # 解析器訊息自帶句點，句子的收尾交給樣板。
            error=text.rstrip("。. ")[: memspec.FENCE_SHELL_ERROR_MAX_CHARS],
            decision=_named(decision),
        )
    return None


def _first_violation(decisions, event, message, config, started_at, defects, turn=None):
    """這批裁定裡第一個被違反的：(裁定, 哪一類, 要說的話, 命中的字)；都沒有回 None。"""
    verdicts = []
    for decision in decisions:
        masked = []
        fragment = _forbidden_fragment(decision, message, defects, masked)
        if masked:
            # 豁免也是一件發生過的事，要進帳。內容本身仍然不記，只記卡名與命中片段。
            _best_effort_audit(
                append_gate_log,
                governance_vault(config, for_write=True),
                with_session(
                    {"kind": memspec.STOP_GATE_MASKED_LOG_KIND, "decision": decision.key,
                     "rule": "quoted", "fragment": masked[0],
                     "digest": _message_digest(message)},
                    event.get("session_id", event.get("sessionId")),
                ),
                started_at,
            )
        if fragment is not None:
            verdicts.append(
                (
                    decision,
                    _FORBIDDEN_RULE,
                    memspec.STOP_GATE_FORBIDDEN_REASON.format(
                        decision=_named(decision),
                        # 描述只取開頭：這段字 owner 也看得到、每擋一次進一次上下文。
                        quote=(decision.advice or decision.quote)[
                            : memspec.STOP_GATE_REASON_QUOTE_MAX_CHARS],
                        fragment=fragment[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS],
                    ),
                    fragment[: memspec.STOP_GATE_FRAGMENT_MAX_CHARS],
                )
            )
            break
    if not verdicts:
        # 字面比對看不到的那兩件事：這回合的話有多長、引的檔這一場有沒有被打開過。
        # 只有卡片指名了內建檢查才跑，而且附記讀得到才比對。
        turn = turn if turn is not None else _turn()
        opened_names = frozenset()
        if any(decision.turn_check == memspec.TURN_CHECK_CITED_UNREAD for decision in decisions):
            try:
                from epitype import opened as opened_module

                # 只收強證據：自由文字裡提到一個檔名不是讀過它（epitype/opened.py）。
                opened_names = opened_module.strong_names(
                    governance_vault(config),
                    event.get("session_id", event.get("sessionId")),
                )
            except Exception:
                opened_names = frozenset()
        for decision in decisions:
            reason = _turn_check_gap(decision, message, turn, opened_names, defects)
            if reason is None:
                continue
            verdicts.append((decision, _TURN_CHECK_RULE, reason, ""))
            break
    if not verdicts:
        # 圍籬區塊在上面的字面比對裡是遮掉的（2026-09-19）；這一道不看字面，直接問 owner
        # 按執行時會跑它的那個殼層「這段合不合法」。沒有卡宣告殼層、或沒有相符的區塊時，
        # 一個行程都不起。
        for decision in decisions:
            reason = _fence_shell_gap(decision, message, event, started_at, defects)
            if reason is None:
                continue
            verdicts.append((decision, memspec.FENCE_SHELL_RULE, reason, ""))
            break
    if not verdicts:
        for decision in decisions:
            trigger = _requirement_gap(decision, message, defects)
            if trigger is None:
                continue
            verdicts.append(
                (
                    decision,
                    _REQUIRE_RULE,
                    _require_reason(decision, trigger),
                    "",
                )
            )
            break
    if not verdicts:
        for decision in decisions:
            if not _asks_again(decision, message):
                continue
            template = (
                memspec.STOP_GATE_QUESTION_REASON
                if decision.decided_at
                else memspec.STOP_GATE_QUESTION_REASON_UNDATED
            )
            verdicts.append(
                (
                    decision,
                    _QUESTION_RULE,
                    template.format(decided_at=decision.decided_at, quote=decision.advice or decision.quote),
                    "",
                )
            )
            break
    return verdicts[0] if verdicts else None


def _selftest():
    import tempfile
    import uuid

    checks = []
    sessions = []
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-stopgate-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / "vault"
            vault.mkdir()
            (vault / "mirror.md").write_text(
                "---\n"
                "name: 虛擬盤鏡像裁定\n"
                "description: 虛擬盤與實盤參數一致\n"
                f"{memspec.DECISION_KEY_FIELD}: virtual-mirrors-live\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-13\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 虛擬必須鏡像實盤\n"
                f"{memspec.ALIASES_FIELD}:\n  - 虛擬盤\n  - 鏡像實盤\n  - virtual mirror\n"
                f"{memspec.FORBIDDEN_FIELD}:\n  - 虛擬盤(?:先)?用不同參數\n  - 兩套參數\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            (vault / "schedule.md").write_text(
                "---\n"
                "name: 排程裁定\n"
                "description: 排程由 owner 決定\n"
                f"{memspec.DECISION_KEY_FIELD}: schedule-owner-only\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-08-20\n"
                f"{memspec.DECIDED_BY_FIELD}: {memspec.OWNER_EXPLICIT_DECIDER}\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 排程只由我改\n"
                f"{memspec.ALIASES_FIELD}: [排程甲, 排程乙]\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            (vault / "retired.md").write_text(
                "---\n"
                "name: 舊制\n"
                "description: 已作廢\n"
                f"{memspec.DECISION_KEY_FIELD}: retired-rule\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.SUPERSEDED_DECISION_STATUS}\n"
                f"{memspec.SUPERSEDED_BY_FIELD}: mirror.md\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-01-01\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 舊制原話\n"
                f"{memspec.ALIASES_FIELD}: [退休甲, 退休乙]\n"
                f"{memspec.FORBIDDEN_FIELD}: [退休禁詞]\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            config = root / "config.json"
            write_config(config, [vault])

            def synthetic(event, config_file, environment=None, clock=memspec.HOOK_CLOCK_FROZEN):
                # 判斷案例一律用凍住的時鐘：機器再忙，期限也不會替判斷做決定。逾時放行
                # 另有一題用 expired 明確斷言，兩件事不再混在同一個結果裡。
                # 時鐘凍住之後，子行程不再在期限到時自己收手，原本跟期限綁在一起的 19 秒
                # 上限就沒有依據了（96 個忙迴圈下單次實測 18 秒）。這個上限只抓卡死。
                return run_synthetic(
                    Path(__file__),
                    event,
                    config_file,
                    environment=environment,
                    timeout=_SELFTEST_HANG_SECONDS,
                    clock=clock,
                )

            def gate_rows():
                # 帳本不存在就是零列：缺帳是一題具名的失敗，不是把整份自測炸成 ERROR。
                target = vault / memspec.GATE_LOG_FILENAME
                if not target.exists():
                    return []
                return [
                    json.loads(line)
                    for line in target.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]

            def run(message, session=None, extra=None):
                session_id = session or f"stopgate-{uuid.uuid4().hex}"
                if session_id not in sessions:
                    sessions.append(session_id)
                event = {
                    "session_id": session_id,
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                    "last_assistant_message": message,
                }
                event.update(extra or {})
                # An event carrying cwd makes every ancestor look for a same-slug
                # native vault under home; without this the run would pull the
                # test machine's real vaults into a synthetic case.
                result = synthetic(
                    event,
                    config,
                    environment={"HOME": os.fspath(root), "USERPROFILE": os.fspath(root)},
                )
                value = json.loads(result.stdout) if result.stdout.strip() else {}
                return result, value, session_id

            forbidden_result, forbidden_value, forbidden_session = run(
                "我建議虛擬盤先用不同參數跑一週再說。"
            )
            log_rows = gate_rows()
            checks.append((
                "a forbidden phrase is blocked and audited as stop_block",
                forbidden_result.returncode == 0
                and forbidden_value.get("decision") == "block"
                and "虛擬盤與實盤參數一致" in forbidden_value.get("reason", "")
                and "virtual-mirrors-live，2026-08-13" in forbidden_value.get("reason", "")
                and "虛擬盤先用不同參數" in forbidden_value.get("reason", "")
                and any(
                    row.get("kind") == memspec.STOP_GATE_LOG_KIND
                    and row.get("decision") == "virtual-mirrors-live"
                    and row.get("rule") == _FORBIDDEN_RULE
                    for row in log_rows
                ),
            ))
            checks.append((
                "the stop_block audit row carries the event's session_id",
                any(
                    row.get("kind") == memspec.STOP_GATE_LOG_KIND
                    and row.get("decision") == "virtual-mirrors-live"
                    and row.get("session_id") == forbidden_session
                    for row in log_rows
                ),
            ))
            checks.append((
                "the blocking payload carries decision and reason and nothing else",
                set(forbidden_value) == {"decision", "reason"}
                and len(forbidden_result.stdout.encode("utf-8")) <= memspec.HOOK_MAX_OUTPUT_BYTES
                and not forbidden_result.stderr,
            ))

            # U64: citing a forbidden phrase as evidence is not re-proposing it.
            quoted_result, quoted_value, _ = run(
                "實測表：「虛擬盤先用不同參數」是被擋下的例子，僅供舉例說明。"
            )
            checks.append((
                "forbidden 落在中文引號「」內是引用，不算再提議",
                quoted_result.returncode == 0
                and not quoted_result.stdout.strip()
                and not quoted_value,
            ))

            backtick_result, backtick_value, _ = run(
                "程式碼片段：`兩套參數` 只是這裡的變數命名範例，不是提議。"
            )
            checks.append((
                "forbidden 落在反引號內是引用，不算再提議",
                backtick_result.returncode == 0
                and not backtick_result.stdout.strip()
                and not backtick_value,
            ))

            blockquote_result, blockquote_value, _ = run(
                "引用回顧：\n> 虛擬盤先用不同參數跑一週再說\n以上是先前被擋下的話，現在已經照裁定改了。"
            )
            checks.append((
                "forbidden 整行落在 > 引用行內是引用，不算再提議",
                blockquote_result.returncode == 0
                and not blockquote_result.stdout.strip()
                and not blockquote_value,
            ))

            table_result, table_value, _ = run(
                "| 案例 | 結果 |\n| --- | --- |\n| 「虛擬盤先用不同參數」 | 擋下 |\n"
            )
            checks.append((
                "forbidden 落在表格儲存格的引號內是引用，不算再提議",
                table_result.returncode == 0
                and not table_result.stdout.strip()
                and not table_value,
            ))

            mixed_result, mixed_value, _ = run(
                "owner 說過「虛擬盤先用不同參數」不行，但我還是想虛擬盤先用不同參數看看。"
            )
            checks.append((
                "同訊息一次加引號、一次沒加：沒加引號那次仍照擋",
                mixed_result.returncode == 0
                and mixed_value.get("decision") == "block"
                and "虛擬盤與實盤參數一致" in mixed_value.get("reason", "")
                and "虛擬盤先用不同參數" in mixed_value.get("reason", ""),
            ))

            contextual = namedtuple("_ContextualProbe", "key forbidden")(
                "negated-action",
                (r"背景[^。]{0,60}(?P<negatable_action>降低標準)。",),
            )
            checks.append((
                "否定動作不誤擋，後續正面動作仍攔截",
                _forbidden_fragment(
                    contextual, "背景不會降低標準。", []
                ) is None
                and _forbidden_fragment(contextual, "背景想降低標準。", []) == "背景想降低標準。"
                and _forbidden_fragment(
                    contextual, "背景不會降低標準。但後來背景想降低標準。", []
                ) == "背景想降低標準。",
            ))

            question_result, question_value, _ = run(
                "先講結論。虛擬盤要不要改成鏡像實盤，還是維持現狀？"
            )
            checks.append((
                "a question naming the ruling by two of its aliases is blocked",
                question_result.returncode == 0
                and question_value.get("decision") == "block"
                and question_value.get("reason", "").startswith("此事 owner 已於 2026-08-13 裁定：")
                and "不得再問" in question_value.get("reason", ""),
            ))

            statement_result, statement_value, _ = run(
                "已依裁定把虛擬盤設成鏡像實盤，參數表已同步。"
            )
            checks.append((
                "a statement that merely names the aliases is left alone",
                statement_result.returncode == 0
                and not statement_result.stdout.strip()
                and not statement_value,
            ))

            split_result, split_value, _ = run(
                "虛擬盤跟排程甲要不要一起處理？"
            )
            two_alias_result, two_alias_value, _ = run(
                "排程甲跟排程乙要不要一起處理？"
            )
            checks.append((
                "one alias from each of two active cards in one question is not a hit,"
                " while two aliases of one card are",
                split_result.returncode == 0
                and not split_value
                and two_alias_value.get("decision") == "block"
                and "排程由 owner 決定" in two_alias_value.get("reason", ""),
            ))

            active_event = {
                "session_id": "stopgate-active",
                "hook_event_name": "Stop",
                "stop_hook_active": True,
                "last_assistant_message": "我建議虛擬盤先用不同參數跑一週再說。",
            }
            active_result = synthetic(active_event, config)
            checks.append((
                "stop_hook_active is never blocked again, so the host cannot loop",
                active_result.returncode == 0
                and not active_result.stdout.strip()
                and not active_result.stderr,
            ))

            retired_result, retired_value, _ = run(
                "這裡用退休禁詞，另外退休甲跟退休乙要不要恢復？"
            )
            checks.append((
                "a superseded card neither forbids nor answers for the owner",
                retired_result.returncode == 0 and not retired_value,
            ))

            repeat_message = "我建議虛擬盤先用不同參數跑一週再說。"
            _first, first_value, repeat_session = run(repeat_message)
            _second, second_value, _ = run(repeat_message, session=repeat_session)
            markers = sorted(
                path.name
                for path in recall_marker_directory(repeat_session).iterdir()
                if path.is_file()
            )
            checks.append((
                "the same message is blocked once per session, from the recall marker directory",
                first_value.get("decision") == "block"
                and not second_value
                and len(markers) == 1
                and markers[0].startswith(memspec.STOP_GATE_MARKER_PREFIX),
            ))
            clear_recall_markers(repeat_session)
            checks.append((
                "PreCompact's marker sweep covers the stop marker",
                not recall_marker_directory(repeat_session).exists(),
            ))

            codex_result, codex_value, _ = run(
                "我建議虛擬盤先用不同參數跑一週再說。",
                extra={
                    "transcript_path": os.fspath(root / "synthetic.jsonl"),
                    "cwd": os.fspath(root),
                },
            )
            checks.append((
                "the Codex-shaped event blocks through the same adapter",
                codex_result.returncode == 0
                and set(codex_value) == {"decision", "reason"}
                and codex_value.get("decision") == "block",
            ))

            # A cache that outlived the card it summarised would silently stop
            # enforcing a ruling the owner just tightened.
            cache_path = vault / memspec.FTS_INDEX_DIRECTORY / _DECISION_CACHE_FILENAME
            cached_before = json.loads(cache_path.read_text(encoding="utf-8"))
            mirror = vault / "mirror.md"
            mirror.write_text(
                mirror.read_text(encoding="utf-8").replace("  - 兩套參數\n", "  - 兩套參數\n  - 分開調參\n"),
                encoding="utf-8",
            )
            edited_result, edited_value, _ = run("我打算分開調參，兩邊各自最佳化。")
            checks.append((
                "the manifest cache is written and a rewritten card is re-read",
                cached_before.get("version") == _DECISION_CACHE_VERSION
                and len(cached_before.get("manifest") or ()) >= 3
                and edited_result.returncode == 0
                and edited_value.get("decision") == "block"
                and "分開調參" in edited_value.get("reason", ""),
            ))

            plain_vault = root / "plain-vault"
            plain_vault.mkdir()
            (plain_vault / "note.md").write_text(
                "---\nname: note\ndescription: 2026-09-01 沒有裁定的卡\n---\nbody\n",
                encoding="utf-8",
            )
            plain_config = root / "plain-config.json"
            write_config(plain_config, [plain_vault])
            plain_result = synthetic(
                {
                    "session_id": "stopgate-plain",
                    "stop_hook_active": False,
                    "last_assistant_message": "虛擬盤要不要改成鏡像實盤？",
                },
                plain_config,
            )
            checks.append((
                "a vault with no decision card emits nothing and exits 0",
                plain_result.returncode == 0
                and not plain_result.stdout
                and not plain_result.stderr,
            ))

            bad_config = root / "bad-config.json"
            bad_config.write_text("{broken", encoding="utf-8")
            bad_result = synthetic(
                {
                    "session_id": "stopgate-bad",
                    "stop_hook_active": False,
                    "last_assistant_message": "我建議虛擬盤先用不同參數跑一週再說。",
                },
                bad_config,
            )
            checks.append((
                # 壞掉的設定檔照舊 fail-open（不擋住工作），但不再安靜：裝了卻用不了
                # 的時候，「這一回合沒有被任何規則檢查過」必須講出來。沒有設定檔才安靜，
                # 那代表這個專案沒在用 Epitype，不是壞掉——動作閘那一題釘的就是那一邊。
                "bad config fails open, and says the rule layer did not load",
                bad_result.returncode == 0
                and not bad_result.stdout
                and "規則層這次沒有生效" in bad_result.stderr,
            ))

            evil_vault = root / "evil-vault"
            evil_vault.mkdir()
            (evil_vault / "evil.md").write_text(
                "---\n"
                "name: 指數正則\n"
                "description: 2026-09-06 壞正則\n"
                f"{memspec.DECISION_KEY_FIELD}: evil-regex\n"
                f"{memspec.DECISION_STATUS_FIELD}: {memspec.ACTIVE_DECISION_STATUS}\n"
                f"{memspec.CURRENT_DECISION_AT_FIELD}: 2026-09-06\n"
                f"{memspec.OWNER_QUOTE_FIELD}: 壞正則不得生效\n"
                f"{memspec.ALIASES_FIELD}: [唯一別名]\n"
                f"{memspec.FORBIDDEN_FIELD}: ['(a+)+b']\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            evil_config = root / "evil-config.json"
            write_config(evil_config, [evil_vault])
            evil_result = synthetic(
                {
                    "session_id": "stopgate-evil",
                    "stop_hook_active": False,
                    "last_assistant_message": "aaaaaaaaaaaaaaaaaaaaaaaac 這行不該被擋。",
                },
                evil_config,
            )
            checks.append((
                "an exponential forbidden pattern is dropped, named on stderr, and blocks nothing",
                evil_result.returncode == 0
                and not evil_result.stdout.strip()
                and "evil-regex" in evil_result.stderr
                and memspec.STOP_GATE_PATTERN_DEFECT.split("{", 1)[0] in evil_result.stderr,
            ))

            # Owner 2026-09-09 (§30): Stop keeps the decision gate and writes no
            # commitment ledger. A turn whose tail is a promise still blocks on the
            # forbidden phrase, and leaves nothing behind in the vault.
            ledger = vault / memspec.FTS_INDEX_DIRECTORY / "commitments.jsonl"
            before = ledger.stat().st_mtime_ns if ledger.exists() else None
            promise = "我等一下會把參數表補上。"
            blocked_result, blocked_value, _ = run(
                "我建議虛擬盤先用不同參數跑一週再說。" + promise
            )
            after = ledger.stat().st_mtime_ns if ledger.exists() else None
            checks.append((
                "a promise in the turn's tail still blocks and writes no ledger",
                blocked_result.returncode == 0
                and blocked_value.get("decision") == "block"
                and before == after,
            ))

            # ---- fence_shell：指令區塊交給 owner 實際的殼層解析器驗語法 ----
            fence_vault = root / "fence-vault"
            fence_vault.mkdir()
            (fence_vault / "run-button.md").write_text(
                "---\n"
                "name: 執行鍵區塊\n"
                "description: 2026-09-25 給 owner 的指令區塊要能在 Windows PowerShell 5.1 直接跑\n"
                f"{memspec.ALIASES_FIELD}: [執行鍵]\n"
                f"{memspec.FENCE_SHELL_FIELD}: {memspec.FENCE_SHELL_POWERSHELL}\n"
                "metadata:\n  type: feedback\n"
                "---\nbody\n",
                encoding="utf-8",
            )
            fence_config = root / "fence-config.json"
            write_config(fence_config, [fence_vault])
            home = {"HOME": os.fspath(root), "USERPROFILE": os.fspath(root)}
            native_powershell = os.name == "nt"

            def fence_run(message):
                session_id = f"stopgate-fence-{uuid.uuid4().hex}"
                sessions.append(session_id)
                result = synthetic(
                    {"session_id": session_id, "hook_event_name": "Stop",
                     "stop_hook_active": False, "last_assistant_message": message},
                    fence_config, environment=home,
                )
                value = json.loads(result.stdout) if result.stdout.strip() else {}
                return result, value, session_id

            # 2026-09-25 原句：宿主要求標 bash，owner 按下去是 PowerShell 5.1 → ParserError。
            original = 'cd C:/titan && PYTHONIOENCODING=utf-8 "...python.exe" "...py"'
            native_parse_ok = True
            if native_powershell:
                try:
                    parsed = _parse_powershell(
                        [original,
                         '$env:PYTHONIOENCODING = "utf-8"; & "C:\\python.exe" "C:\\a.py"',
                         "git -C C:\\repo log --oneline -5"],
                        timeout=30,
                    )
                    native_parse_ok = (
                        parsed[0] is not None
                        and parsed[0][0] == 1
                        and "&&" in parsed[0][1]
                        and parsed[1:] == [None, None]
                    )
                except Exception:
                    native_parse_ok = False
            checks.append((
                "fence_shell: Windows PowerShell 5.1 真解析器擋原句並放行兩種合法寫法",
                native_parse_ok,
            ))
            bad_result, bad_value, bad_session = fence_run(
                "好，直接跑這個：\n```bash\n" + original + "\n```\n")
            bad_reason = bad_value.get("reason", "")
            fence_log = fence_vault / memspec.GATE_LOG_FILENAME
            fence_rows = [
                json.loads(line)
                for line in (fence_log.read_text(encoding="utf-8").splitlines()
                             if fence_log.exists() else ())
                if line.strip()
            ]
            bad_logged = any(
                row.get("kind") == memspec.STOP_GATE_LOG_KIND
                and row.get("decision") == "執行鍵區塊"
                and row.get("rule") == memspec.FENCE_SHELL_RULE
                and row.get("session_id") == bad_session
                for row in fence_rows
            )
            blocked_bad = (
                bad_result.returncode == 0
                and bad_value.get("decision") == "block"
                and "執行鍵區塊" in bad_reason
                and "第 1 個程式碼區塊（bash）" in bad_reason
                and "Windows PowerShell 5.1" in bad_reason
                and "第 1 行" in bad_reason
                and "&&" in bad_reason
                and "owner 按執行會直接失敗" in bad_reason
                and bad_logged
            )
            timed_out_bad = (
                bad_result.returncode == 0
                and not bad_result.stdout.strip()
                and "TimeoutExpired" in bad_result.stderr
                and not bad_logged
            )
            checks.append((
                "fence_shell: 原句被攔截；解析器逾時或不存在時明確放行並回報",
                (blocked_bad or timed_out_bad) if native_powershell else (
                    bad_result.returncode == 0
                    and not bad_result.stdout.strip()
                    and "FileNotFoundError" in bad_result.stderr
                    and not any(row.get("session_id") == bad_session for row in fence_rows)
                ),
            ))
            good_result, good_value, _ = fence_run(
                "兩個都可以直接按：\n```powershell\n"
                '$env:PYTHONIOENCODING = "utf-8"; & "C:\\python.exe" "C:\\a.py"\n'
                "```\n以及\n```bash\ngit -C C:\\repo log --oneline -5\n```\n")
            checks.append((
                "fence_shell: 合法 PowerShell 放行；解析器不可用時在 stderr 回報",
                good_result.returncode == 0
                and not good_result.stdout.strip()
                and ((not good_result.stderr.strip()
                      or "TimeoutExpired" in good_result.stderr) if native_powershell
                     else "FileNotFoundError" in good_result.stderr),
            ))
            prose_result, _prose_value, _ = fence_run(
                "先 cd C:/titan && python a.py 再看結果；沒標語言的區塊不查：\n"
                "```\ncd C:/titan && python a.py\n```\n")
            checks.append((
                "fence_shell: 區塊外的 && 與未標語言的區塊都不受影響",
                prose_result.returncode == 0
                and not prose_result.stdout.strip()
                and not prose_result.stderr.strip(),
            ))

            fence_decision = _Decision(
                key="執行鍵區塊", decided_at="", quote="", forbidden=(), aliases=(),
                path=fence_vault / "run-button.md", decided_by="", require_when="",
                require_text="", advice="", applies_to="", turn_check="",
                turn_check_limit="", fence_shell=memspec.FENCE_SHELL_POWERSHELL,
            )
            bad_block = "```bash\n" + original + "\n```\n"
            _FENCE_RESULTS.clear()
            formatted_bad = _fence_shell_gap(
                fence_decision, bad_block, {}, time.monotonic(), [],
                parser=lambda shell, blocks, timeout: [(1, "Unexpected token '&&'")],
            )
            checks.append((
                "fence_shell: parser result names the exact block, line and syntax error",
                formatted_bad is not None
                and "第 1 個程式碼區塊（bash）" in formatted_bad
                and "第 1 行" in formatted_bad
                and "&&" in formatted_bad,
            ))
            calls = []

            def recording(shell, blocks, timeout):
                calls.append((shell, list(blocks)))
                return [None] * len(blocks)

            _FENCE_RESULTS.clear()
            skipped = [
                _fence_shell_gap(fence_decision, "先 cd a && b，再看。", {}, time.monotonic(), [],
                                 parser=recording),
                _fence_shell_gap(fence_decision, "```\ncd a && b\n```", {}, time.monotonic(), [],
                                 parser=recording),
                _fence_shell_gap(fence_decision._replace(fence_langs=("powershell",)), bad_block,
                                 {}, time.monotonic(), [], parser=recording),
                _fence_shell_gap(fence_decision, bad_block,
                                 {"hook_event_name": memspec.SUBAGENT_STOP_EVENT},
                                 time.monotonic(), [], parser=recording),
            ]
            checks.append((
                "fence_shell: 沒有相符區塊（散文、未標語言、不在 fence_langs、子代理）時解析器一次都不起",
                skipped == [None, None, None, None] and calls == [],
            ))

            saved_parser = globals()["_parse_shell_blocks"]
            globals()["_parse_shell_blocks"] = recording
            try:
                _FENCE_RESULTS.clear()
                plain_decisions = _decisions(vault, time.monotonic(), [])
                no_fence_verdict = _first_violation(
                    plain_decisions, {}, "照這樣跑：\n" + bad_block,
                    {memspec.CONFIG_VAULTS_FIELD: [vault], "_unavailable_vaults": []},
                    time.monotonic(), [],
                )
            finally:
                globals()["_parse_shell_blocks"] = saved_parser
            checks.append((
                "fence_shell: 沒有任何卡宣告 fence_shell 時，即使有壞的 bash 區塊也完全不啟動解析器",
                plain_decisions
                and not any(decision.fence_shell for decision in plain_decisions)
                and no_fence_verdict is None
                and calls == [],
            ))

            _FENCE_RESULTS.clear()
            late_defects = []
            late = _fence_shell_gap(
                fence_decision, bad_block, {}, time.monotonic(), late_defects,
                parser=lambda shell, blocks, timeout: _parse_powershell(blocks, 0.001))
            _FENCE_RESULTS.clear()
            missing_defects = []
            missing = _fence_shell_gap(
                fence_decision, bad_block, {}, time.monotonic(), missing_defects,
                parser=lambda shell, blocks, timeout: _parse_powershell(
                    blocks, timeout, executable=os.fspath(root / "no-such" / "powershell.exe")))
            checks.append((
                "fence_shell: 解析器逾時或不存在 → 放行，stderr 留一行點名卡與原因",
                late is None
                and len(late_defects) == 1
                and "執行鍵區塊" in late_defects[0]
                and ("TimeoutExpired" in late_defects[0] or "FileNotFoundError" in late_defects[0])
                and missing is None
                and len(missing_defects) == 1
                and "FileNotFoundError" in missing_defects[0],
            ))

            _FENCE_RESULTS.clear()
            unknown_defects = []
            unknown = _fence_shell_gap(fence_decision._replace(fence_shell="pwsh"), bad_block, {},
                                       time.monotonic(), unknown_defects, parser=recording)
            checks.append((
                "fence_shell: 值不在允許集合 → 不猜殼層、放行並點名",
                unknown is None and calls == []
                and len(unknown_defects) == 1 and "pwsh" in unknown_defects[0],
            ))

            _FENCE_RESULTS.clear()
            counted = []

            def counting(shell, blocks, timeout):
                counted.append(len(blocks))
                if native_powershell:
                    return _parse_shell_blocks(shell, blocks, timeout)
                return [None, (2, "Missing closing brace")]

            second_bad = _fence_shell_gap(
                fence_decision,
                "```powershell\ngit status\n```\n說明文字\n```ps1\nif ($x) {\n  Write-Host a\n```\n",
                {}, time.monotonic(), [], parser=counting)
            checks.append((
                "fence_shell: 全部區塊一個行程一次解析；錯在第 2 個區塊就點名第 2 個",
                counted == [2]
                and second_bad is not None
                and "第 2 個程式碼區塊（ps1）" in second_bad,
            ))

            import shutil as _shutil

            _FENCE_RESULTS.clear()
            bash_decision = fence_decision._replace(fence_shell=memspec.FENCE_SHELL_BASH)
            bash_defects = []
            bash_ok = _fence_shell_gap(bash_decision, bad_block, {}, time.monotonic(), bash_defects)
            bash_bad = _fence_shell_gap(bash_decision, "```sh\nif [ -f a ]; then\n  echo a\n```",
                                        {}, time.monotonic(), bash_defects)
            checks.append((
                "fence_shell: bash 用 -n 驗——09-25 原句在 bash 合法、少了 fi 的 if 擋下（沒有 bash 則放行並點名）",
                (bash_ok is None and bash_bad is not None and "bash" in bash_bad
                 and "syntax error" in bash_bad and not bash_defects)
                if _shutil.which("bash")
                else (bash_ok is None and bash_bad is None and bash_defects),
            ))

            # 未閉合的引號：bash 回的是 unexpected EOF（不含 syntax error），照樣是語法錯。
            _FENCE_RESULTS.clear()
            quote_defects = []
            bash_quote = _fence_shell_gap(
                bash_decision, '```bash\necho "unterminated\n```', {}, time.monotonic(), quote_defects)
            _FENCE_RESULTS.clear()
            ps_quote = _fence_shell_gap(
                fence_decision, '```powershell\nWrite-Host "unterminated\n```', {}, time.monotonic(),
                quote_defects,
                parser=None if native_powershell else
                lambda shell, blocks, timeout: [(1, "The string is missing the terminator")])
            checks.append((
                "fence_shell: 未閉合引號由 bash 與 PowerShell 解析結果攔截",
                (bash_quote is not None and "unexpected EOF" in bash_quote
                 if _shutil.which("bash") else bash_quote is None)
                and ps_quote is not None
                and "第 1 個程式碼區塊（powershell）" in ps_quote
                and (not quote_defects if _shutil.which("bash") else len(quote_defects) == 1),
            ))
            _FENCE_RESULTS.clear()

            # 逾時放行是設計，但它必須跟「判斷沒擋」分得開：同一句禁語在期限已過的時鐘下
            # 不擋、不記帳，而且 stderr 明說這一回合沒檢查。
            late_session = f"stopgate-late-{uuid.uuid4().hex}"
            sessions.append(late_session)
            late_result = synthetic(
                {
                    "session_id": late_session,
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                    "last_assistant_message": "我建議虛擬盤先用不同參數跑一週再說。",
                },
                config,
                environment={"HOME": os.fspath(root), "USERPROFILE": os.fspath(root)},
                clock=memspec.HOOK_CLOCK_EXPIRED,
            )
            checks.append((
                "期限已過：禁語照樣放行、不記帳，stderr 明說這回合沒有檢查",
                late_result.returncode == 0
                and not late_result.stdout.strip()
                and memspec.STOP_GATE_EXPIRED_BEFORE_CHECK_DEFECT in late_result.stderr
                and not any(row.get("session_id") == late_session for row in gate_rows()),
            ))

            # 快取換版：舊版正例沿用、負例重認。大庫升級後第一回合，排在上限之後的既有武裝
            # 卡仍然要擋；還沒認完的那些要在 stderr 講出來。
            from epitype import cardscan

            big_vault = root / "big-vault"
            big_vault.mkdir()
            for index in range(memspec.STOP_GATE_MAX_CARDS_PER_VAULT + 5):
                (big_vault / f"note-{index:03d}.md").write_text(
                    f"---\nname: note-{index:03d}\ndescription: 2026-09-01 沒有裁定的卡\n---\nbody\n",
                    encoding="utf-8")
            last_card = big_vault / [entry[0] for entry in cardscan.scan_vault(big_vault)][-1]
            last_card.write_text(
                "---\nname: 晚到的卡\ndescription: 2026-09-25 排在上限之後的武裝卡\n"
                f"{memspec.ALIASES_FIELD}: [晚到]\n{memspec.FORBIDDEN_FIELD}: [晚到禁語]\n"
                "metadata:\n  type: feedback\n---\nbody\n",
                encoding="utf-8")
            big_config = root / "big-config.json"
            write_config(big_config, [big_vault])
            capped_line = memspec.STOP_GATE_CAPPED_DEFECT.split("{")[0]

            def big_run(message):
                session_id = f"stopgate-big-{uuid.uuid4().hex}"
                sessions.append(session_id)
                result = synthetic(
                    {"session_id": session_id, "hook_event_name": "Stop",
                     "stop_hook_active": False, "last_assistant_message": message},
                    big_config, environment=home)
                return result, json.loads(result.stdout) if result.stdout.strip() else {}

            fresh_result, fresh_value = big_run("這裡有晚到禁語。")
            big_run("今天先到這裡。")
            big_cache = big_vault / memspec.FTS_INDEX_DIRECTORY / _DECISION_CACHE_FILENAME
            legacy = json.loads(big_cache.read_text(encoding="utf-8"))
            legacy["version"] = _DECISION_CACHE_VERSION - 1
            big_cache.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            upgraded_result, upgraded_value = big_run("這裡有晚到禁語。")
            checks.append((
                "快取換版＋超過上限張卡：排在後面的武裝卡升級後第一回合仍然擋，沒認完的在 stderr 講出來",
                # 前提：這張卡確實排在上限之後——全新的庫第一回合認不到它，而且有講。
                not fresh_value and capped_line in fresh_result.stderr
                and upgraded_value.get("decision") == "block"
                and "晚到禁語" in upgraded_value.get("reason", "")
                and capped_line in upgraded_result.stderr,
            ))

            # 注入時鐘只給 run_synthetic 的子行程：正式掛鉤從環境繼承到 EPITYPE_HOOK_CLOCK
            # 時，期限照真時鐘生效（起始 1,000 秒前 → 已逾時），並在 stderr 講一句。
            import subprocess as _subprocess

            probe = (
                "import sys, time; sys.path[:0] = [{adapters!r}, {repo!r}]\n"
                "import _hook_common\n"
                "print(_hook_common.expired(time.monotonic() - 1000))\n"
            ).format(adapters=os.fspath(Path(__file__).resolve().parent), repo=os.fspath(_REPO_ROOT))
            outside_home = root / "outside-home"
            outside_home.mkdir()
            probe_config = root / "probe" / "config.json"
            probe_config.parent.mkdir()
            probe_config.write_text("{}", encoding="utf-8")
            token = uuid.uuid4().hex
            (probe_config.parent / (memspec.HOOK_CLOCK_CREDENTIAL_PREFIX + token)).write_text(
                memspec.HOOK_CLOCK_FROZEN, encoding="ascii")

            def clock_probe(value, home):
                environment = {**os.environ, memspec.HOOK_CLOCK_ENV: value,
                               memspec.EPITYPE_CONFIG_ENV: os.fspath(probe_config),
                               "HOME": os.fspath(home), "USERPROFILE": os.fspath(home),
                               "PYTHONDONTWRITEBYTECODE": "1"}
                result = _subprocess.run(
                    [sys.executable, "-c", probe], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=environment,
                    timeout=_SELFTEST_HANG_SECONDS, check=False)
                return result.stdout.strip(), memspec.HOOK_CLOCK_REJECTED_NOTICE.format(
                    env=memspec.HOOK_CLOCK_ENV) in result.stderr

            inherited = clock_probe(memspec.HOOK_CLOCK_FROZEN, probe_config.parent)
            wrong_home = clock_probe(token, outside_home)
            forged = clock_probe(uuid.uuid4().hex, probe_config.parent)
            genuine = clock_probe(token, probe_config.parent)
            checks.append((
                "正式路徑繼承 EPITYPE_HOOK_CLOCK=frozen（或憑證不吻合）：期限照常生效，stderr 說明忽略了注入",
                inherited == ("True", True)
                and wrong_home == ("True", True)
                and forged == ("True", True)
                and genuine == ("False", False)
                and not list(root.glob(memspec.HOOK_CLOCK_CREDENTIAL_PREFIX + "*")),
            ))

            blown = time.monotonic() - memspec.HOOK_TIMEOUT_SECONDS - 1
            checks.append((
                "期限在 marker 寫下之後才到：block 照樣送出，不會只留帳不擋",
                _emits({"decision": "block", "reason": "x"}, blown)
                and not _emits(None, time.monotonic())
                and not _emits({"decision": "approve"}, blown),
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        for session_id in sessions:
            clear_recall_markers(session_id)

    passed = sum(bool(ok) for _, ok in checks)
    total = 38
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _emits(value, started_at):
    """A block reaches the host even past the deadline.

    `_verdict` claims the same-session dedupe marker and appends the audit row
    before it returns, so a deadline crossed in between used to drop the block
    while leaving the ruling on the ledger and unable to fire again this session
    (2026-09-06 review). The action gate's `main` has the same shape."""
    return value is not None and (
        value.get("decision") == "block" or not expired(started_at)
    )


def main():
    if "--selftest" in sys.argv[1:]:
        with isolated_temp_root():
            return _selftest()
    defects = []
    try:
        event = read_event(sys.stdin)
        value = _handle(event, _STARTED_AT, defects)
        # 斷點檔：不管這一回合有沒有被擋，都把「改了什麼、跑了什麼、最後一句話」寫下來。
        # 進度只存在對話裡的話，這一場結束或被停掉就等於沒發生過（owner 2026-09-19）。
        # 純副作用，寫不出來也不影響這道閘的判斷。
        try:
            from epitype import handoff

            config = load_config(_STARTED_AT)
            if config is not None and not expired(_STARTED_AT):
                vault = governance_vault(config, for_write=True)
                if str(event.get("hook_event_name") or "") == memspec.SUBAGENT_STOP_EVENT:
                    # 子代理說過的話哪裡都沒有：不在宿主的對話紀錄裡，它自己的工作檔是
                    # 0 位元組。這個時機是唯一看得見那句話的地方，所以在這裡落檔——
                    # 不落檔就等於「當下擋得住、事後查不到」，檢討對子代理整段是盲的。
                    handoff.record_subagent(vault, event, _turn_texts(event.get("transcript_path")))
                else:
                    handoff.update(
                        vault,
                        event.get("session_id"),
                        event.get("transcript_path"),
                        event.get("cwd"),
                    )
        except Exception:
            pass
        for line in defects[: memspec.GATE_DEFECT_MAX_LINES]:
            print(line, file=sys.stderr)
        if _emits(value, _STARTED_AT):
            emit(value)
    except Exception as exc:
        # 這裡是最後一道：設定檔壞了、記憶庫讀不到、程式本身有 bug，全都走這一圈。
        # 仍然 fail-open（不擋住工作），但裝了卻用不了的時候一定要講一句——安靜退場
        # 跟「沒有東西要擋」在外面看起來一模一樣，而這正是本專案最不能容忍的那種壞掉。
        # 沒有設定檔則照舊安靜：那代表這個專案根本沒在用 Epitype，不是壞掉。
        try:
            if config_path().exists():
                print(memspec.GATE_DEGRADED_NOTICE.format(
                    gate=memspec.GATE_NAME_STOP,
                    reason=type(exc).__name__), file=sys.stderr)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
