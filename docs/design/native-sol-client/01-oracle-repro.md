# Native `sol` Grammar Oracle Reproduction

## Reproduction Result

| Check | Expected | Reproduced | Verdict |
|---|---:|---:|---|
| entries | `174` | `174` | PASS |
| serialized bytes | `120632` | `120632` | PASS |
| SHA-256 | `cfa8c95c25e14937e5f616027bd2f15d610c0fa86d542b7adc4eb5da39409ce2` | `cfa8c95c25e14937e5f616027bd2f15d610c0fa86d542b7adc4eb5da39409ce2` | PASS |

Command run from current worktree:

```text
.venv/bin/python scratch/native-sol/gen_grammar.py
entries=174 bytes=120632 sha256=cfa8c95c25e14937e5f616027bd2f15d610c0fa86d542b7adc4eb5da39409ce2
```

Independent file check:

```text
120632 scratch/native-sol/sol-call-grammar-v1.json
cfa8c95c25e14937e5f616027bd2f15d610c0fa86d542b7adc4eb5da39409ce2  scratch/native-sol/sol-call-grammar-v1.json
```

No isolated `c3eb` worktree was needed; the current tree and current `.venv` reproduced the corrected pin.

## Entry Rules That Yield 174

| Rule | Reproduced detail |
|---|---|
| Root command | `root = typer.main.get_command(solstone.think.call.call_app)`; root itself is not emitted because its callback is null. Generator: `scratch/native-sol/gen_grammar.py:59`, `scratch/native-sol/gen_grammar.py:61`. |
| Path representation | Entry `path` excludes the root `call` command and is the command-name path from root, e.g. first entry `["activities","create"]`, last entry `["transcripts","stats"]`. Generator: `scratch/native-sol/gen_grammar.py:38`, `scratch/native-sol/gen_grammar.py:43`. |
| Command leaves | Non-group leaves emit `kind="command"`; reproduced count is `170`. Generator: `scratch/native-sol/gen_grammar.py:55`. |
| Group callbacks | Click groups with non-null `.callback` emit their own `kind="callback"` entry before recursing into sorted children; reproduced count is `4`. Generator: `scratch/native-sol/gen_grammar.py:47`, `scratch/native-sol/gen_grammar.py:54`. |
| Callback paths | The four callback entries are `["identity"]`, `["journal"]`, `["journal","facet"]`, and `["navigate"]`. |
| Ordering | Group children recurse in lexicographic command-name order via `sorted(command.commands)`. Generator: `scratch/native-sol/gen_grammar.py:52`. |

Breakdown:

```text
commands=170
callbacks=4
total=174
```

Callback details:

```text
identity params=1 help='Moved to `journal identity`.'
journal params=0 help='Journal search and browsing.'
journal/facet params=0 help='Facet management.'
navigate params=1 help='Moved to `journal navigate`.'
```

## Environment

| Tool | Version |
|---|---:|
| Click | `8.3.1` |
| Typer | `0.21.1` |
| Source field | `c3eb606395862edf34d19865a2341f6e7f538edc` |

Generated files are under `scratch/native-sol/`, which is ignored by `scratch/.gitignore`.

## Minimal Generator Logic

The throwaway generator that reproduced the pin is `scratch/native-sol/gen_grammar.py`.

| Contract piece | Generator location |
|---|---|
| `source` and schema constants | `scratch/native-sol/gen_grammar.py:15`, `scratch/native-sol/gen_grammar.py:16` |
| Param object fields in Click declared order | `scratch/native-sol/gen_grammar.py:20`, `scratch/native-sol/gen_grammar.py:35` |
| Entry object fields: `path`, `kind`, `help`, `params` | `scratch/native-sol/gen_grammar.py:38`, `scratch/native-sol/gen_grammar.py:44` |
| Click group recursion and callback emission | `scratch/native-sol/gen_grammar.py:47`, `scratch/native-sol/gen_grammar.py:56` |
| Canonical JSON serialization with `ensure_ascii=False`, `sort_keys=True`, compact separators, and one trailing LF | `scratch/native-sol/gen_grammar.py:63`, `scratch/native-sol/gen_grammar.py:66` |
| Oracle bytes written to `scratch/native-sol/sol-call-grammar-v1.json` | `scratch/native-sol/gen_grammar.py:17`, `scratch/native-sol/gen_grammar.py:67` |
