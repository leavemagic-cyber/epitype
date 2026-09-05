# Exam boundary

This directory is reserved for project-specific synthetic release-exam cases.

The executable runner and its validated sample schema live in `exam/exam_runner.py` and `exam/sample_corpus.json` at the repository root. An empty directory is not evidence of a pass; run the intended corpus with `python exam/exam_runner.py <corpus.json> --strict`.
