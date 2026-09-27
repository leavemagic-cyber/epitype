# Epitype Essentials

Epitype Essentials gives Claude three short workflow skills for everyday project work. They help Claude check a completion claim against an actual result, stage only the Git files it meant to change, and explain progress in plain language. Each skill has a narrow trigger, concrete steps, and examples. You can use them in Claude chat, Cowork, or Claude Code.

## Skills

- **Verify completion** applies when Claude reports that work is finished or reviews someone else's completion claim. It asks for the exact result and a clear statement of what remains unverified.
- **Safe Git changes** applies before staging or committing in a Git repository. It uses the current status and explicit file paths so other work in the checkout stays visible.
- **Clear status** applies when explaining progress or a result to a user. It starts with the practical answer, then gives the evidence and any remaining action.

Example requests:

1. “Check whether this change is actually ready to ship, and tell me what you verified.”
2. “Commit the two files you changed while preserving other work in this repository.”
3. “Explain the current release status to me in plain language.”

This bundle contains Markdown instructions only. It runs no commands on installation, reads no Claude memory or conversation history, stores no user data, and connects to no service. Claude may use its normal tools when a user asks for work that needs them. Review a suggested command before approving it in your own environment.

See the [privacy policy](PRIVACY.md) for the plugin's data handling scope.

The [Epitype Python package](https://github.com/leavemagic-cyber/epitype) offers local hook-based memory management as a separate, optional installation. Installing this Claude plugin adds only the three skills listed above; it does not install the Python package or its hooks.

For support, open an [Epitype issue](https://github.com/leavemagic-cyber/epitype/issues).
