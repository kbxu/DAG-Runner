# Production setup templates

Edit the placeholders before passing a template to `--setup-file`:

- `production_setup.ps1`: replace `workdir\path`, `path\conda.exe`, and
  `environment` with the target environment name (or its full path);
- `production_setup.sh`: replace `workdir/path` and
  `path/conda` with the Conda installation prefix, then replace `environment`
  with the target environment name. A full environment path is also accepted.

The setup and each task command run in the same shell process, so the working
directory and environment settings remain available to the task.

The PowerShell template resolves a named environment through `conda.exe` and
adds the same main environment directories used by Conda's Windows activator.
It deliberately does not load `conda-hook.ps1` or environment `activate.d`
scripts, so it also works when the machine's execution policy blocks `.ps1`
activation scripts.
