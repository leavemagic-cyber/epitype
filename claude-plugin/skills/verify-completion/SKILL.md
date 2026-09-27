---
name: verify-completion
description: Use when assessing whether project work is complete, reviewing a completion claim, or reporting that your own change is finished.
---

# Verify completion

1. Name the exact change, file, build, or result being judged.
2. Check that exact subject using an available result: inspect the changed artifact, run the relevant check when the user has authorized the work, and read its output. A successful process exit alone does not prove the intended result.
3. Report what you observed with enough detail to repeat or locate the check: command, result count, artifact path, commit, or published page as appropriate.
4. Separate an inference from a checked fact. If the needed check has not run, say what remains unverified and give the next check. Do not turn “should work” into “works.”
5. Keep the check proportional to the claim. A local result does not prove that a public release or another person's environment has updated.

Example: “I changed the parser and ran `python -m unittest tests.test_parser`; 18/18 passed. I have not checked the packaged wheel.”

Do not use this skill to search Claude memory, chat history, or conversation summaries. The skill works from the current task and the results available in that task.
