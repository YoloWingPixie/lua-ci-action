# Lua CI Action

Run Lua code-quality checks from one prebuilt container. Consumer repositories do not compile the action or install its tools.

The action checks:

- StyLua formatting. This fails the action.
- Lua 5.1 syntax. This fails the action.
- Function and file complexity, parameter counts, and duplicate code. These are advisory.
- Strict source-policy rules. New violations and stale baseline entries fail the action.

The source policy rejects accidental globals, dynamic code loading, multiple statements per line, one-line control bodies, empty control bodies, calls without parentheses, duplicate or mixed table keys, shadowed locals, dynamic or nested `require`, debug and raw table escape functions, hidden metatable behavior, monkey-patching imports, implicit literal coercion, mutation during table traversal, inconsistent return counts, excessive nesting, excessive parameters, and oversized functions.

Selene stays in the separate [`YoloWingPixie/selene-lua-linter-action`](https://github.com/YoloWingPixie/selene-lua-linter-action).

## Usage

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0

- name: Lua quality
  uses: YoloWingPixie/lua-ci-action@v1
  with:
    base-ref: ${{ github.event.pull_request.base.sha }}

- name: Selene
  uses: YoloWingPixie/selene-lua-linter-action@v1
  with:
    config-path: selene.toml
    lint-path: src
```

`base-ref` is optional. It limits complexity annotations to changed functions. Full Git history is required when it is set.

## `.lua-ci-actionrc`

The file uses strict JSON:

```json
{
  "schema_version": 1,
  "format": {
    "paths": ["src", "tests"],
    "config": "stylua.toml"
  },
  "syntax": {
    "paths": ["src", "tests"]
  },
  "complexity": {
    "source": "src",
    "ccn_warning": 17,
    "parameter_warning": 8,
    "healthy_ccn": 10,
    "healthy_parameters": 5
  },
  "source_policy": {
    "source": "src",
    "allowed_global_writes": ["Medusa"],
    "max_control_nesting": 5,
    "max_parameters": 9,
    "max_function_nloc": 100,
    "baseline": "scripts/ci/lua_source_policy_baseline.json"
  }
}
```

Each check accepts `"enabled": false`. Paths must stay inside the checked-out repository.

## Releases

1. Set the exact image tag in `action.yml`.
2. Commit the release change.
3. Create and push the matching semantic-version tag.
4. Make the GHCR package public after its first publication.
5. Move the major action tag after the image publication succeeds.
