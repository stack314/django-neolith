# 0035. The boundary check reads every file whose name Python's import system reaches, an identifier or not

- **Status:** Accepted
- **Accepted:** 2026-11-25
- **Date:** 2026-11-25
- **Narrows:** [0030](0030-the-boundary-check-uses-the-standard-library-only.md) (two sentences of its Decision 3, Decision 1 here). It gains `Narrowed by 0035 (Decision 1)` under its status (D2).

## Context

[0030](0030-the-boundary-check-uses-the-standard-library-only.md) Decision 3, under *Which files of an app*, reads:

> Every `.py` file under the directories of the app's package, as its `__path__` lists them, at any depth, except under `__pycache__` and every file and directory whose name no dotted name reaches: one that contains a dot, other than a file's `.py`, or that is not an identifier.

The same bullet ends: "Migration modules and test modules are app code and are read (K8 asks the same of neolith's own tests)." A migration's file is named `0001_initial.py`, and `"0001_initial".isidentifier()` is false, so the first sentence skips what the last says is read. Under *How a dotted name is placed, without running code*, the map's walk repeats the rule: "The walk skips `__pycache__`, the names no dotted name reaches as above, symbolic links to directories, and directories that hold a `pyvenv.cfg`, which are virtual environments."

A dotted name does reach such a file. Django lists an app's migrations with `pkgutil.iter_modules(module.__path__)` and imports each with `import_module("%s.%s" % (module_name, migration_name))` (`django/db/migrations/loader.py`, lines 113 and 120 in 5.2.19, 114 and 121 in 6.1.3). `pkgutil.iter_modules` names a file by `inspect.getmodulename`, which takes off any suffix of `importlib.machinery.all_suffixes()`, and drops only a name that is `__init__` or that contains a dot (`pkgutil.py`, `_iter_file_finder_modules`, lines 144 to 166 in Python 3.13). The path finder looks up the last part of a dotted name as it is written (`importlib/_bootstrap_external.py`, `FileFinder.find_spec`, lines 1624 to 1673 in 3.13), so `import_module("pkg.0001_initial")` and `import_module("pkg.my-module")` both load their files, while no dotted name holds a dot inside one part. #14 settled open point 2 with the sentence quoted above; this record replaces that part of the settlement, as decided on #14.

## Options

1. **Skip a name that is not an identifier**, as 0030 reads. Every migration of every app goes unread, and a module that `import_module` loads by a string is a blind spot no line reports.
2. **Read every name the import system reaches**: skip only a name with a dot inside the module name it gives. What a module imports is judged wherever Python can load it from. A name that is not an identifier can hold any character a file name can, so a path is printed escaped, as 0030 Decision 8 already prints every path.

## Decision

Option 2.

1. **The walks skip `__pycache__` and a name that contains a dot other than a file's suffix, and read a name that is not an identifier.** Both sentences quoted above now read, for each walk: a file's module name is its name less a suffix Python's path finder loads (`importlib.machinery.all_suffixes()`, as `inspect.getmodulename` reads it), `.py` for a file that is read and an extension or bytecode suffix for a file without Python source (`neolith.W104`); a file or directory is skipped when that name, or the directory's name, contains a dot, or is `__pycache__`, and is read otherwise, whether or not it is an identifier. Symbolic links to directories and, in the map's walk, directories that hold a `pyvenv.cfg` stay skipped as 0030 says. The names the walk gives the modules directly in a package directory are those `pkgutil.iter_modules` lists for it, and a directory with no `__init__` file is read as a namespace portion, as the path finder reaches it. A `<path>` is still printed with each control, format or line-ending character written as its escape (0030 Decision 8).

## Consequences

- Migration modules are read, as 0030 Decision 3 says they are, and so is any module whose name is not an identifier, such as `my-module.py`.
- A test generates package directories with names that are and are not identifiers, with and without a dot, and checks that the walk reads exactly the names `pkgutil.iter_modules` lists, and that a generated migrations package is read.
- Nothing else in 0030 changes. Its other skips, its findings and its messages stay as written.
