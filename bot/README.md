# bot/

The Discord bot: the internship finder's modules, which arrive in a later
change. Its tests go in [`tests/bot/`](../tests/bot/).

The modules use bare imports (`import intern_store`), with this directory on
`sys.path`, rather than forming a package. `tests/bot/__init__.py` puts it
there for the tests.

**Keep this directory free of an `__init__.py`.** Under
`python -m unittest discover -s tests`, `tests/bot/` is a package called
`bot`. A second package with that name would make every import of it depend
on the order of `sys.path`. `tests/bot/test_bot_path.py` checks both rules.
