# This public example is intentionally environment-neutral.
export PYTHONUNBUFFERED=1

cd "workdir/path"
CONDA_EXE="path/conda/bin/conda"
CONDA_ENV="environment"
eval "$("$CONDA_EXE" shell.bash activate "$CONDA_ENV")"
