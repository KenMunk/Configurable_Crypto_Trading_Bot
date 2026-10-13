# .vscode/: shared VS Code settings

Workspace settings for Visual Studio Code. They apply to anyone who opens this repository in VS Code.

## settings.json

The file holds one rule, `chat.tools.terminal.autoApprove`. It lets VS Code's AI chat agent run exactly this command in the terminal without asking for approval each time:

```text
.\.venv\Scripts\python -m pytest -q
```

That command runs the [test suite](../tests/README.md) quietly, using the project's virtual environment.

- **Only that exact command line matches** (`matchCommandLine` is `true`). Any other command still needs your approval.
- **It assumes the virtual environment is at `.venv`** in the repository root, which is where the [quick start](../README.md#quick-start) creates it. The path uses Windows form.

Put personal preferences in your VS Code **user** settings, not here, because this folder is shared through Git.
