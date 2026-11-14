# Offline regression tests

Install the Python dependencies from `requirements.txt`, then run from the repository root:

```sh
python -m unittest discover -s tests -v
```

Tests use fictional identifiers, temporary workspaces, and mocked external writes.
