---
name: A completion claim carries its evidence
description: saying the work is finished must come with the command, count or commit that shows it
aliases:
  - all tests pass
  - claim evidence
  - 宣稱完成要附證據
starter: epitype
last_verified_at: 2026-09-20
require_when: "(?i)(?<!not )(?<!n't )(?<!never )(?<!unless )(?<!once )(?<!when )(?<!until )(?<!after )(?<!if )(all tests pass|all tests passed|all green|it is done|this is done|all done)"
require_text: '(\d+ ?/ ?\d+|commit [0-9a-f]{7,40}|python |pytest|npm test|npm run|cargo test|go test|RESULT PASS|TOTAL PASS|I ran )'
example_blocks:
  - "All tests pass, so the change is ready to merge."
  - "This is done and you can ship it."
example_allows:
  - "I ran python tests/run_all.py and all tests pass, 72/72."
  - "Not all tests pass yet; two are still red."
  - "Once all tests pass I will open the pull request."
---

Stops the end of a turn that claims the work is finished without anything a reader can
check: a command that was actually run, a count like 12/12, or a commit hash. The claim
itself is invisible from outside; the sentence that reports the check is not, so the rule
asks for the sentence.

`require_when` says when the evidence is owed and `require_text` says what counts as
evidence — widen either list to fit your toolchain (add `mvn test`, `dotnet test`). The
negative lookbehinds keep phrases like `not all tests pass` and `once all tests pass` out
of the trigger; keep them if you edit the pattern. Delete this file to switch the rule off.
